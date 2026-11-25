# ROS 2 事件录制器

实现 `ros2_data_recorder_design.md` 的接收时间方案，目标平台为 Ubuntu 22.04 / ROS 2 Humble，C++17。v0.2 按最新需求改为命名 trigger 规则、单事件互斥、窗口结束后 3 秒冷却，取代原设计中的连续触发合并。

持续接收配置的 topic，默认触发前 5 秒、后 10 秒，保留 8 秒历史。使用单调时钟划窗，ROS 接收时间作为 bag 时间戳；不解析传感器时间做窗口判断。通用序列化订阅无需为每种传感器类型重新编译，但运行环境必须安装相应消息类型支持。

## 构建与运行

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select event_recorder
source install/setup.bash
ros2 run event_recorder recorder --ros-args \
  --params-file src/event_recorder/config/recorder.yaml
```

在配置中将 `topics`、`types` 改为目标 topic 及其完整消息类型；`reliability` 和 `durability` 与 topic 数组一一对应，省略时分别为 `best_effort` 和 `volatile`。默认示例注册 `/imu/data`、`/points_raw` 和 `/tf_static`。上下文 topic 使用 reliable/transient-local；普通 topic 可分别配置 QoS。

## 轻量 Web GUI（v0.3）

![Web GUI 控制台（测试数据示例）](docs/gui-overview.png)

无需 Node.js、npm 或数据库。运行依赖为 ROS 2 自带的 rclpy、std_msgs、std_srvs、launch_ros，以及 Python PyYAML。构建后在工作目录执行：

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 launch event_recorder gui.launch.py
```

浏览器打开 **http://127.0.0.1:8080**。首次启动会在当前目录创建可写的 `recorder.local.yaml`（从示例配置复制），recorder 和 GUI 共同使用这个文件。后续启动保留文件内容，不会覆盖已保存配置。也可以指定路径和端口：

```bash
ros2 launch event_recorder gui.launch.py \
  config:=/home/nezha/recorder.local.yaml port:=8080
```

- **录制控制台**：运行状态、后置窗口/冷却倒计时、历史缓存与写盘队列、输入丢弃与触发拒绝计数；每条运行中的规则对应一个触发按钮。按钮等待 recorder 的实际反馈，显示接受的事件 ID 或拒绝原因。
- **规则与配置**：编辑数据 topic、类型与 QoS，或从 ROS graph 发现 topic；管理规则与录制 topic 清单；设置窗口、存储目录、格式及内存预算。保存会校验配置、检测并发修改并保留一份 `.bak`。
- **事件记录**：显示输出目录内最近 100 个事件，含规则、消息数、完整性状态、错误及 bag 路径。没有删除文件或停止录制的操作。

保存配置**不热更新、不重启节点**。页面会提示“待重启”，录制按钮继续使用运行中的规则。完成当前录制后，在启动终端 Ctrl+C，再重新运行同一条 launch 命令使配置生效。不要在录制过程中重启，否则当前窗口会被标记截断。

浏览器关闭不影响录制。Web 服务与 recorder 是独立进程；单独停止 Web 服务也不会停止 recorder。停止整个 launch 则会正常关闭这两个进程。

如已有独立运行的 recorder，只启动 GUI 并指定它的配置文件：

```bash
ros2 run event_recorder web_gui --config /absolute/path/recorder.yaml
# recorder 在其他 namespace 时追加 --recorder /robot/event_recorder
```

不要同时启动两个同名 recorder。GUI 和 recorder 应使用同一 ROS_DOMAIN_ID 和 RMW 配置；查看的事件文件也应在 GUI 主机可访问的输出目录中。GUI 默认和 recorder 运行在同一台设备上。

### 从笔记本访问 Orin

默认只监听设备本机。可以在笔记本建立 SSH 隧道，再访问笔记本的 localhost:8080：

```bash
ssh -L 8080:127.0.0.1:8080 user@orin-ip
```

需要直接监听可信局域网时，设置访问密钥（在页面输入），然后指定 host：

```bash
export EVENT_RECORDER_GUI_TOKEN='替换为你自己的长随机密钥'
ros2 launch event_recorder gui.launch.py host:=0.0.0.0
```

直接局域网模式使用 HTTP，不提供 TLS；需要加密时使用 SSH 隧道。GUI 不依赖 CDN，页面资源全部本地提供。

### ROS / HTTP 接口

