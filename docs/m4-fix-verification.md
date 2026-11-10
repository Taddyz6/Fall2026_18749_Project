# M4 修复与验证记录

2026-11-09，macOS / Python 3.13.2。`m4-fix` 基于 `m4` 提交
`a610a44f139254d71aa7c6a325014603f95c74eb`。

本次修复 active 恢复期间的晚到请求、客户端重连、RM 初次传输等待和控制消息类型校验。
线协议、配置格式及 passive 的周期 checkpoint 机制保持兼容；没有新增依赖。
本地回归通过，四台实体机器的网络与窗口演示仍待验证。

## 需求与验收证据

| 编号 | 行为与实现 | 测试及实际结果 |
| --- | --- | --- |
| REQ-01 / AC-01 | `server/app.py`：M4 active 丢弃已应用的旧 increment，不重复修改状态、不返回致命错误；边界请求仍返回缓存回复，错误 operation 仍被拒绝 | TC-01：`test_active_recovery_keeps_connection_when_checkpoint_overtakes_requests` 让 checkpoint 15 先于请求 11–15 到达；原连接保持，后续三个副本 C1 都达到 20。listener 单元测试同时验证旧模式行为和非法操作。通过 |
| REQ-02 / AC-02 | `client/active.py`：收到 RM view 后后台重试当前成员，无需等待新 view；成员移除或客户端关闭时不接纳未完成连接 | TC-02：`test_active_retries_current_member_without_new_view` 覆盖初次失败、运行中断连；其他副本继续处理五个请求，连接恢复前 view 不变，随后三个副本状态都到 6。`test_active_cancels_unneeded_connection_attempts` 覆盖移除和关闭。四个参数化用例通过 |
| REQ-03 / AC-03 | `rm/app.py`：注册只加速已经尝试过的任务，保留初次 `transfer_delay` | TC-03：`test_registering_replica_preserves_initial_transfer_delay` 强制成员报告早于副本注册；配置 0.2 秒，实际初次传输不早于该期限。通过 |
| REQ-04 / AC-04 | `common/protocol.py`：先校验 `kind`、`mode`、`command` 为字符串，再判断合法枚举值 | TC-04：`test_rm_control_fields_reject_non_strings_as_protocol_errors` 从 JSON 解码入口测试 null、布尔、数字、数组、对象，共 15 个用例，均产生 `ProtocolError`。通过 |
| REQ-05 / AC-05 | README、M4 说明和 smoke 注释统一为 11 个进程，补充晚到请求及后台重连行为 | TC-05：差异审阅与 `git diff --check` 通过 |

## 验证结果

最初的定向测试在修复前得到 **11 failed, 47 passed**，包括原审查中的断连、一致性、传输延迟和类型错误。
延迟测试当时配置 200ms，实际约 13ms 即开始传输。之后增加了两个连接清理用例。
修复后定向测试 **60 passed**；全量从原来的 150 个用例增加到 **174 passed**。

| 验证 | 命令或方式 | 结果 |
| --- | --- | --- |
| 定向回归 | `PYTHONPATH=src .venv/bin/python -m pytest -q tests/unit/test_client_listener.py tests/unit/test_protocol.py tests/integration/test_m4_system.py --tb=short` | 60 passed |
| 全量回归 | `PYTHONPATH=src .venv/bin/python -m pytest -q --tb=short` | 174 passed，6.46s |
| M1 smoke | `.venv/bin/python scripts/m1_smoke.py` | PASS |
| M2 smoke | `.venv/bin/python scripts/m2_smoke.py --output logs/m4-fix/m2` | PASS；C1–C3 各完成 60 个请求，存活副本状态为 60/60/60 |
| M3 原始 smoke | `.venv/bin/python scripts/m3_smoke.py --output logs/m4-fix/m3` | 启动失败：GFD 等待全部成员超时，原因见下文 |
| M3 本机兼容性 | 临时配置覆盖地址后运行原 smoke 脚本与当前源码 | PASS；checkpoint 及两次 backup 故障后的持续服务通过 |
| M4 smoke | `.venv/bin/python scripts/m4_smoke.py --output logs/m4-fix/m4` | active PASS；passive PASS |
| 差异检查 | `git diff --check` | PASS |

M3 的既有 `configs/m3.local.toml` 使用四机局域网地址，而 smoke 在单机启动所有进程，
导致 LFD 连不上 GFD、S1 连不上 backup。这两个文件与 `m4` 基线一致，是既有配置问题。
兼容性验证在临时目录复制该配置，仅将 `bind_host` / `advertised_host` 改为 `127.0.0.1`，
将临时 `src` 指向当前源码，再把原 `m3_smoke.py` 的 `ROOT` 指向临时目录执行。
原配置和脚本未修改；临时目录已自动清理。结果不能记为原始 M3 命令直接通过。

运行日志保存在被 Git 忽略的 `logs/m4-fix/`：`regression-before.log`、`pytest.log`、
`m1-smoke.log`、`m2-smoke.log`、`m3-smoke.log`、`m3-loopback-smoke.log`、`m4-smoke.log`。
各多进程运行目录保留生成的配置和逐进程日志。

## 限制与评分

- 本次验证使用本机 TCP，含进程崩溃/手动重启及可控的通道延迟；未验证四机防火墙、地址和真实网络时序。
- Passive 最后一次 checkpoint 之后的已确认请求仍可能在主副本故障后丢失；这是原设计的已披露限制，本次未改变。
- 自动拉起进程属于 M5；本次仍采用手动恢复。

工程证据评分 **94/100**，不是课程成绩预测，也不代表四机正式验收通过。

| 维度 | 分数 | 依据与剩余验证 |
| --- | --- | --- |
| 需求与验收覆盖 | 24/25 | 五项修复均有证据；仍需四机现场验收 |
| 功能正确性与边界 | 24/25 | 晚到请求、重试、关闭、延迟及非法输入已覆盖；真实网络故障组合尚未验证 |
| 测试充分性与结果 | 17/20 | 新增 24 个用例、全量和本地 smoke 通过；M3 原命令存在既有配置失败，缺少四机证据 |
| 架构与接口 | 10/10 | 沿用状态锁、RM view 和既有协议，没有新依赖或接口变更 |
| 代码质量与维护性 | 10/10 | 四个生产模块局部修复，异常走既有错误处理，连接取消有回归覆盖 |
| 文档与交付 | 9/10 | 操作说明、计数、失败原因和复现条件已记录；四机演示记录待补 |
