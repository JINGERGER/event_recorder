# 验证结果

> 下方原有记录对应 v0.1。v0.2 已将触发合并改为命名 trigger、忙时拒绝及窗口结束后 3 秒冷却；当前版本结果见本文末尾。

2026-11-25，在本机 Ubuntu 22.04 / ROS 2 Humble 上完成 C++ 构建及真实 DDS/rosbag2 验证。最后一次构建无编译警告，完整验收脚本退出码为 0，13 个场景通过。

原始报告：[report.json](reports/sqlite3_acceptance.json)。该目录保留每个场景的 bag、事件元数据、节点日志及 `ros2 bag info` 输出。

| 场景 | 结果 |
| --- | --- |
| 前 5 秒 / 后 10 秒窗口 | 2637 条序号消息，连续、无重复，首末接收时间在窗口内且接近边界 |
| `ros2 bag play` 实际回放 | 2637 条消息逐条一致，恢复 sensor_a / sensor_b 两个静态 TF |
| 连续触发及 ID 去重 | 保存两次有效触发，重复 ID 被忽略，截止时间延长到末次触发后 0.6 秒 |
| 触发后无数据 | 定时结束并关闭 bag |
| 启动即触发 | PARTIAL，标记 insufficient_prehistory |
| 窗口结束后再次触发 | 两个不同事件，分别核对计数和连续序号 |
| 历史/写盘容量不足 | PARTIAL，明确报告历史不足和写盘队列丢弃 |
| 磁盘剩余空间不足 | FAILED，不宣称成功 |
| 正常退出 | 排空已接收消息，标记 shutdown_truncated |
| 最大事件时长 | 按上限停止，标记 max_event_duration_truncated |
| 输入队列容量不足 | 检测到 1 条丢弃，事件标记 PARTIAL |
| 仿真 ROS 时间回跳 100s → 50s | 标记 ros_clock_jump，单调时钟后置窗口仍为 0.6 秒 |
| 已有 IMU bag 回放并重录 | 3039 条 IMU，header 时间戳唯一，逐条匹配源 bag 中的连续片段 |

真实输入为 `/home/nezha/Documents/rosbag2_2025_07_29-18_14_13`，仅通过 `ros2 bag play --topics /lidar_imu` 播放，不修改源文件。

完整窗口和真实输入测试使用前 5 秒 / 后 10 秒；其他故障场景使用缩短窗口加快验收。ROS 时间回跳时 rosbag2 按存储时间戳读取可能重排消息，该场景检查异常标记、窗口持续时间及无重复，不要求 bag 读出顺序保持接收顺序。

## 验证边界

- SQLite3 与 MCAP 均已验证；MCAP 补充结果见下方。
- 队列过载使用小容量确定性触发，磁盘故障通过剩余空间阈值触发；未模拟物理磁盘 I/O 错误、磁盘持续降速或进程崩溃恢复。
- 跨事件隔离通过连续两个事件验证，但未确定性强制第二次触发恰好发生在前一个 Writer 关闭期间。
- 未对生产相机/点云的持续高吞吐、长期 RSS、真实网络丢包开展负载测试。需要根据目标 topic 及设备预算进一步测量。
- 可选的采集时间模式和独立重叠事件模式不在本次实现范围内。

## MCAP 补充验证（2026-11-25）

安装并确认 `ros-humble-rosbag2-storage-mcap` 版本 0.15.17，用 `--storage mcap` 完整重跑上述 13 个场景，全部通过，脚本退出码 0。默认窗口录入 2697 条序号消息，实际回放逐条一致且恢复两组静态 TF；真实源 bag 重录 3039 条 IMU，匹配源数据连续片段。

原始报告：[MCAP report.json](reports/mcap_acceptance.json)。容量不足场景产生的空 bag 会提示没有 message indices，这是预期的空数据故障测试结果。示例配置现默认使用 MCAP。

另外完成 SQLite3 / MCAP 的 IMU 和 20MiB/s 合成图像负载对比，见 [PERFORMANCE.md](PERFORMANCE.md)。这项短时合成测试不替代生产数据持续负载验证。

## v0.2：Trigger list 与单事件冷却

2026-11-25，构建无编译警告。MCAP 和 SQLite3 各通过 4 组专项验证，MCAP 完整回归 13 个场景通过，所有脚本退出码为 0。

