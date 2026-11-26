# 参考文档

[English](reference.md) | **简体中文** · [返回首页](../README.zh-CN.md)

## 配置

以 [recorder.yaml](../src/event_recorder/config/recorder.yaml) 为起点。GUI launch 在工作目录中创建尚不存在的 `recorder.local.yaml`；距离示例使用独立的 `bunker_distance_demo.local.yaml`。相对输出路径以进程工作目录为基准。配置在启动时读取，保存后需要重启。

以下参数位于 `event_recorder.ros__parameters`，默认值取自示例配置。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `pre_trigger_seconds` | 5.0 | 触发前保留的秒数。 |
| `post_trigger_seconds` | 10.0 | 触发后录制的秒数。 |
| `history_retention_seconds` | 8.0 | 历史保留秒数，同时受容量限制。 |
| `max_event_seconds` | 300.0 | 前置与后置时长之和的上限。 |
| `trigger_cooldown_seconds` | 3.0 | 从目标窗口结束时刻计算的冷却时间。 |
| `dedup_seconds` | 60.0 | 已接受请求 ID 的去重时间。 |
| `history_bytes` | 268435456 | 历史预算 256 MiB。 |
| `input_bytes` | 67108864 | 输入队列预算 64 MiB。 |
| `writer_bytes` | 536870912 | 写盘队列预算 512 MiB。 |
| `context_bytes` | 16777216 | 上下文预算 16 MiB。 |
| `queue_items` | 100000 | 与字节预算共同生效的条数限制。 |
| `max_pending_events` | 8 | 兼容参数，不会开启并发事件。 |
| `minimum_free_bytes` | 1073741824 | 最低磁盘剩余空间 1 GiB。 |
| `output_directory` | `event_bags` | bag 和事件元数据的输出目录。 |
| `storage_id` | `mcap` | 支持 `mcap`、`sqlite3`；不加载配置直接启动节点时默认 SQLite3。 |

### Topic 与机器人配置

`topics`、`types` 注册普通 topic 及其完整消息类型。可选的 `reliability`、`durability` 数组与其逐项对应，省略时默认 `best_effort`、`volatile`。QoS 需与发布者兼容。`context_topics`、`context_types` 注册上下文订阅，采用 reliable/transient-local QoS。

所有已注册 topic 持续订阅，共享历史缓存。`trigger_list` 定义配置名称，`triggers.<名称>.topics` 决定每次事件写入哪些数据；上下文 topic 也需显式选择。名称区分大小写，符合 `[A-Za-z][A-Za-z0-9_]*`。`default_trigger` 指定通用触发服务使用的配置。未注册 topic、重复名称、空 topic 清单均为无效配置。

以下是配置片段，需与示例 YAML 中的 topic 注册配置组合使用：

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

未配置 `trigger_list` 时，旧配置隐式创建选择所有已注册 topic 的 `default` 规则；topic 输入继续支持旧格式 `请求ID|原因` 或匿名原因，但仍遵循当前的单事件与冷却策略。

## 录制语义与限制

- 窗口按单调接收时间划分，bag 时间戳使用 ROS 接收时间，不按传感器消息头划窗。`use_sim_time` 影响 ROS 时间戳，不改变窗口时长。ROS 与单调时间增量相差超过 100 ms 时标记 `ros_clock_jump`。
- 全局只允许一个正在录制或完成写盘的事件。录制中、冷却期或写盘未完成时拒绝新请求，不合并、不延长、不延迟执行。冷却从目标窗口结束时刻计算，且写盘完成后才能接受下次触发。
- 按 `(机器人类型, 请求ID)` 去重，只有已接受请求进入缓存；空 ID 不去重，不同类型可复用同一 ID。
- 输入/写盘过载丢弃新消息，历史容量不足淘汰最旧消息，已知受影响事件标记不完整。未选中 topic 的丢弃不会直接将当前事件标为不完整，但共享容量仍影响保留时间。时间水位判断偏保守；消息数为零本身不代表丢包。
- 字节预算计入 payload capacity 和固定消息开销，**不是 RSS 硬上限**。payload 共享引用；DDS、内存分配、处理中消息、反序列化与存储插件还会占用内存。上下文快照可临时增加约一个上下文预算。进程崩溃会丢失内存历史。
- `/tf_static` 按 child frame 聚合，保留窗口前基线和窗口内更新。其他上下文 topic 以整条消息替换完整状态基线，不聚合多个发布者的增量状态。
- 上下文使用原始 ROS 接收时间注入，因此 bag 总时长可能超过录制窗口；检查窗口时应核对普通消息和事件元数据。
- `COMPLETED` 表示写盘完成且未检测到本地不完整，`PARTIAL` 表示已知缺失/截断，`FAILED` 表示失败。磁盘低于阈值或写盘异常会导致失败。正常退出排空已接收任务，未结束窗口标记截断。存储不可写时元数据也可能无法保存。崩溃后遗留的 `CAPTURING` 不表示完成。
- 完整性只覆盖本地可观测的丢失，不能保证接收前未丢包；端到端验证需要发布端序号。目前不支持独立重叠事件、传感器时间窗口和崩溃前历史持久化。

