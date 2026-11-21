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

四台 Mac 使用同一份 `m5` 代码和 Python 3.11 或更新版本，Intel 和 Apple Silicon 可以混用。
每个人在自己的 Mac 上打开终端，进入仓库根目录，创建本机的虚拟环境。

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
| B | `192.168.1.11` | LFD1、S1 | 5201、6201、8201 |
| C | `192.168.1.12` | LFD2、S2 | 5202、6202、8202 |
| D | `192.168.1.13` | LFD3、S3 | 5203、6203、8203 |

修改两个 distributed 配置文件中的**每一个 `advertised_host`**：`[rm]` 和 `[gfd]`
填 A 当前的 IPv4，`[server.S1]` / S2 / S3 分别填 B/C/D 的地址。
`bind_host = "0.0.0.0"` 保持不变。Mac 可用 `ipconfig getifaddr en0` 查询地址
（接口不同时替换为当前接口），Linux 使用 `ip -4 addr`。四台机器必须能互通，
防火墙放行上表端口。将修改后的每个配置文件原样复制到全部四台电脑。

所有启动参数中的路径都指向**执行命令的这台机器**上的文件。启动前可检查配置：

```bash
.venv/bin/python -c 'from ft_system.common.config import load_config; c = load_config("configs/m5.active.distributed.toml"); print(c.recovery); print(c.servers)'
```

演示时共有 **11 个业务窗口**。A 开五个，分别运行 RM、GFD 和三个 client。
B、C、D 各自手动开一个 LFD 窗口。RM 发来启动指令后，LFD 会在本机自动开第二个窗口运行 server。

## 启动 active 演示

每条命令在独立终端运行，工作目录均为该电脑的仓库根目录。

在 **A** 上先启动 RM，再启动 GFD：

```bash
.venv/bin/python -m ft_system.rm.main --config configs/m5.active.distributed.toml
.venv/bin/python -m ft_system.gfd.main --config configs/m5.active.distributed.toml
```

**A 的同学**确认两个窗口分别显示 `RM: 0 members` 和 `GFD: 0 members`，并看到 GFD 已通知 RM。
保持这两个进程运行。在 A 的网络设置中打开演示用的 Wi-Fi 或有线连接，查看 IPv4 地址，核对它与共享配置中
`[rm]`、`[gfd]` 的 `advertised_host` 一致，再把这个地址告诉 B、C、D。

**B、C、D 的同学**可以在启动 LFD 前，各自在自己的电脑上打开一个终端，检查能否连到 A。
假设 A 报的地址是 `192.168.1.42`，每个人都运行下面两行。如果 A 报的是其他地址，
就把两行里的 `192.168.1.42` 都换成 A 报的地址。第一行连 RM（7300），第二行连 GFD（7200）。

```bash
nc -vz 192.168.1.42 7300
nc -vz 192.168.1.42 7200
```

`nc -vz` 会尝试建立一次 TCP 连接，并打印结果。

- 两行都显示 `succeeded` 或 `open`，说明当前这台电脑能连到 A 上的两个服务。接下来按下文顺序启动 LFD。
- 若某一行显示 `Connection refused`，请 A 的同学检查对应进程是否正在运行。例如只有 7200 被拒绝，就先看 A 的 GFD 窗口。
- 若显示 `timed out`，先确认命令里的 IP 与 A 报的一致，再检查演示网络和 A 的防火墙是否允许连接该端口。

保持 B、C、D 的 LFD 窗口运行。两份四机配置都已开启 `[recovery].mac_terminal = true`。
首次启动时，如果 macOS 询问是否允许控制“终端”，点**允许**。自动打开的窗口前台运行
真实的 server，直接显示实时日志。每次重启的输出也会追加到 `--replica-log` 指定的文件中。

**B 先在终端 1 启动 LFD1。**

```bash
# B
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD1 --replica-log logs/m5-active/S1.log
```

B 会看到新的 **S1** 窗口，里面开始打印 S1 的日志。

**A** 在自己的 RM 窗口看到 `RM: S1 is ready (i_am_ready = 1)` 后，通知 **C**。
C 这时在自己的终端 1 启动 LFD2。

```bash
# C
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD2 --replica-log logs/m5-active/S2.log
```

C 会看到新的 **S2** 窗口。

**A** 在 RM 窗口看到 `RM: S2 is ready (i_am_ready = 1)` 后，通知 **D**。
D 这时在自己的终端 1 启动 LFD3。

```bash
# D
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD3 --replica-log logs/m5-active/S3.log
```

D 会看到新的 **S3** 窗口。

**A** 在 RM 窗口看到 `RM: S3 is ready (i_am_ready = 1)` 后，确认最新 view 显示
`members: S1, S2, S3; ready: S1, S2, S3`。随后 A 再开三个终端，分别启动 C1、C2、C3，每个终端运行一条命令。

