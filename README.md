# ROS 2 事件录制器

实现 `ros2_data_recorder_design.md` 的默认接收时间方案，目标平台为 Ubuntu 22.04 / ROS 2 Humble，C++17。

持续接收配置的 topic，默认触发前 5 秒、后 10 秒，保留 8 秒历史。使用单调时钟划窗，ROS 接收时间作为 bag 时间戳；不解析传感器时间做窗口判断。通用序列化订阅无需为每种传感器类型重新编译，但运行环境必须安装相应消息类型支持。

## 构建与运行

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select event_recorder
source install/setup.bash
ros2 run event_recorder recorder --ros-args \
  --params-file src/event_recorder/config/recorder.yaml
```

在配置中将 `topics`、`types` 改为目标 topic 及其完整消息类型；`reliability` 和 `durability` 与 topic 数组一一对应，省略时分别为 `best_effort` 和 `volatile`。默认示例订阅 `/imu/data` 和 `/tf_static`。上下文 topic 使用 reliable/transient-local；普通 topic 可分别配置 QoS。

另一个已 source 的终端触发：

```bash
ros2 topic pub --once /event_recorder/trigger std_msgs/msg/String \
  "{data: 'alarm-001|collision detected'}"
# 或匿名触发（返回值仅表示已进入输入队列）：
ros2 service call /event_recorder/trigger std_srvs/srv/Trigger '{}'
```

字符串格式为 `trigger_id|reason`，没有 `|` 时整个字符串作为匿名原因。默认 60 秒内重复 ID 被忽略；匿名触发不去重。活动事件内合并触发，截止时间延长至最后一次触发后 10 秒。时间戳定义为回调进入排序入口的时间。

录制结果位于 `event_bags/<event_id>/`，旁边的 `<event_id>.event.yaml` 记录触发、窗口、实际写入数、首末接收时间、上下文注入、配置和错误。`COMPLETED` 表示关闭成功且没有检测到本地缺失；`PARTIAL` 表示已写盘但已知不完整；`FAILED` 表示存储失败。进程崩溃遗留的 `CAPTURING` 文件不能视为完成。

```bash
ros2 bag info event_bags/<event_id>
ros2 bag play event_bags/<event_id>
```

本机已安装 `ros-humble-rosbag2-storage-mcap`（0.15.17）。示例配置默认 `storage_id: mcap`，也可改为 `sqlite3`；不加载配置文件直接启动节点时仍默认 SQLite3。安装命令：`sudo apt-get install ros-humble-rosbag2-storage-mcap`。

## 行为与资源限制

- 输入入口在同一互斥区内采样时间、分配序号、入队；消息、触发、结束检查由一个管理线程串行处理，Writer 由另一个线程独占。
- 历史、输入、写盘、上下文缓存均有字节和条数限制；共享不可变 payload，Writer 使用保持原始所有权的别名引用。写盘控制操作不丢弃，待完成事件数量由 `max_pending_events` 限制。
- 输入/写盘过载丢弃新消息，历史容量不足淘汰最旧消息；受影响事件标记不完整。触发队列或待完成事件容量耗尽时拒绝触发并记录日志和计数。消息数为零不自动判定丢包。
- 默认最大事件窗口 300 秒。采用文档允许的**停止并明确标记不完整**策略：超过上限的后置请求被截断，元数据含 `max_event_duration_truncated`，不自动分段。
- `/tf_static` 按 child frame 保存静态变换，保留窗口起点之前的基线和窗口内更新。其他上下文 topic 采用整条消息替换基线，适用于单一完整状态，不能代替多发布者增量状态的专用聚合器。
- 上下文采用原始接收 ROS 时间注入，并记录原始时间及注入标记，因此含上下文的 bag 总时长可能超过目标窗口。窗口完整性要检查普通消息和元数据。
- ROS 与单调时间增量相差超过 100ms 时记录 `ros_clock_jump`；窗口仍按单调时间运行。`use_sim_time` 可影响 bag 时间戳，但不改变窗口语义。
- 磁盘剩余空间低于阈值或 Writer 抛出异常时标记 FAILED，正常退出排空已接收数据和写盘任务，未结束窗口标记截断。存储完全不可写时，元数据也可能无法保存，错误会输出到日志。
- 配置在启动时读取，修改参数后应重启。未实现可选的采集时间模式、独立重叠事件和崩溃前历史持久化。

字节预算计入 payload capacity 和固定消息开销，**不等于进程 RSS 硬上限**：DDS 队列、容器分配、单个处理中消息、类型反序列化临时对象和存储插件仍占额外内存。上下文快照还可临时增加约一个上下文预算。请根据真实图像/点云速率测量并调整。

完整性标记只覆盖 recorder 可观测的本地缺失，不保证 DDS 之前未丢包；端到端完整性需要发布端序号。输入丢弃采用时间水位保守标记，可能将邻近事件判为 PARTIAL。

## 自动验证

依赖 Humble 的 `rclpy`、`rosbag2_py`、`sensor_msgs`、`tf2_msgs` 和 Python PyYAML。使用独立 DDS domain 避免干扰现有系统：

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_rosbag.py --storage mcap \
  --real-bag /home/nezha/Documents/rosbag2_2025_07_29-18_14_13
```

`--real-bag` 可省略；若提供，输入 bag 应包含 `/lidar_imu` (`sensor_msgs/msg/Imu`)。脚本通过真实 DDS 发布递增序号消息，读取 rosbag2 校验窗口、连续性和重复，调用 `ros2 bag info` 和 `ros2 bag play` 校验实际回放。真实 bag 测试回放原始 IMU，并核对录制结果为源数据中的连续片段。每次输出保存在 `validation/<时间>/`，含 bag、事件元数据、日志及 `report.json`。

## 负载测量

`tests/benchmark_recorder.py` 从 `/proc/<pid>/stat` 每 200ms 采样 recorder 进程，分别统计空闲、缓存和录制阶段的 CPU 与 RSS。CPU 的 100% 表示一个逻辑核，不包括发布器/回放器。测试源为约 200Hz IMU 和 reliable QoS 下每帧 1MiB、20Hz 的合成图像；图像录制数量必须接近 15 秒窗口的 300 帧，否则基准失败。

```bash
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/benchmark_recorder.py --storage mcap \
  --output validation/my_mcap_benchmark \
  --real-bag /home/nezha/Documents/rosbag2_2025_07_29-18_14_13
```

性能数据及适用范围见 [PERFORMANCE.md](PERFORMANCE.md)。