- `/event_recorder/status`：`std_msgs/msg/String`，5Hz YAML 状态快照，reliable/transient-local；包含运行配置、规则、缓存/队列、最近完成事件。超过 2 秒未收到新状态时 GUI 显示离线并禁用触发。
- `/event_recorder/triggers/<规则名>`：`std_srvs/srv/Trigger`，返回实际接受结果；通用 `/event_recorder/trigger` 服务使用 default_trigger。原 topic 触发入口保持兼容。
- HTTP `GET /api/state`、`/api/config`、`/api/topics`、`/api/events`；`POST /api/trigger`（`{"rule":"motion"}`）、`/api/config`（parameters 与 revision）。后者保存文件，触发接口不会自行重试。若回应超时，页面会提示结果未知，需检查状态。

## Trigger list

所有数据 topic 的名称、类型及 QoS 在顶层统一注册，持续订阅并共享一份历史缓存；`trigger_list` 管理规则名称，每条规则的 `topics` 决定实际写入 bag 的数据。上下文 topic（例如 `/tf_static`）也需要显式选入规则。

```yaml
event_recorder:
  ros__parameters:
    # topics/types/reliability/durability/context_topics 等仍按示例配置注册
    trigger_cooldown_seconds: 3.0
    trigger_list: [motion, perception]
    default_trigger: motion
    triggers:
      motion:
        topics: [/imu/data, /tf_static]
      perception:
        topics: [/points_raw, /tf_static]
```

统一入口 `/event_recorder/trigger` 使用 `std_msgs/msg/String`，格式为 **`规则名|请求ID|原因`**。规则名只允许字母开头、后续字母/数字/下划线，名称区分大小写。请求 ID 为空时不去重；也可以只发送规则名匿名触发。

```bash
ros2 topic pub --once /event_recorder/trigger std_msgs/msg/String \
  "{data: 'motion|alarm-001|motion anomaly'}"
ros2 topic pub --once /event_recorder/trigger std_msgs/msg/String \
  "{data: 'perception|alarm-002|point cloud anomaly'}"
# 匿名触发指定规则：
ros2 topic pub --once /event_recorder/trigger std_msgs/msg/String "{data: 'motion'}"
# 服务触发配置中的 default_trigger：
ros2 service call /event_recorder/trigger std_srvs/srv/Trigger '{}'
```

全局同时仅允许一个事件，行为如下：

1. 空闲时接受有效规则，录制该规则选中的 topic，窗口仍为接收时间的前 5 秒 / 后 10 秒。
2. 录制窗口内所有新 trigger（包括同一规则）均被拒绝，不合并、不延长窗口。
3. 窗口结束后从目标截止时间开始休眠 3 秒；如果旧事件还未完成写盘，则继续拒绝新 trigger。
4. 冷却结束且写盘完成后，才允许接受下一次 trigger。被拒绝的请求不会等待条件满足后自动执行，发送方需要重新发起。

例如 T=100s 接受 motion，录制窗口为 [95s,110s]，最早在 113s 接受下一次 trigger（若写盘完成）。期间 perception 请求会被拒绝，既不会加入当前 bag，也不会自动创建后续 bag。缓存仍持续更新，因此下一次规则也能获得前置历史。

默认 60 秒内按 `(规则名, 请求ID)` 去重，只有已接受的请求进入去重缓存；不同规则可以使用相同 ID。未知规则、未注册 topic、重复规则名、空 topic 清单等会被拒绝或使启动失败。日志区分 `busy`、`cooldown`、`finalizing`、`duplicate_id` 等原因，事件元数据保存 `trigger_rule`、选中的 topic、冷却截止时间及累计拒绝数量。

v0.3 起，服务返回的 success 表示事件管理器已经实际接受/拒绝本次触发；message 是包含 accepted、rule、event_id 和 reason 的 YAML。服务位于独立回调组，等待结果不会阻塞数据订阅。内部队列用于串行判定，不会保留被拒绝的请求等待重试。时间戳定义为进入排序入口的接收时间。

兼容旧配置：未配置 `trigger_list` 时隐式创建 `default` 规则，选择所有已注册 topic；字符串继续支持旧的 `请求ID|原因` / 匿名原因格式。**旧配置也遵循新的单事件和 3 秒冷却策略，不再合并触发。**

录制结果位于 `event_bags/<event_id>/`，旁边的 `<event_id>.event.yaml` 记录触发、窗口、实际写入数、首末接收时间、上下文注入、配置和错误。`COMPLETED` 表示关闭成功且没有检测到本地缺失；`PARTIAL` 表示已写盘但已知不完整；`FAILED` 表示存储失败。进程崩溃遗留的 `CAPTURING` 文件不能视为完成。

