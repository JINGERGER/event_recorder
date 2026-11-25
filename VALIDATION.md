# 验证结果

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