```bash
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C1 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C2 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C3 --interval 0.2
```

客户端会无限循环。Active 请求发送给全部副本，每个请求只交付一个回复，其余去重。
配置、server 日志、相邻的 `.pid` 和 `.pid.tmp` 文件必须使用不同路径；LFD 会在启动前
拒绝直接重合的路径。

## Active 三轮故障

以 **C** 为例。点进自动打开的 **S2 server 窗口**，按 **Control+C**，S2 收到中断后退出。
也可以直接点这个窗口左上角的红色关闭按钮。如果 macOS 询问是否终止窗口内的进程，点**终止**。
保持 LFD2 的窗口开着。RM 要求恢复时，C 会自动弹出一个**新的 S2 窗口**。等 A 的同学
确认三个副本都 ready，再进行下一轮。如果旧窗口显示“进程已完成”，下一轮操作新窗口。
Control+Z 会暂停进程，原进程仍占用端口。

| 轮次 | 操作 | 必须观察到的结果 |
| --- | --- | --- |
| 1 | 杀任意副本，例如 S2 | LFD 检测故障，GFD/RM 剩两个成员；客户端继续；RM 指示 LFD2 启动 S2，新 PID 出现，状态同步，恢复三个 ready 副本 |
| 2 | 完全恢复后，关闭**新弹出的 S2 窗口**并确认终止 | 再次自动完成整个流程，包括重新传输状态 |
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

每轮都在目标副本**当前的 server 窗口**按 Control+C 或点红色关闭按钮。本机 LFD 会按 RM 指令
自动打开替代副本的新窗口。现场核对以下现象。

- RM 输出 `primary S1 failed; new primary is ...`，三个 client 均输出 `primary changed`。
- 新 primary 向 backup 发送 checkpoint，并回复三个 client。
- 原 primary 恢复后输出 `role = backup (primary is ...)`，安装 checkpoint，不抢回主角色。
- 每轮 membership 都是 `3 -> 2 -> 3`，随后三个副本都 ready。
- 整个演示保持 client 运行，轮次之间不暂停、不重启 client。
- 每个 client 的 `request_num N: client_value = N` 在切主前后都应成立；
  primary 崩溃后的下一次成功加法不能返回之前已经出现过的值。

Checkpoint 仍按周期发送。Primary 执行请求后，等待下一次包含该请求的 checkpoint。
Backup 安装完成后才回 ACK；当前 RM membership 中所有 backup 均确认，且至少有一个
backup 时，primary 输出 `checkpoint N confirmed by ...; client replies released`，
再发送成功回复。因此已成功回复的请求能保留到一次后续 primary 崩溃之后；重试也不会重复执行。

示例的 checkpoint 周期是 1 秒，因此每个 client 通常约每秒收到一次回复，三个 client
可由同一份 checkpoint 一起确认，并未改成每请求发送 checkpoint。未收到成功回复的请求
可以在切主后重试。如果没有可用 backup，成功回复会等待 backup 回来；单个内存副本无法
保证自身崩溃后的数据保留。修改演示参数时，client 的 read timeout 应大于 checkpoint
周期加传输时间。所有机器必须使用同一版本代码。故障检测、选主和重连也会增加响应延迟，
应记录响应间隔，不应宣称零延迟。

### 验收时解释一致性、延迟与持续运行

`18-749 _ The Project.docx` 的 FAQ **“What do you mean by trade-offs in the
system?”** 明确讨论了一致性与性能的取舍，包括
“a blocking implementation that then affects latency”（阻塞实现会影响延迟）。
FAQ **“Should the clients be automated, and running in a loop?”** 要求演示期间
应用持续运行。M5 passive rubric 也要求副本暂时只剩两个、RM 正在恢复第三个时，
client 仍能收到回复。因此，我们将“持续运行”理解为：client 的自动循环保持运行，
存活副本在恢复期间持续完成请求。提供的 rubric 没有规定固定请求速率或最大响应延迟。

每个 client 使用串行 request–reply 循环：收到当前请求的回复后，才发送下一条请求。
等待 checkpoint 确认会降低请求速率、增加响应延迟。等待期间会释放 server 的请求处理锁，
其他请求、checkpoint 和 heartbeat 可以继续执行。切主时 client 自动重试，无需人工
暂停或重启。换来的保证是：成功回复对应的状态已经安装到可被选为新 primary 的 backup，
在后文规定的故障模型下，一次 primary 崩溃不会丢失已确认的更新。