```bash
ros2 bag info event_bags/<event_id>
ros2 bag play event_bags/<event_id>
```

本机已安装 `ros-humble-rosbag2-storage-mcap`（0.15.17）。示例配置默认 `storage_id: mcap`，也可改为 `sqlite3`；不加载配置文件直接启动节点时仍默认 SQLite3。安装命令：`sudo apt-get install ros-humble-rosbag2-storage-mcap`。

## 行为与资源限制

- 输入入口在同一互斥区内采样时间、分配序号、入队；消息、触发、结束检查由一个管理线程串行处理，Writer 由另一个线程独占。
- 历史、输入、写盘、上下文缓存均有字节和条数限制；共享不可变 payload，Writer 使用保持原始所有权的别名引用。写盘控制操作不丢弃，全局最多一个活动或写盘中的事件；保留 `max_pending_events` 参数兼容旧配置，但它不会开启并发事件。
- 输入/写盘过载丢弃新消息，历史容量不足淘汰最旧消息；受影响事件标记不完整。触发队列或待完成事件容量耗尽时拒绝触发并记录日志和计数。消息数为零不自动判定丢包。
- 默认 `max_event_seconds` 为 300 秒，启动时要求前置加后置时长不超过此值。新策略不再延长窗口，因此不需要连续触发截断或分段。
- `/tf_static` 按 child frame 保存静态变换，保留窗口起点之前的基线和窗口内更新。其他上下文 topic 采用整条消息替换基线，适用于单一完整状态，不能代替多发布者增量状态的专用聚合器。
- 上下文采用原始接收 ROS 时间注入，并记录原始时间及注入标记，因此含上下文的 bag 总时长可能超过目标窗口。窗口完整性要检查普通消息和元数据。
- ROS 与单调时间增量相差超过 100ms 时记录 `ros_clock_jump`；窗口仍按单调时间运行。`use_sim_time` 可影响 bag 时间戳，但不改变窗口语义。
- 磁盘剩余空间低于阈值或 Writer 抛出异常时标记 FAILED，正常退出排空已接收数据和写盘任务，未结束窗口标记截断。存储完全不可写时，元数据也可能无法保存，错误会输出到日志。
- 配置在启动时读取，修改参数后应重启。未实现可选的采集时间模式、独立重叠事件和崩溃前历史持久化。

字节预算计入 payload capacity 和固定消息开销，**不等于进程 RSS 硬上限**：DDS 队列、容器分配、单个处理中消息、类型反序列化临时对象和存储插件仍占额外内存。上下文快照还可临时增加约一个上下文预算。请根据真实图像/点云速率测量并调整。

完整性标记只覆盖 recorder 可观测的本地缺失，不保证 DDS 之前未丢包；端到端完整性需要发布端序号。输入/历史丢弃按当前规则选中的 topic 判断，未选中 topic 的丢弃不会直接将当前事件标为 PARTIAL；共享缓存容量仍可能影响其他 topic 的保留时间。时间水位采用保守判断，可能将邻近事件判为 PARTIAL。

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

命名 trigger 专项验证（两种存储均支持）：

```bash
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_triggers.py --storage mcap \
  --output validation/my_trigger_checks
```

专项测试覆盖 topic 隔离、静态上下文选择、忙时拒绝、完整 3 秒冷却及边界前拒绝、不延迟重放、规则内去重、跨规则相同 ID、未选中 topic 丢弃隔离和错误配置拒绝。

### GUI 验证

配置校验单元测试无需浏览器：

```bash
/usr/bin/python3 tests/test_gui_config.py
```

浏览器验收的 Playwright 仅是测试依赖，不是 GUI 运行依赖。使用系统已安装的 Chrome（可通过 `--chrome` 指定）：

```bash
/usr/bin/python3 -m pip install --target /tmp/event-recorder-browser-deps playwright==1.48.0
# 先 source ROS 与 install/setup.bash
PYTHONPATH=/tmp/event-recorder-browser-deps:$PYTHONPATH \
ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1 ROS_LOG_DIR=/tmp/event_recorder_ros_logs \
  /usr/bin/python3 tests/validate_gui.py --output validation/my_gui_checks
```

测试覆盖实际服务反馈、冷却禁用按钮、配置并发修改、重启生效、访问密钥、离线检测、桌面/手机布局和关闭浏览器后的持续录制。结果与截图保存在指定目录。GUI 当前要求至少注册一个普通数据 topic；保存时自动省略可选的空数组，以兼容 Humble 的参数 YAML 解析器。
