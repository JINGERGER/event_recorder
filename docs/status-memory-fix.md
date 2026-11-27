# Status snapshot memory growth / 状态快照内存增长

## 中文

针对 `5262010` 的问题反馈，本机 Ubuntu 22.04 / ROS 2 Humble、x86_64、yaml-cpp `0.7.0+dfsg-8build1` 已复现以下生命周期问题：

```cpp
// 修复前：将短生命周期状态树与长期配置节点的内存池合并。
status["configuration"] = configuration_;

// 修复后：仅合并独立副本，状态销毁时可以一起释放。
status["configuration"] = YAML::Clone(configuration_);
```

yaml-cpp 的 `Node::operator=(const Node&)` 调用 `AssignNode()`，其中执行 `m_pMemory->merge(*rhs.m_pMemory)`。这不是递归复制配置。`configuration_` 在 recorder 生命周期内一直存在，反复合并使已经不再使用的状态节点仍被共享内存池持有。配置的逻辑内容与输出 YAML 可以一直正确，但底层节点持续累积，后续合并越来越昂贵。简单的 `YAML::Node` 拷贝或 `const` 修饰不能隔离内存池。

`publish_status()` 在消费 `input_` 的管理线程内同步执行。状态生成变慢会阻塞消息处理，输入队列达到字节/条数预算后丢弃消息，也会拒绝触发并返回 `input_queue_full`。磁盘线程此时可能没有任务，因而 `writer_queue_bytes` 为 0 与此问题相符；队列为 0 本身并不是根因证据。状态发布的计时点位于函数开头；如果单次处理超过 200 ms，下一次管理循环可能再次立即发布状态，进一步压缩消费数据的时间。

已经审查同一函数的其他 YAML 赋值：`rule_list`、`topics` 和 `YAML::Load(last_result_)` 都是临时节点，没有同样的长期配置持有关系。OPEN/CLOSE 写盘任务已经使用 `YAML::Clone()` 隔离事件元数据。本次只修改配置快照这一处行为。

每种模式在独立进程内连续构造、序列化并销毁 5000 个代表性状态快照，每 500 次记录一次耗时与 RSS：

| 模式 | 首批 500 次 | 最后 500 次 | 初始 → 最终 RSS |
| --- | --- | --- | --- |
| 直接赋值 | 0.453 s | 20.242 s | 3.74 → 78.09 MiB |
| `YAML::Clone` | 0.105 s | 0.109 s | 3.73 → 4.02 MiB |

Clone 的各批耗时为 0.103–0.109 s，5000 次总计约 1.061 s；直接赋值总计约 99.870 s。两个模式均检查输出配置内容一致；Clone 模式额外检查修改快照不改变原配置。原始数据见 [JSON 报告](../reports/yaml_status_memory.json)。这不是 ROS 发布吞吐基准，也不是完整录制器 RSS 测量。

本机最小复现和代码机制确认了缺陷，但不能据此断言 Orin 的全部丢包均来自这一处问题。双雷达/ZED、约 67 MiB/s 的实机长跑仍需验证。升级需重启 recorder，使旧进程持有的内存释放。建议记录重启后的输入丢弃增量、输入队列、状态到达间隔、CPU/RSS，并在超过原故障出现时长后验证触发与输出 bag。

另一个独立容量约束：若仍使用默认 256 MiB 历史预算，67 MiB/s 下理论上只能容纳约 3.8 秒 payload，尚未计算开销。前置 5 秒约需 335 MiB，完整保留 8 秒约需 536 MiB，再留元数据等余量；本次修复不会自动调整预算，也不保证端到端无丢包。

## English

The report against `5262010` is reproducible with yaml-cpp 0.7.0 on x86_64 Ubuntu 22.04 / ROS 2 Humble. Assigning the persistent `configuration_` node into each temporary status snapshot merges their memory pools. The persistent configuration keeps old status allocations alive even though the logical YAML content stays unchanged. Subsequent merges become progressively more expensive.

The fix attaches `YAML::Clone(configuration_)` instead. This isolates snapshot ownership without changing the status schema or configuration values. Status generation runs synchronously on the input manager thread, so its slowdown can fill the input queue and reject triggers while the independent writer queue stays empty. A zero writer queue alone is not proof of the cause.

In the standalone reproducer, each mode runs in a fresh process for 5000 snapshots. The shared-node mode grows from 0.453 to 20.242 seconds per 500 updates and from 3.74 to 78.09 MiB RSS. The cloned mode stays between 0.103 and 0.109 seconds per batch, ending at 4.02 MiB RSS. The test checks serialized configuration equivalence and clone isolation. See the [raw report](../reports/yaml_status_memory.json).

These accelerated snapshot measurements are not a full-recorder throughput benchmark or an AGX Orin soak test. Rebuild and restart the recorder, then verify queue depth, incremental drops, status cadence, CPU/RSS and successful recording beyond the original failure duration on the affected hardware. Separately, a default 256 MiB history budget cannot retain 5 seconds at 67 MiB/s; budget at least the required payload plus overhead.

## Reproduce / 复现

From the repository root / 在仓库根目录执行（需要 `g++` 与 `libyaml-cpp-dev`）：

```bash
g++ -O2 -std=c++17 tests/reproduce_yaml_status.cpp -lyaml-cpp -o /tmp/event_recorder_yaml_status
/tmp/event_recorder_yaml_status shared src/event_recorder/config/recorder.yaml
/tmp/event_recorder_yaml_status clone src/event_recorder/config/recorder.yaml
```

Each command prints a JSON report. No ROS process or sensor input is needed. The fixture is representative of the lifetime relationship; it does not invoke the complete `publish_status()` function. Timings are diagnostic measurements, not fixed pass/fail limits across hardware.

每条命令输出 JSON，不需要 ROS 进程或传感器输入。该程序复现节点生命周期关系，不调用完整的 `publish_status()`。时间数据用于诊断，不作为跨硬件固定通过阈值。