验收时，应展示**每轮仅剩两个成员期间**，存活副本仍有成功回复、各 client 的计数继续
增长、checkpoint 继续发送，随后自动恢复为三个 ready 副本；保留回复时间戳以说明间隔。
[本机验证记录](m5-passive-commit-verification.md) 使用原有 1 秒 checkpoint 周期，
三轮仅剩两个成员的区间内，三个 client 每轮都各收到一次成功回复。包含切主在内的最大
实测回复间隔为 **2.004 秒**；每个 client 完成 30 个请求，所有副本最终状态均为
**30/30/30**。这是本机观测值，不是延迟上限；四台演示机器上还需重复核对。

现场可以这样解释：

> 故障期间，三个 client 始终运行自动请求—回复循环。我们保留周期 checkpoint，等 backup
> 安装了请求对应的状态才回复成功，用响应延迟换取已确认更新的保留。一个副本正在恢复时，
> 其余两个副本继续完成请求，整个过程不需要暂停或重启 client。

## 配置与关闭

```toml
[recovery]
enabled = true
mac_terminal = true
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

演示结束后先停三个 client，然后**先停 RM，再停 LFD**，最后停 GFD。在各自的 **LFD 窗口**
按 Control+C，LFD 会终止自己的 server 并删除 PID 文件。可以关掉已经结束的窗口，日志会保留在磁盘上。

没有图形桌面时，可设 `mac_terminal = false`。单机配置默认使用这个后台模式，Linux 和 WSL
也使用后台模式。这时可以在 C 用 `tail -F logs/m5-active/S2.log` 看 S2 的日志，用
`kill -KILL "$(cat logs/m5-active/S2.pid)"` 强制终止 S2。演示 passive 时换成对应日志目录。

## 自动测试与排错

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/m5_smoke.py
.venv/bin/python scripts/m5_smoke.py --mode passive --first S2
.venv/bin/python scripts/m5_smoke.py --demo-timing
```

要在一台已登录桌面的 Mac 上验证真实窗口，可以运行下面这条命令。
测试会启动本地 RM、GFD 和三个客户端，在真实 Terminal 中向前台副本发送 Control+C
和终端挂断信号，检查每种模式的三轮恢复。要实测红色关闭按钮，可把环境变量改为
`M5_TEST_TERMINAL=confirm-close`，在 Terminal 询问是否关闭测试 S2 窗口时点**终止**。

```bash
M5_TEST_TERMINAL=1 .venv/bin/python -m pytest -q tests/integration/test_m5_mac_terminal.py
```

Smoke 自动分配 loopback 端口，启动 11 个业务进程并实际杀掉副本。只有 LFD 可以
启动 server 子进程，测试脚本不会替系统重启副本。默认缩短各计时参数，每个 client
完成 240 个请求。`--demo-timing` 保留演示配置的原有周期和超时，每个 client 在 active
下完成 1200 个请求，在 passive 下完成 30 个请求，以适配按 checkpoint 周期返回的回复。
检查 membership、readiness、主备角色、checkpoint、请求顺序、两种模式的每次回复值和
最终计数，以及退出清理。
配置、各进程日志和各模式的 `result.json` 写入 `logs/m5-smoke/` 下新的时间戳目录。
pytest 还会分别让 S1/S2/S3 先启动，覆盖两种模式。
这些是本机测试；四机网络仍需实际执行上面的流程，并保留终端日志作为现场证据。

| 现象 | 检查内容 |
| --- | --- |
| LFD 已注册但 server 没启动 | RM/GFD 是否运行、配置是否一致、`recovery.enabled`、RM 是否收到 GFD membership 及 LFD 注册 |
| Mac 提示没有自动化权限 | 在该副本所在 Mac 的“系统设置 → 隐私与安全性 → 自动化”里，允许启动 LFD 的 App 控制“终端”。保持 RM 和 LFD 运行，等待下一次重试。 |
| 重复出现 `launch ... failed` 或子进程迅速退出 | LFD 诊断、server 日志、解释器与安装、配置路径、日志目录权限、端口占用 |
| RM 显示 `external` | 已有手动启动的 server 响应，应先停止旧演示，再开始由 LFD 管理的 M5 演示 |
| 成员数为三但 ready 不足三个 | checkpoint 端口连通性、donor/primary 日志、`i_am_ready`；先等状态同步完成 |
| Client 连不上副本 | advertised 地址必须是真实 LAN 地址，不能用 `0.0.0.0`/loopback；检查防火墙及网络隔离 |
| 杀进程后短暂没有 PID 文件 | 旧进程已回收，新进程尚未启动；等待新 PID 和 ready view |
| Passive 回复等待，但 heartbeat 继续 | 检查 checkpoint 周期、backup 安装 ACK 和当前 membership；成功回复要求至少一个 backup，且当前所有 backup 均已确认 |

本次 M5 rubric 验证的是 RM/GFD/LFD 保持运行时的连续副本进程崩溃。整机/LFD 崩溃、
网络分区、全部副本状态丢失，以及通过 RM 在运行时修改配置，不属于本次实现范围。
本分支已按用户选择暂缓运行时配置功能。
