# Bunker Mini：累计行驶超过 20 米自动触发

本示例使用 `rosbag2_2025_12_24-21_12_52` 中的 `/odom`（`nav_msgs/msg/Odometry`），累加相邻有效消息的位置平面增量：

```text
累计行驶距离 += hypot(current.x - previous.x, current.y - previous.y)
```

首次 **严格大于 20 米**时，向 `/event_recorder/trigger` 发布一次：

```text
bunker_mini|odom_distance_<UUID>|<JSON 触发详情>
```

JSON 详情包含阈值、实际累计距离、起点/当前位置、frame、消息时间戳和样本数，可在事件的 `.event.yaml` 的 `triggers[].reason` 中追溯。它是触发原因字符串，不改变 recorder 的 ROS 消息类型。

## 这个 bag 的实际情况

- `/odom` 共 14,473 条，frame 为 `odom`，child frame 为 `base_link`。
- 总累计行驶距离约 **32.6211 米**。
- 距离起点的最大直线位移约 **10.8092 米**，所以不能用“离起点超过 20 米”代替累计行驶。
- 首次超过 20 米发生在第 **10,345** 条有效里程计，距首条里程计约 **206.7599 秒**；累计 **20.001397 米**。

## 启动示例

先完成构建：

```bash
cd /home/nezha/Documents/postman
source /opt/ros/humble/setup.bash
colcon build --packages-select event_recorder
source install/setup.bash
```

终端 1：使用独立 ROS domain，启动 recorder、GUI 和距离判断节点：

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
export ROS_DOMAIN_ID=90 ROS_LOCALHOST_ONLY=1
ros2 launch event_recorder bunker_distance_demo.launch.py port:=8081
```

浏览器打开 **http://127.0.0.1:8081**，等待连接就绪。首次启动会创建 `bunker_distance_demo.local.yaml`，与普通 GUI 的 `recorder.local.yaml` 分开，不修改你的现有配置。

终端 2：在同一个 domain 回放这个 bag：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=90 ROS_LOCALHOST_ONLY=1
ros2 bag play /home/nezha/Documents/rosbag2_2025_12_24-21_12_52 \
  --delay 2 --disable-keyboard-controls \
  --topics /odom /imu/data_raw /scan /tf /tf_static
```

正常 1 倍速下，约 3 分 27 秒触发，随后继续录制 10 秒并关闭文件。GUI 的状态会依次进入录制、写盘/冷却、准备就绪，事件记录中可以看到 `bunker_mini` 的结果。输出默认位于 `bunker_distance_bags/`。

如需快速测试，可在回放命令加入 `--rate 4`，约 52 秒触发。**录制窗口仍是 recorder 接收时间的前 5 秒/后 10 秒**；4 倍速时 15 秒接收窗口大致覆盖 60 秒源 bag 时间。不能将加速回放结果误认为源数据时间中的前 5 秒/后 10 秒。

示例只回放和录制 `/odom`、`/imu/data_raw`、`/scan`、`/tf`、`/tf_static`，这些 topic 都存在于源 bag 中。源 bag 没有 `/cmd_vel`，因此示例不使用普通模板中的 `/cmd_vel`，也不加载图像/点云，以便专注验证触发逻辑。可在 GUI 中扩充录制 topic，但回放命令也应包含对应 topic。

## 单独使用距离判断节点

已有 recorder 且配置中包含 `bunker_mini` 时，也可以只启动判断逻辑：

```bash
ros2 run event_recorder odom_distance_trigger --ros-args \
  -p odom_topic:=/odom \
  -p robot_type:=bunker_mini \
  -p threshold_m:=20.0
```

可配置参数：`odom_topic`、`trigger_topic`、`robot_type`、`threshold_m`、`qos_reliability`。默认 reliable，与此 bag 的发布 QoS 一致；连接 best-effort 实机发布者时，将 `qos_reliability` 配为 `best_effort`。

查看累计距离和判断状态：

```bash
ros2 topic echo /odom_distance_trigger/status std_msgs/msg/String
```

每次启动/重置只发布一次触发，避免超过阈值后每条消息都触发。收到更多消息时仍更新累计距离，便于观察。重新测试时先停止回放，再重置判断节点，然后从头回放：

```bash
ros2 service call /odom_distance_trigger/reset std_srvs/srv/Trigger '{}'
```

重置后的第一条有效消息成为新的起始位置，累计距离归零，并生成新的请求 ID。不要在旧回放仍运行时先重置再跳回开头，否则会检测到源时间回退。

## 边界和状态说明

- 计算 XY 平面行驶长度，不计算 Z；不使用速度积分，也不计算相对起点的直线距离。
- 忽略非有限位置、空 frame 和重复的非零时间戳；frame/child frame 改变或源时间回退后进入 `FAULT`，停止判断，需显式重置。`ros2 bag play --loop` 不会自动重置示例。
- 里程计噪声也会被累加。坐标突然跳变但 frame/时间未改变时，可能导致提前触发；本例没有定位跳变识别、滤波或去抖。真实应用可根据传感器噪声和运动约束扩展。
- topic 发布成功不代表 recorder 已接受。`TRIGGER_PUBLISHED` 仅表示判断节点发出了请求；最终以 recorder 状态和事件元数据为准。忙时/冷却期仍按现有策略拒绝，不自动重试。
- 越过阈值时若未发现触发 topic 的订阅者，则记录 `UNDELIVERED`，不延迟补发；启动 recorder 后重置并重新回放。应先启动示例并等待就绪，再开始回放。
- 这里只验证录制逻辑，不向机器人发布运动控制指令。关闭浏览器不影响判断节点和 recorder；Ctrl+C 停止整个 demo launch 会关闭这三个进程。

## 自动验证

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
/usr/bin/python3 tests/test_distance_accumulator.py
ROS_DOMAIN_ID=90 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_bunker_distance.py \
  --bag /home/nezha/Documents/rosbag2_2025_12_24-21_12_52 \
  --output validation/my_bunker_distance_test --rate 4
```

测试独立读取源 bag 的里程计计算参考阈值，再用 `ros2 bag play` 驱动示例。核对只发布一次请求、首次过阈值的时间戳/累计距离完全一致、recorder 接受的请求 ID/原因相符、输出窗口为 15 秒且完整、MCAP 的里程计 payload 是源 bag 的连续片段，以及重置服务恢复初始状态。源 bag 只读，不修改原文件。

实测结果见 [20 米触发报告](../reports/bunker_distance_20m.json)：一次触发被接受，事件 COMPLETED，录入 3002 条连续里程计，累计距离与离线参考一致。
