# Milestone 5：四机自动恢复演示

[English](milestone-5-instructions.md) · [设计与接口](m5-design.md) ·
[验证证据](m5-verification.md)

M5 在 M4 上增加由 RM 控制的自动进程恢复，支持 active 和 warm-passive 两种模式。
每次注入故障后，系统自动恢复到三个 ready 副本，三个客户端持续发送请求。
演示包含连续三轮故障，其中同一个刚恢复的副本会再次被杀掉。

LFD 主动连接 RM；RM 通过该连接发送 `ensure_replica`，由 LFD 使用本机 Python
解释器和配置文件绝对路径启动本机副本。不需要 SSH、密钥、共享文件系统或远程 shell。
GFD 仍通过 heartbeat 确定 membership，副本安装 checkpoint 后才报告 ready。
进程已经启动，不代表它已具备正确状态。

## 文件与环境准备

| 文件 | 用途 |
| --- | --- |
| `configs/m5.active.local.toml` | 单机 active |
| `configs/m5.passive.local.toml` | 单机 passive |
| `configs/m5.active.distributed.toml` | 四机 active |
| `configs/m5.passive.distributed.toml` | 四机 passive |
| `scripts/m5_smoke.py` | 单机自动故障与恢复验收 |

四台电脑使用同一份 `m5` 代码、Python 3.11 或更新版本。以下命令使用 macOS/Linux
shell；Windows 可使用 WSL 执行相同命令。在每台电脑的仓库根目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

运行系统不需要第三方库；`dev` 用于安装 pytest。各电脑的仓库绝对路径可以不同。
开始前先停止其他 milestone 的演示，因为示例配置复用了原端口。

## 四台电脑的布局

| 电脑 | 示例 IPv4 | 组件 | 需要放行的入站 TCP 端口 |
| --- | --- | --- | --- |
| A（sacred） | `192.168.1.10` | RM、GFD、C1、C2、C3 | 7300、7200 |
| B | `192.168.1.11` | LFD1 及其子进程 S1 | 5201、6201、8201 |
| C | `192.168.1.12` | LFD2 及其子进程 S2 | 5202、6202、8202 |
| D | `192.168.1.13` | LFD3 及其子进程 S3 | 5203、6203、8203 |

修改两个 distributed 配置文件中的**每一个 `advertised_host`**：`[rm]` 和 `[gfd]`
填 A 当前的 IPv4，`[server.S1]` / S2 / S3 分别填 B/C/D 的地址。
`bind_host = "0.0.0.0"` 保持不变。Mac 可用 `ipconfig getifaddr en0` 查询地址
（接口不同时替换为当前接口），Linux 使用 `ip -4 addr`。四台机器必须能互通，
防火墙放行上表端口。将修改后的每个配置文件原样复制到全部四台电脑。

所有启动参数中的路径都指向**执行命令的这台机器**上的文件。启动前可检查配置：

```bash
.venv/bin/python -c 'from ft_system.common.config import load_config; c = load_config("configs/m5.active.distributed.toml"); print(c.recovery); print(c.servers)'
```

系统共有 **11 个业务进程**：RM、GFD、三个 LFD、三个 server 子进程、三个 client。
展示时使用 11 个终端窗口：八个直接运行组件，另外三个用 `tail` 显示 server 日志。
`tail` 只是日志查看器，不算额外的容错组件。

## 启动 active 演示

每条命令在独立终端运行，工作目录均为该电脑的仓库根目录。

在 **A** 上先启动 RM，再启动 GFD：

```bash
.venv/bin/python -m ft_system.rm.main --config configs/m5.active.distributed.toml
.venv/bin/python -m ft_system.gfd.main --config configs/m5.active.distributed.toml
```

确认 `RM: 0 members`、`GFD: 0 members`，以及 GFD 已通知 RM。从 B/C/D 可用
`nc -vz 192.168.1.10 7300` 和 `nc -vz 192.168.1.10 7200` 检查已启动的监听端口，
其中地址替换成 A 的实际地址。

在 **B** 启动 LFD1；收到 RM 指令后，它会自动启动 S1：

```bash
.venv/bin/python -m ft_system.lfd.main --config configs/m5.active.distributed.toml --lfd-id LFD1 --replica-log logs/m5-active/S1.log
```

等到 `RM: S1 is ready (i_am_ready = 1)`。然后依次在 **C**、**D** 启动对应 LFD，
每启动一个，都等其副本 ready 后再继续：

