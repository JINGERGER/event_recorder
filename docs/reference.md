# Reference guide

**English** | [简体中文](reference.zh-CN.md) · [README](../README.md)

## Configuration

Start with [recorder.yaml](../src/event_recorder/config/recorder.yaml). GUI launch creates `recorder.local.yaml` in the working directory if absent; the distance demo uses its own `bunker_distance_demo.local.yaml`. Relative output paths resolve from the process working directory. Settings are read at startup; restart after saving changes.

Parameters below are under `event_recorder.ros__parameters`. Defaults shown are those of the example configuration.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `pre_trigger_seconds` | 5.0 | History to include before the trigger. |
| `post_trigger_seconds` | 10.0 | Capture time after the trigger. |
| `history_retention_seconds` | 8.0 | History retention, also limited by capacity. |
| `max_event_seconds` | 300.0 | Upper limit for pre + post duration. |
| `trigger_cooldown_seconds` | 3.0 | Cooldown measured from the target window end. |
| `dedup_seconds` | 60.0 | Accepted request ID retention. |
| `history_bytes` | 268435456 | 256 MiB history budget. |
| `input_bytes` | 67108864 | 64 MiB input queue budget. |
| `writer_bytes` | 536870912 | 512 MiB writer queue budget. |
| `context_bytes` | 16777216 | 16 MiB context budget. |
| `queue_items` | 100000 | Item capacity limit alongside byte budgets. |
| `max_pending_events` | 8 | Compatibility parameter; does not enable concurrent events. |
| `minimum_free_bytes` | 1073741824 | 1 GiB minimum free disk space. |
| `output_directory` | `event_bags` | Output bag and event metadata directory. |
| `storage_id` | `mcap` | `mcap` or `sqlite3`; a node started without configuration defaults to SQLite3. |

### Topics and robot profiles

`topics` and `types` register ordinary topics and fully qualified message types. Optional `reliability` and `durability` arrays correspond to the same indices; omitted arrays default to `best_effort` and `volatile`. Choose QoS compatible with the publishers. `context_topics` and `context_types` register context subscriptions using reliable/transient-local QoS.

All registered topics are continuously subscribed and share history. `trigger_list` names the profiles; `triggers.<name>.topics` selects what each event writes. Context topics must also be selected explicitly. Names are case-sensitive and match `[A-Za-z][A-Za-z0-9_]*`. `default_trigger` selects the generic trigger service's profile. Unknown topics, duplicate profiles and empty profile topic lists are invalid.

The following is a fragment to combine with the topic registration in the example YAML:

```yaml
trigger_list: [go2, bunker_mini, piper]
default_trigger: go2
triggers:
  go2:
    topics: [/lidar_imu, /tf_static]
  bunker_mini:
    topics: [/odom, /cmd_vel, /tf_static]
  piper:
    topics: [/joint_states, /tf, /tf_static]
```

Without `trigger_list`, legacy configuration creates an implicit `default` profile selecting all registered topics. Its topic input accepts the legacy `request_id|reason` or anonymous reason format. It still follows the current single-event/cooldown policy.

## Recording semantics and limits

- A monotonic reception clock defines the window. ROS reception time becomes the bag timestamp; sensor header timestamps do not define the window. `use_sim_time` changes ROS timestamps, not window duration. A divergence over 100 ms between ROS and monotonic clock increments is marked `ros_clock_jump`.
- Only one event may capture or finalize globally. Requests during capture, cooldown, or pending writer completion are rejected, without merging, extending or deferred execution. Cooldown starts at the target window end, and writing must also finish before another trigger can be accepted.
- Deduplication is keyed by `(profile, request_id)`; only accepted requests enter the cache. Empty IDs bypass deduplication. Different profiles can reuse the same ID.
- Input and writer overload drop new messages; history capacity pressure evicts the oldest messages. Known affected events are marked incomplete. Drops on unselected topics do not directly mark the current event incomplete, although shared capacity affects retention. Time-watermark checks are conservative. A zero message count alone is not proof of loss.
- Byte budgets account for payload capacity and fixed message overhead, **not a hard RSS limit**. Payloads are shared; DDS, allocations, in-flight messages, deserialization and storage plugins add memory. Context snapshots can temporarily add approximately another context budget. RAM history is lost on process failure.
- `/tf_static` is aggregated by child frame, retaining the baseline before the window and updates within it. Other context topics replace their full-state baseline; they do not aggregate incremental updates from multiple publishers.
- Context is injected with its original ROS reception timestamp. Bag duration can therefore exceed the capture window; check ordinary messages and event metadata when validating the window.
- `COMPLETED` means writing finished with no detected local incompleteness; `PARTIAL` indicates known missing/truncated data; `FAILED` indicates failure. Disk space below the threshold or writer exceptions cause failure. Graceful shutdown drains accepted work and marks unfinished windows truncated. Metadata may also be unavailable if storage cannot be written. Crash-leftover `CAPTURING` is not completion.
- Completeness covers locally observable loss, not loss before reception. End-to-end guarantees require publisher sequence information. Overlapping events, sensor-time windows and persistent pre-crash history are not implemented.

