# Bunker Mini: trigger after 20 m of cumulative travel

**English** | [简体中文](bunker-distance-demo.md) · [README](../README.md)

This example uses `/odom` (`nav_msgs/msg/Odometry`) from `rosbag2_2025_12_24-21_12_52` and sums successive valid XY position increments:

```text
cumulative_distance += hypot(current.x - previous.x, current.y - previous.y)
```

When the total first becomes **strictly greater than 20 m**, it publishes one request to `/event_recorder/trigger`:

```text
bunker_mini|odom_distance_<UUID>|<JSON trigger details>
```

The JSON includes the threshold, cumulative distance, origin/current position, frames, source timestamp and sample count. It is stored in `triggers[].reason` in the event's `.event.yaml`; the ROS trigger message remains `std_msgs/msg/String`.

## Source bag measurements

- 14,473 odometry messages, frame `odom`, child frame `base_link`.
- Total cumulative travel: **32.6211 m**.
- Maximum straight-line displacement from the origin: **10.8092 m**. Displacement would never cross 20 m in this bag.
- First crossing: valid sample **10,345**, about **206.7599 seconds** after the first odometry message, at **20.001397 m**.

The source bag is not included in the repository. Replace `/path/to/rosbag2_2025_12_24-21_12_52` below with its location.

## Run the demo

Build from the repository root:

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select event_recorder
source install/setup.bash
```

Terminal 1, from the repository root: start the recorder, GUI and detector in a separate ROS domain:

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=90 ROS_LOCALHOST_ONLY=1
ros2 launch event_recorder bunker_distance_demo.launch.py port:=8081
```

Open **http://127.0.0.1:8081** and wait for the recorder to connect. The first launch creates `bunker_distance_demo.local.yaml`, separate from the normal GUI configuration. Output defaults to `bunker_distance_bags/`. Launch arguments include `config`, `threshold_m`, `host` and `port`.

Terminal 2: replay the bag in the same domain:

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=90 ROS_LOCALHOST_ONLY=1
ros2 bag play /path/to/rosbag2_2025_12_24-21_12_52 \
  --delay 2 --disable-keyboard-controls \
  --topics /odom /imu/data_raw /scan /tf /tf_static
```

At normal speed the trigger occurs after approximately 3 minutes 27 seconds, followed by 10 seconds of capture. The GUI shows capture, writing/cooldown, then readiness; event history contains the `bunker_mini` result.

Add `--rate 4` for a faster test, reaching the threshold in about 52 seconds. **The window remains 5 seconds before and 10 seconds after in reception time.** At 4× speed it covers roughly 60 seconds of source data, not a 15-second source-time window.

The demo replays and records `/odom`, `/imu/data_raw`, `/scan`, `/tf` and `/tf_static`. All exist in this bag. It does not use the normal template's `/cmd_vel`, which is absent from this bag, or load images/point clouds. If you add topics in the GUI, include them in replay as well.

## Use the detector separately

With an existing recorder configured with a `bunker_mini` profile:

```bash
ros2 run event_recorder odom_distance_trigger --ros-args \
  -p odom_topic:=/odom \
  -p robot_type:=bunker_mini \
  -p threshold_m:=20.0
```

| Parameter | Default |
| --- | --- |
| `odom_topic` | `/odom` |
| `trigger_topic` | `/event_recorder/trigger` |
| `robot_type` | `bunker_mini` |
| `threshold_m` | `20.0` |
| `qos_reliability` | `reliable` |

For a best-effort publisher, set `qos_reliability:=best_effort`. Inspect cumulative distance and detector status:

```bash
ros2 topic echo /odom_distance_trigger/status std_msgs/msg/String
```

The detector publishes at most once per startup/reset, while continuing to update distance. To test again, stop replay, reset, then replay from the beginning:

```bash
ros2 service call /odom_distance_trigger/reset std_srvs/srv/Trigger '{}'
```

Reset clears distance, samples and faults and generates a new request ID. The next valid sample becomes the origin. Do not reset while the old replay continues and then jump back to the beginning: this causes a backwards-time fault.

## Limits and detector states

- Travel uses XY only, not Z, velocity integration or displacement from the origin.
- Nonfinite positions, empty frames and duplicate nonzero timestamps are ignored. Frame/child-frame changes or backwards source time enter `FAULT` until explicitly reset. Replay with `--loop` does not reset automatically.
- Odometry noise accumulates. Position jumps without a frame/time change can trigger early; this example has no localization-jump detection, filtering or debounce.
- `ARMED` means the detector is waiting for the crossing. `TRIGGER_PUBLISHED` means a request was sent, not that the recorder accepted it. Check recorder status and event metadata. Busy/cooldown rejection does not cause an automatic retry.
- If no trigger subscriber is discovered at the crossing, status becomes `UNDELIVERED`, without delayed resend. Start the recorder, reset and replay again. Always wait for readiness before replay.
- The example sends no robot motion commands. Closing the browser does not stop detection/recording. Ctrl+C on the demo launch stops all three processes.

## Automated verification

From the repository root, use a fresh output directory:

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
/usr/bin/python3 tests/test_distance_accumulator.py
ROS_DOMAIN_ID=90 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_bunker_distance.py \
  --bag /path/to/rosbag2_2025_12_24-21_12_52 \
  --output validation/my_bunker_distance_test --rate 4
```

The test independently computes the crossing from the source bag, then drives the example with `ros2 bag play`. It checks one publication, matching crossing timestamp/distance, accepted request ID/reason, a complete 15-second reception window, an exact contiguous sequence of source odometry payloads in MCAP, and reset behavior. It does not modify the source bag.

The recorded [20 m test report](../reports/bunker_distance_20m.json) shows one accepted trigger, a `COMPLETED` event and 3,002 contiguous odometry messages, with distance matching the offline reference.