- 两条命名规则分别选择不同 topic，核对 bag 注册的 topic 和实际消息均无混入；静态 TF 仅在选中它的规则中出现。
- 窗口内相同/不同规则的请求均拒绝，截止时间不延长。
- 使用默认完整 3 秒冷却，验证冷却期及结束前约 200ms 的请求被拒绝；结束后发送新请求成功，之前被拒绝的请求没有被延迟执行。
- 同规则相同 ID 在冷却后仍去重；不同规则可使用相同 ID，忙时被拒绝的 ID 不会污染去重缓存。
- 未被选中的 topic 输入溢出，不会将当前事件标记为不完整。
- 不存在的 topic 与重复规则名在启动时被拒绝。
- 回归测试按新语义将“合并触发”替换为“忙时拒绝”、将“最大时长延长截断”替换为“不延长窗口”。2692 条序号消息实际回放逐条一致，两组静态 TF 恢复成功；3039 条真实 IMU 匹配源 bag 连续片段。

报告：[MCAP 专项](reports/v02_triggers_mcap.json)、[SQLite3 专项](reports/v02_triggers_sqlite3.json)、[MCAP 回归](reports/v02_mcap_acceptance.json)。

当前实现也会在旧 Writer 尚未完成时拒绝新 trigger；本轮未用慢磁盘强制制造“超过 3 秒仍在 FINALIZING”的场景。完整 SQLite3 旧场景回归和 v0.2 性能复测不包含在本轮，SQLite3 本轮验证范围为上述 trigger 专项。

## v0.3：轻量 Web GUI 与真实触发反馈

2026-11-25 完成构建及验收，无编译警告：

- 配置单元测试 3 组通过（包含无效时间、NaN、预算、topic/类型、QoS、规则及原子保存/版本冲突子场景）。
- 真实 ROS + HTTP + Chrome 浏览器测试 9 组通过：启动与实时状态、无效请求拒绝、实际触发反馈/忙时拒绝/冷却、配置保存/待重启/冲突、桌面与 390px 手机布局、关闭浏览器仍完成录制、保存后重启生效、密钥访问、离线检测与禁用触发。浏览器无 JavaScript 错误。
- GUI 验收包含名称为 `on` 的规则，确认 YAML 1.1 读取时仍保留字符串类型。保存配置自动省略 Humble 不能推断类型的可选空数组。
- MCAP / SQLite3 各 4 组 trigger 专项回归通过。
- MCAP 13 项完整回归通过：2703 条序号消息回放逐条一致并恢复静态 TF；3039 条 IMU 匹配原 bag 连续片段。

报告：[GUI 端到端](reports/v03_gui.json)、[MCAP trigger 专项](reports/v03_triggers_mcap.json)、[SQLite3 trigger 专项](reports/v03_triggers_sqlite3.json)、[MCAP 完整回归](reports/v03_mcap_acceptance.json)。截图见 [控制台](docs/gui-overview.png)，本机 `validation/gui_v03_final/` 还保存了配置页和手机截图及日志。

测试设备为本机 Ubuntu 22.04 / ROS 2 Humble，浏览器为本机 Google Chrome，Playwright 1.48 为临时测试依赖。尚未在 AGX Orin 上实测，不包含 TLS、持续高并发 HTTP 或当前版本性能复测。GUI 不提供运行中热更新、自动重启、删除 bag 或停止正在进行的录制。

## 2026-11-26：按机器人类型命名

默认配置改为 go2 / bunker_mini / piper，默认类型 go2，GUI 文案改为机器人类型。配置通过校验，示例使用的标准消息类型在 Humble 环境中可加载。构建通过，并在独立 ROS domain 88 启动默认配置验证实时状态和三种类型的按钮、配置页名称，Chrome 无页面错误；更新了控制台截图。此次验证未触发录制，未重复执行此前的完整 rosbag 回归。Bunker Mini/Piper 的 topic 清单是待实机确认的示例，不代表已完成对应机器人驱动适配。

## 2026-11-26：Bunker Mini 累计距离触发样例