## ROS and HTTP interfaces

ROS names below assume the default node namespace.

| Interface | Type | Behavior |
| --- | --- | --- |
| Topic `/event_recorder/trigger` | `std_msgs/msg/String` | `data: 'robot_type\|request_id\|reason'`; a profile name alone is an anonymous request. Publication is not acceptance confirmation. |
| Service `/event_recorder/triggers/<robot_type>` | `std_srvs/srv/Trigger` | Triggers the selected profile. |
| Service `/event_recorder/trigger` | `std_srvs/srv/Trigger` | Triggers `default_trigger`. |
| Topic `/event_recorder/status` | `std_msgs/msg/String` | 5 Hz YAML snapshot, reliable/transient-local: running configuration, profiles, buffers, queues, capture/cooldown and last event. |

Trigger service `success` reports actual acceptance. `message` is YAML containing `accepted`, `rule`, `event_id` and `reason`. Rejection reasons distinguish busy, cooldown, finalizing and duplicate IDs. A timeout leaves the outcome unknown; inspect status before retrying.

| HTTP endpoint | Purpose |
| --- | --- |
| `GET /api/state` | Recorder state. |
| `GET /api/config` | Saved configuration and revision. |
| `GET /api/topics` | ROS graph topic discovery. |
| `GET /api/events` | Latest 100 event records. |
| `POST /api/trigger` | Body `{"rule":"go2"}`; returns the trigger result, without automatic retry. |
| `POST /api/config` | Body contains `parameters` and `revision`; saves the file only. |

Configuration saves are validated and atomic, keep one `.bak`, and reject revision conflicts. The GUI requires at least one ordinary topic and omits optional empty arrays for Humble YAML compatibility. It marks the recorder offline after 2 seconds without new status and disables triggering. Buttons use the running configuration until restart.

UI language is a browser-local preference (`recorderLanguage` in local storage), separate from YAML and ROS parameters. English and Simplified Chinese are available in the top-bar selector. Topic names, robot profile names, paths and machine status/error codes remain unchanged. Dates and message counts follow the selected locale.

## Headless, attached and remote operation

Run the recorder without a browser:

```bash
ros2 run event_recorder recorder --ros-args \
  --params-file src/event_recorder/config/recorder.yaml
```

Attach only the GUI to an existing recorder:

```bash
ros2 run event_recorder web_gui --config /absolute/path/recorder.yaml
# For a namespaced recorder, append: --recorder /robot/event_recorder
```

Avoid duplicate recorder instances with the same name. GUI and recorder need the same ROS domain/RMW configuration; the output filesystem must be accessible to the GUI. Closing the browser or stopping a separately started GUI does not stop the recorder.

Prefer SSH forwarding for remote access:

```bash
ssh -L 8080:127.0.0.1:8080 user@robot-host
```

For direct LAN access, set a token and enter it in the browser:

```bash
export EVENT_RECORDER_GUI_TOKEN='replace-with-your-own-long-random-token'
ros2 launch event_recorder gui.launch.py host:=0.0.0.0
```

LAN mode uses HTTP without TLS; SSH provides encrypted access. All UI assets are local.

## Validation commands

Run from the repository root after building and sourcing ROS and `install/setup.bash`. Use fresh output directories. ROS tests need Humble `rclpy`, `rosbag2_py`, standard message packages and PyYAML. Domains below isolate tests from the default domain.

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
/usr/bin/python3 tests/test_gui_config.py
/usr/bin/python3 tests/test_distance_accumulator.py

ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_rosbag.py --storage mcap

ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_triggers.py --storage mcap \
  --output validation/my_trigger_checks
```

Both storage checks also support `--storage sqlite3`. `validate_rosbag.py` optionally accepts `--real-bag /path/to/imu_bag`, which must contain `/lidar_imu` of type `sensor_msgs/msg/Imu`. Tests exercise actual DDS transport, bag contents and replay. Trigger checks cover topic/context selection, rejection, cooldown, deduplication and invalid configurations.

Measure CPU/RSS using the benchmark (CPU 100% means one logical core, excluding the publisher/player):

```bash
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/benchmark_recorder.py --storage mcap \
  --output validation/my_mcap_benchmark --real-bag /path/to/imu_bag
```

The benchmark samples every 200 ms during idle, buffering and capture, using IMU and synthetic 1 MiB images at 20 Hz. See [historical measurements](../PERFORMANCE.md) for limits; these are not current-version or Orin results.

GUI acceptance needs an installed Chrome browser (optional `--chrome` path). Playwright is a test dependency only:

```bash
/usr/bin/python3 -m pip install --target /tmp/event-recorder-browser-deps playwright==1.48.0
PYTHONPATH=/tmp/event-recorder-browser-deps:$PYTHONPATH \
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_gui.py --output validation/my_gui_checks
```

GUI checks cover service feedback, cooldown, configuration conflicts/restart, authentication, offline state, desktop/mobile layouts and continued recording after browser closure. For real-bag distance tests, see the [Bunker Mini guide](bunker-distance-demo.en.md). Existing evidence is in [VALIDATION.md](../VALIDATION.md) and [reports](../reports/).
