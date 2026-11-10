# Milestone 4：Replication Manager 与 manual recovery

M4 增加了 Replication Manager（RM），并运行 **11 个进程**：RM、GFD、LFD1–LFD3、S1–S3 和 C1–C3。同一套代码可运行要求的两种配置：`mode = "active"` 和 `mode = "passive"`。被终止并手动重新启动的 replica 以 `i_am_ready = 0` 开始，从健康的 replica 获取正确状态，然后重新提供服务。在 passive 模式下，故障的 primary 由选举产生的 backup 替代。

## 部署布局与配置

| 电脑 | 进程 |
| --- | --- |
| A（sacred） | RM、GFD、C1、C2、C3 |
| B | S1、LFD1 |
| C | S2、LFD2 |
| D | S3、LFD3 |

| 文件 | 用途 |
| --- | --- |
| `configs/m4.active.local.toml` / `configs/m4.passive.local.toml` | 单台电脑，使用 loopback 地址 |
| `configs/m4.active.distributed.toml` / `configs/m4.passive.distributed.toml` | 四台电脑。将每个 `advertised_host` 替换为对应电脑当前的 IPv4 地址，并将完全相同的文件复制到四台电脑上 |

每组中的两个文件仅在 `[replication]` 部分有所不同。端口：RM 7300、GFD 7200、client 5201–5203、heartbeat 6201–6203、checkpoint 8201–8203。请在每台电脑的防火墙中放行这些端口。

`[rm] transfer_delay = 2.0` 让 RM 在向重新启动的 replica 发送状态前等待两秒，便于观察其 "logged without replying" 日志行。使用 `--transfer-delay 0` 参数运行 `ft_system.rm.main` 可按正常速度运行。`[replication] checkpoint_freq` 设置 passive checkpoint 的周期，`--checkpoint-freq` 参数可在运行 `ft_system.server.main` 时覆盖该设置。

## 命令

从项目根目录开始，在各自独立的 terminal 中运行每条命令。passive 演示使用 `configs/m4.passive.distributed.toml`。

```bash
# Computer A, in this order
PYTHONPATH=src python -m ft_system.rm.main  --config configs/m4.active.distributed.toml
PYTHONPATH=src python -m ft_system.gfd.main --config configs/m4.active.distributed.toml

# Computers B, C, D: the LFD first, then the replica (wait for each GFD/RM update before the next replica)
PYTHONPATH=src python -m ft_system.lfd.main    --config configs/m4.active.distributed.toml --lfd-id LFD1
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S1

# Computer A, after "RM: 3 members: S1, S2, S3"
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C1 --interval 1
```

要手动恢复一个 replica，请在它所在的电脑上再次运行同一条 `ft_system.server.main` 命令。保持 LFD 运行。

## 演示：active replication 的 manual recovery

1. 启动 RM。它会输出 `RM: 0 members`。
2. 启动 GFD。它会输出 `GFD: 0 members`，向 RM 注册（`GFD: notified RM: 0 members`），随后 RM 再次输出 `RM: 0 members`。
3. 启动 LFD1–LFD3。GFD 会输出 `GFD: registered LFD1` 等信息；GFD↔LFD 之间开始进行 heartbeat。
4. 启动 S1。它会输出 `S1: i_am_ready = 0 (waiting for the RM)`。LFD1 的第一次 heartbeat 之后，依次出现 `LFD1: add replica S1`、`GFD: 1 member: S1` 和 `RM: 1 member: S1`。RM 发送 `S1 is the first replica; asking it to set i_am_ready = 1`，S1 输出 `i_am_ready = 1 (set by the RM)`。
5. 先启动 S2，再启动 S3。GFD 和 RM 显示的成员数先变为 2，再变为 3。RM 会输出 `asking S1 to transfer its state to S2`。S1 输出 `secondary channel to S2 open` 和 `S1 sending checkpoint <my_state = ..., checkpoint_count = N> to S2`。S2 输出收到的 checkpoint 和 `i_am_ready = 1 (checkpoint installed)`。这就是 rubric 第 9 步中的 secondary channel。
6. 启动 C1–C3。每个 client 都会输出 RM 的 view，将每个请求发送给全部三个 replica，并为每个请求丢弃两个重复回复。
7. 在 S2 中按 Ctrl-C。LFD2 报告 `S2 has died` 和 `delete replica S2`。GFD 和 RM 显示 `2 members: S1, S3`。各 client 继续运行，每个请求对应一个重复回复。
8. 重新启动 S2。LFD2 输出 `S2 has recovered` 和 `add replica S2`；GFD 和 RM 再次显示 3 个成员。各 client 输出 `reconnected to S2`。
9. S2 等待状态期间（约为 `transfer_delay`），它会先输出 `Received <C1, S2, n, request>`，再输出 `S2: i_am_ready = 0; logged <C1, S2, n, request> without replying (high_watermark_request_num[C1] = n)`。各 client 不会收到 S2 的回复。
10. S1 发送其 checkpoint。S2 输出 `i_am_ready = 1 (checkpoint installed)` 和 `replaying K logged requests`，然后重新回复请求。各 client 恢复为每个请求收到两个重复回复。任何 client 都不应输出 `replica state divergence`。

## 演示：passive replication 的 manual recovery

使用 passive 配置和相同的启动顺序。RM 输出 `RM: primary is S1`；S1 输出 `role = primary`；S2 和 S3 输出 `role = backup (primary is S1)`，并在收到第一个 checkpoint 时变为 ready。client 收到的每条回复都来自 S1，S1 向 S2 和 S3 发送 `checkpoint <my_state, checkpoint_count>`，发送间隔为 `checkpoint_freq`。