## ROS 与 HTTP 接口

以下 ROS 名称使用默认节点命名空间。

| 接口 | 类型 | 行为 |
| --- | --- | --- |
| Topic `/event_recorder/trigger` | `std_msgs/msg/String` | `data: '机器人类型\|请求ID\|原因'`；只发送类型名称为匿名请求。发布不代表接受。 |
| Service `/event_recorder/triggers/<机器人类型>` | `std_srvs/srv/Trigger` | 触发指定配置。 |
| Service `/event_recorder/trigger` | `std_srvs/srv/Trigger` | 触发 `default_trigger`。 |
| Topic `/event_recorder/status` | `std_msgs/msg/String` | 5 Hz YAML 快照，reliable/transient-local；含运行配置、规则、缓存、队列、录制/冷却和最近事件。 |

触发服务的 `success` 表示实际接受结果，`message` 是含 `accepted`、`rule`、`event_id`、`reason` 的 YAML。拒绝原因区分忙碌、冷却、写盘未完成和重复 ID 等。超时意味着结果未知，重试前应检查状态。

| HTTP 接口 | 功能 |
| --- | --- |
| `GET /api/state` | 录制器状态。 |
| `GET /api/config` | 已保存配置及 revision。 |
| `GET /api/topics` | ROS graph topic 发现。 |
| `GET /api/events` | 最近 100 个事件。 |
| `POST /api/trigger` | 请求体 `{"rule":"go2"}`，返回触发结果，不自动重试。 |
| `POST /api/config` | 请求体包含 `parameters`、`revision`，仅保存文件。 |

配置保存经过校验并原子替换，保留一份 `.bak`，revision 冲突时拒绝覆盖。GUI 要求至少一个普通 topic，保存时省略可选空数组以兼容 Humble YAML。超过 2 秒未收到新状态时显示离线并禁用触发；重启前按钮使用运行中的配置。

界面语言是浏览器本地偏好（local storage 中的 `recorderLanguage`），不写入 YAML 或 ROS 参数。顶部可切换英文与简体中文。Topic 名称、机器人配置名称、路径以及机器状态/错误码保持原样，日期和消息计数使用所选语言的格式。

## 无界面运行、连接已有节点与远程访问

仅启动录制器：

```bash
ros2 run event_recorder recorder --ros-args \
  --params-file src/event_recorder/config/recorder.yaml
```

只启动 GUI，连接已有录制器：

```bash
ros2 run event_recorder web_gui --config /absolute/path/recorder.yaml
# recorder 使用命名空间时追加：--recorder /robot/event_recorder
```

避免启动同名的重复录制器。GUI 与录制器需使用相同 ROS domain/RMW 配置，输出文件系统需能被 GUI 访问。关闭浏览器或停止单独启动的 GUI 不会停止录制器。

远程访问优先使用 SSH 转发：

```bash
ssh -L 8080:127.0.0.1:8080 user@robot-host
```

直接局域网访问时设置密钥，并在浏览器中输入：

```bash
export EVENT_RECORDER_GUI_TOKEN='replace-with-your-own-long-random-token'
ros2 launch event_recorder gui.launch.py host:=0.0.0.0
```

局域网模式使用 HTTP，不提供 TLS；SSH 可提供加密访问。页面资源均由本地提供。

## 验证命令

构建完成后，在仓库根目录 source ROS 和 `install/setup.bash` 再执行。每次使用新的输出目录。ROS 测试依赖 Humble 的 `rclpy`、`rosbag2_py`、标准消息包与 PyYAML。以下 domain 将测试与默认 domain 隔离。

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

两个存储检查也支持 `--storage sqlite3`。`validate_rosbag.py` 可追加 `--real-bag /path/to/imu_bag`，输入需含 `/lidar_imu`，类型为 `sensor_msgs/msg/Imu`。测试检查真实 DDS 通信、bag 内容和回放；trigger 测试覆盖 topic/上下文选择、拒绝、冷却、去重和无效配置。

测量 CPU/RSS（CPU 100% 表示一个逻辑核，不包括发布器/回放器）：

```bash
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/benchmark_recorder.py --storage mcap \
  --output validation/my_mcap_benchmark --real-bag /path/to/imu_bag
```

基准每 200 ms 采样空闲、缓存和录制阶段，输入为 IMU 和 20 Hz、每帧 1 MiB 的合成图像。[历史性能数据](../PERFORMANCE.md)列出了适用范围，不代表当前版本或 Orin 测量结果。

GUI 验收需要已安装的 Chrome，可用 `--chrome` 指定路径。Playwright 仅为测试依赖：

```bash
/usr/bin/python3 -m pip install --target /tmp/event-recorder-browser-deps playwright==1.48.0
PYTHONPATH=/tmp/event-recorder-browser-deps:$PYTHONPATH \
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_gui.py --output validation/my_gui_checks
```

GUI 检查覆盖服务反馈、冷却、配置冲突与重启生效、认证、离线状态、桌面/手机布局和关闭浏览器后持续录制。实包距离测试见 [Bunker Mini 示例](bunker-distance-demo.md)。已有验证证据见 [VALIDATION.md](../VALIDATION.md) 和 [reports](../reports/)。