```bash
# C
.venv/bin/python -m ft_system.lfd.main --config configs/m5.active.distributed.toml --lfd-id LFD2 --replica-log logs/m5-active/S2.log
# D
.venv/bin/python -m ft_system.lfd.main --config configs/m5.active.distributed.toml --lfd-id LFD3 --replica-log logs/m5-active/S3.log
```

在 B/C/D 各开一个窗口查看本机 server 日志：

```bash
# B；C 改为 S2，D 改为 S3
tail -n 80 -F logs/m5-active/S1.log
```

等到 RM 最新 view 显示 `members: S1, S2, S3; ready: S1, S2, S3`。
然后在 **A** 的三个独立窗口启动客户端：

```bash
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C1 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C2 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C3 --interval 0.2
```

客户端会无限循环。Active 请求发送给全部副本，每个请求只交付一个回复，其余去重。
副本每次恢复后的输出都追加到同一个文件，原 `tail` 窗口可持续显示。
配置、server 日志、相邻的 `.pid` 和 `.pid.tmp` 文件必须使用不同路径；LFD 会在启动前
拒绝直接重合的路径。

## Active 三轮故障

使用本机 LFD 写入的当前 PID；每次恢复后 PID 都会变化。例如在 **C** 上执行：

```bash
cat logs/m5-active/S2.pid
ps -p "$(cat logs/m5-active/S2.pid)" -o pid=,command=
kill -KILL "$(cat logs/m5-active/S2.pid)"
```

确认 PID 对应 `ft_system.server.main --server-id S2`。只杀 server，保持 LFD 运行。
**在 `tail` 窗口按 Ctrl-C 只会关闭日志查看器，不会杀掉副本。** 不需要再次手动执行
server 启动命令。

| 轮次 | 操作 | 必须观察到的结果 |
| --- | --- | --- |
| 1 | 杀任意副本，例如 S2 | LFD 检测故障，GFD/RM 剩两个成员；客户端继续；RM 指示 LFD2 启动 S2，新 PID 出现，状态同步，恢复三个 ready 副本 |
| 2 | 完全恢复后，再杀同一个 S2，使用新 PID | 再次自动完成整个流程，包括重新传输状态 |
| 3 | 完全恢复后，杀另一个副本，例如 D 上的 S3 | 再次自动恢复，三个客户端持续运行，最终三个副本 ready |

每轮恢复按顺序检查：

1. `S2 has died`、`delete replica S2`，membership 变为 `S1, S3`。
2. `RM: asking LFD2 to ensure replica S2 is running`。
3. `LFD2: launched S2 pid=...`，新的 S2 以 `i_am_ready = 0` 启动。
4. S2 先记录 active 请求、不回复；收到 checkpoint，回放未包含的请求，再变为 ready。
5. RM view 中三个副本均 ready，S2 的重复回复恢复；client 不出现 `replica state divergence`。

必须等到 **ready**，不能仅看到 `RM: 3 members` 就注入下一轮故障。
判断实际状态时，查看 `installed checkpoint` 和 `my_state_S2`。只有
`received checkpoint` 不能证明安装成功：副本已经 ready 时，迟到的 checkpoint
可能被接收后忽略。

## Passive 三轮故障

按后文顺序停止 active 系统。将上述命令中的配置换成
`m5.passive.distributed.toml`，日志目录换成 `logs/m5-passive/`，重新启动。
不要让两种模式同时占用同一组端口。

初始 primary 可以是**任意副本**：先启动目标副本对应的 LFD，等该副本 ready 后再
启动其他两个。例如先启动 C 上的 LFD2，就会由 S2 成为 primary，并未写死 S1。
下表以 S1 为初始 primary，具体操作以 RM 实际 view 为准。

| 轮次 | 操作 | 必须观察到的结果 |
| --- | --- | --- |
| 1 | 在 B 上杀当前 primary S1 | RM 选出 ready 的 backup；client 切换到新 primary；checkpoint 继续；S1 自动恢复为 **backup**，收到新 primary 的 checkpoint 后 ready |
| 2 | 再杀刚恢复的 S1 backup | RM/LFD1 再次将它恢复为 backup，当前 primary 持续回复并发送 checkpoint |
| 3 | 杀另一个 backup，避开当前 primary | 该 backup 自动恢复并接收 checkpoint，三个副本再次 ready |