**Backup 故障与恢复。** 在 S2 中按 Ctrl-C。Membership 减少为 `S1, S3`，S1 继续回复请求并向 S3 发送 checkpoint。重新启动 S2。它以 backup 身份重新加入，初始状态为 `i_am_ready = 0`，并在收到 S1 的下一个 checkpoint 时变为 ready。

**Primary 故障与重新选举。** 在 S1 中按 Ctrl-C。检测到故障后，RM 输出 `primary S1 failed; new primary is S2`。新的 primary 是最早加入且已 ready 的 backup；如果 S2 刚刚重新启动，则改由 S3 当选。新的 primary 输出 `role = primary`，并从最后收到的编号继续递增 `checkpoint_count`。各 client 输出 `primary changed S1 -> S2`，并使用相同的 `request_num` 重新发送正在进行的请求。重新启动 S1。它作为新 primary 的 backup 返回，并在收到该 primary 的 checkpoint 时变为 ready。

## 工作原理

- **RM view。** RM 持有 `{view_num, mode, members, ready, primary}`。Membership 仅来自 GFD。Readiness 来自每个 replica 自身的报告。RM 将每个新 view 推送给 replica 和 client，它们各自保持一条与 RM 的 TCP 连接。client 从 RM 而非 GFD 获取 membership，因为只有 RM 知道 passive primary。课程指南允许采用其他设计，只要保留要求展示的行为即可。
- **i_am_ready。** 使用 RM 时，每个 replica 启动时都尚未 ready。同类中的第一个 replica 会被告知直接以当前状态变为 ready。在 active 模式下，后续的每个 replica 都从最早 ready 的 replica 获取 checkpoint，由 RM 的 `transfer_state` 命令触发发送。在 passive 模式下，backup 在收到 primary 的第一个周期性 checkpoint 时变为 ready。
- **不丢失请求的 catch-up。** checkpoint 携带 `my_state`、`checkpoint_count`，以及每个 client 最后一个已应用请求的 `request_num`。replica 恢复状态，重建 duplicate-reply cache，并按照到达顺序 replay 已记录的请求。它会跳过状态中已经包含的请求，从 cache 回复边界请求，并应用更新的请求。每个 client 的 counter 只会因该 client 自己的请求而改变，且每个 client 的请求按顺序到达，因此其状态会与 donor 完全一致。
- **晚到请求与重连。** active replica 还会丢弃在 checkpoint 安装后才到达、但已经应用过的 increment，保持 client 连接打开。即使 view 没有变化，active client 也会在每次连接尝试完成后间隔 0.5 秒，重试与当前 RM 成员之间缺失的连接。重试期间，对已连接 replica 的请求会继续进行。
- **Gap detection。** 如果 active replica 发现 `request_num` 跳号（它漏掉了其他 replica 已应用的请求），就会设置 `i_am_ready = 0` 并重新请求状态。如果 checkpoint 比 replica 日志的起始位置还旧，则会拒绝它并重试。这处理了 client 重连与获取 snapshot 之间的 race。
- **Election。** 新的 primary 是最早加入且已 ready 的成员；不会将任何 replica 硬编码为 primary。重新启动的 replica 始终以 backup 身份返回。

## 取舍与限制（用于 Q&A）

- **Warm-passive 数据丢失窗口。** 旧 primary 在最后一次 checkpoint 之后应用的请求会在 failover 时丢失。该窗口受 `checkpoint_freq` 限制，新的 primary 会输出丢失了哪些请求编号。client 可能观察到其 counter 回退了相应的请求数量。这是采用周期性 checkpoint 而非逐请求 checkpoint 的代价。
- **RM 是 readiness 的权威来源。** 如果 RM 不可用，新的 replica 就无法变为 ready。RM、GFD 和各 LFD 都是允许存在的 single points of failure。组件会重新连接到重启后的 RM，但运行期间重启 RM 不属于演示内容。
- **Catch-up 依赖于应用。** 它依赖每个 client 的独立状态和每条连接上的 FIFO 顺序。通用应用需要跨 client 的 total order 和完整的 message log。
- **Membership 与 readiness。** `RM: N members` 统计存活的 replica，与 GFD 一致。重新启动的 replica 在变为 ready 前，会先作为成员存在约 `transfer_delay` 的时间；view 会同时列出这两种状态。
- **仅支持 manual recovery。** M5 才会加入由 RM 或 LFD 自动重新启动 replica 的功能。

## 验证

```bash
python3 -m pytest -q
python3 scripts/m4_smoke.py              # both modes; or --mode active / --mode passive
```

`m4_smoke.py` 在 loopback 上使用较短的定时参数运行包含 11 个进程的 topology。它会在两种模式下终止并重新启动 S2，在两种模式下终止 S1（passive 模式下会验证 primary election），并在 passive 模式下重新启动 S1。它检查 RM、replica 和 client 的日志，并将日志保存到 `logs/m4-smoke/<mode>/`。它使用 hard kill，因此也能在 Windows 上运行。pytest suite 包含这两种场景的进程内版本（`tests/integration/test_m4_system.py`）。

Milestone 1–3 的配置和演示保持不变。不包含 `[rm]` 的配置会保留之前的行为：replica 启动时即为 ready，passive primary 由 `primary_id` 固定指定。