源 bag：`rosbag2_2025_12_24-21_12_52`。独立读取全部 14,473 条 `/odom`，参考累计路径 32.6210836m，最大起点位移 10.8091630m。首次累计超过 20m 出现在第 10,345 条有效里程计，距首条约 206.759858s，距离 20.001397104m。

- 距离计算单元测试 5 组通过，覆盖累计与直线位移区别、严格阈值、单次触发、重置、NaN、重复时间戳、时间回退和坐标系变化。
- 使用完整源 bag 的 5 个选定 topic 做 4 倍速真实 DDS 回放，专用 demo launch 启动 recorder、GUI 和判断节点。仅发布 1 次 `bunker_mini` 请求，时间戳/累计距离/样本数与离线参考完全匹配。
- 事件状态 COMPLETED，前 5 秒/后 10 秒为 recorder 接收时间；4 倍速时约覆盖 60 秒源数据，不等于源时间中的 15 秒。
- 输出 MCAP：`/odom` 3002 条、`/imu/data_raw` 11999 条、`/scan` 601 条、`/tf` 3002 条、静态上下文 8 条。里程计序列及序列化 payload 逐条等于源 bag 的连续片段，包含首次越过阈值的消息。
- 实际调用 reset 服务后，状态回到 ARMED，距离/样本数归零，请求 ID 更新。

原始分析：[里程计统计](reports/bunker_odom_analysis.json)；验证结果：[20m 触发报告](reports/bunker_distance_20m.json)；使用方法：[示例文档](docs/bunker-distance-demo.md)。本轮不包含真实机器人驾驶、图像/点云压力测试或抗定位跳变算法。

## 2026-11-26：Web UI 中英文切换

构建通过，配置单元测试 3 组通过。真实 ROS + HTTP + Chrome 验收 11 组通过，报告见 [中英文 GUI 验收](reports/gui_i18n.json)。

- 首次语言跟随浏览器；中文浏览器中切换英文后刷新仍保持英文。
- 控制台、配置、事件页以及访问密钥弹窗支持两种语言，动态状态、触发反馈、配置校验错误随语言切换。日期与消息计数使用所选语言格式；ROS 名称、路径和机器状态码保持原样。
- 切换保留未保存的参数、新增规则、topic 选择与当前页面，不修改配置文件；插值保留用户值，不作为 HTML 解释。
- 回归覆盖真实触发、忙时/冷却拒绝、配置保存与版本冲突、重启生效、浏览器关闭后完成录制、密钥访问和离线检测。Chrome 无 JavaScript 错误。
- 中英文桌面及 390px 手机布局检查通过；默认 go2 / bunker_mini / piper 的[英文截图](docs/gui-overview.en.png)与[中文截图](docs/gui-overview.png)已更新。

本轮没有修改 recorder 核心，也没有重复完整 rosbag、距离样例和性能回归。原始浏览器截图及日志保存在本机 `validation/gui_i18n_20261126_final/`，默认配置截图运行记录位于 `validation/gui_i18n_20261126_preview/`。

## 2026-11-27：状态配置快照内存池修复

确认 `5262010` 的 `status["configuration"] = configuration_` 存在 yaml-cpp 内存池生命周期问题。改为 `YAML::Clone(configuration_)`，隔离每次状态快照；ROS 接口与配置值不变。

- 本机 x86_64 / yaml-cpp 0.7.0，两个独立进程各执行 5000 次快照更新：直接赋值每 500 次耗时由 0.453 s 增至 20.242 s，RSS 由 3.74 MiB 增至 78.09 MiB；Clone 每批稳定在 0.103–0.109 s，最终 RSS 约 4.02 MiB。两模式序列化配置一致，Clone 的快照修改不会影响原配置。
- `colcon build --packages-select event_recorder` 通过。真实 ROS + HTTP + Chrome 11 组回归通过，包含状态读取、触发/冷却、配置展示/保存/重启、双语切换与离线检测，无 JavaScript 错误。
- 复现针对 YAML 节点生命周期，不是 Orin 或 67 MiB/s 双雷达/ZED 负载测试。本轮未重复完整 rosbag/距离样例回归；整机长跑仍待现场验证。

详见[原因、复现与部署验证说明](docs/status-memory-fix.md)、[内存/耗时报告](reports/yaml_status_memory.json)、[修复后 GUI 回归](reports/gui_yaml_clone.json)。