故障命令同前，PID 文件换成 `logs/m5-passive/<副本>.pid`。核对：

- RM 输出 `primary S1 failed; new primary is ...`，三个 client 均输出 `primary changed`。
- 新 primary 向 backup 发送 checkpoint，并回复三个 client。
- 原 primary 恢复后输出 `role = backup (primary is ...)`，安装 checkpoint，不抢回主角色。
- 每轮 membership 都是 `3 -> 2 -> 3`，随后三个副本都 ready。
- 整个演示保持 client 运行，轮次之间不暂停、不重启 client。

Warm-passive 沿用 M4 的周期 checkpoint 语义：最后一次 checkpoint 之后已经确认的请求
可能在 primary 故障时丢失，counter 因此可能回退。自动恢复不会改变这个持久性取舍。
故障检测、选主和重连期间可能产生响应延迟，应观察持续进展和响应间隔，不应宣称零延迟。

## 配置与关闭

```toml
[recovery]
enabled = true
restart_delay = 1.0
restart_retry = 1.0
```

`restart_delay` 是 RM 发现符合恢复条件的缺失副本后，等待启动的秒数；`restart_retry`
是重试周期，直到 GFD 再次发现该副本。重复指令不会重复创建尚存活的子进程。
如果已有手动启动的副本响应 heartbeat，LFD 会保留它，不重复启动。启动失败会记录
错误并重试；尚存活但卡住的进程不会被再启动一个副本替代。
`rm.transfer_delay` 仍控制 active 的演示状态传输等待，
`replication.checkpoint_freq` 控制 passive checkpoint 周期。
自动恢复必须通过配置启用，现有 M1–M4 配置仍是手动恢复。

关闭顺序：停止 client，然后**先停 RM，再停 LFD**，最后停 GFD。
正常停止 LFD 使用 Ctrl-C，它会终止并回收自己的 server 子进程，删除 PID 文件。
最后关闭 `tail`。正常退出不要对 LFD 使用 `kill -9`，否则无法执行清理。
日志会保留，便于回看。

## 自动测试与排错

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/m5_smoke.py
.venv/bin/python scripts/m5_smoke.py --mode passive --first S2
.venv/bin/python scripts/m5_smoke.py --demo-timing
```

Smoke 自动分配 loopback 端口，启动 11 个业务进程并实际杀掉副本。只有 LFD 可以
启动 server 子进程，测试脚本不会替系统重启副本。默认缩短各计时参数，每个 client
完成 240 个请求。`--demo-timing` 保留演示配置的原有周期和超时，每个 client 完成
1200 个请求，以覆盖更长的恢复过程。检查 membership、readiness、主备角色、checkpoint、
请求顺序、active 一致性和退出清理。
配置、各进程日志和各模式的 `result.json` 写入 `logs/m5-smoke/` 下新的时间戳目录。
pytest 还会分别让 S1/S2/S3 先启动，覆盖两种模式。
这些是本机测试；四机网络仍需实际执行上面的流程，并保留终端日志作为现场证据。

| 现象 | 检查内容 |
| --- | --- |
| LFD 已注册但 server 没启动 | RM/GFD 是否运行、配置是否一致、`recovery.enabled`、RM 是否收到 GFD membership 及 LFD 注册 |
| 重复出现 `launch ... failed` 或子进程迅速退出 | LFD 诊断、server 日志、解释器与安装、配置路径、日志目录权限、端口占用 |
| RM 显示 `external` | 已有手动启动的 server 响应，应先停止旧演示，再开始由 LFD 管理的 M5 演示 |
| 成员数为三但 ready 不足三个 | checkpoint 端口连通性、donor/primary 日志、`i_am_ready`；先等状态同步完成 |
| Client 连不上副本 | advertised 地址必须是真实 LAN 地址，不能用 `0.0.0.0`/loopback；检查防火墙及网络隔离 |
| 杀进程后短暂没有 PID 文件 | 旧进程已回收，新进程尚未启动；等待新 PID 和 ready view |

本次 M5 rubric 验证的是 RM/GFD/LFD 保持运行时的连续副本进程崩溃。整机/LFD 崩溃、
网络分区、全部副本状态丢失，以及通过 RM 在运行时修改配置，不属于本次实现范围。
本分支已按用户选择暂缓运行时配置功能。
