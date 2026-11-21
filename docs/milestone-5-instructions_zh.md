# M5 四机自动恢复演示

[English](milestone-5-instructions.md) · [系统设计](m5-design.md) · [验证记录](m5-verification.md)

## 文件与环境准备

四台 Mac 连同一局域网，使用 `m5` 分支和 Python 3.11+。以下命令均在各机的仓库根目录运行。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

各机用 `networksetup -listallhardwareports` 找到演示用 Wi-Fi 或有线网卡的 `Device`，用它替换下面的 `en0`，查本机 IPv4。

```bash
ipconfig getifaddr en0
```

B/C/D 把各自的 IP 发给 A。A 按表填写 `configs/m5.active.distributed.toml` 和 `configs/m5.passive.distributed.toml` 中的 `advertised_host`。**A push 到 `m5`，B/C/D pull。**

| 电脑 | 运行的进程 | 填本机 IP 的配置节 | 入站 TCP 端口 |
| --- | --- | --- | --- |
| A（sacred） | RM、GFD、C1–C3 | `[rm]`、`[gfd]` | 7300、7200 |
| B | LFD1、S1 | `[server.S1]` | 5201、6201、8201 |
| C | LFD2、S2 | `[server.S2]` | 5202、6202、8202 |
| D | LFD3、S3 | `[server.S3]` | 5203、6203、8203 |

## 启动 Active

**A** 开两个终端，先启动 RM，再启动 GFD。两个窗口都应显示 `0 members`。

```bash
.venv/bin/python -m ft_system.rm.main --config configs/m5.active.distributed.toml
.venv/bin/python -m ft_system.gfd.main --config configs/m5.active.distributed.toml
```

**B/C/D** 可分别检查能否连到 A。把示例 IP 换成 A 的实际 IP。7300 对应 RM，7200 对应 GFD，两行都显示 `succeeded` 或 `open` 即可。

```bash
nc -vz 192.168.1.42 7300
nc -vz 192.168.1.42 7200
```

B/C/D 各自只开 LFD。RM 发指令后，LFD 自动打开本机 server 窗口并显示日志。macOS 首次询问是否允许控制“终端”时，点**允许**。

**B 先启动 LFD1。**

```bash
# B
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD1 --replica-log logs/m5-active/S1.log
```

**A** 在 RM 窗口看到 `S1 is ready` 后，通知 **C** 启动 LFD2。

```bash
# C
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD2 --replica-log logs/m5-active/S2.log
```

**A** 看到 `S2 is ready` 后，通知 **D** 启动 LFD3。

```bash
# D
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD3 --replica-log logs/m5-active/S3.log
```

**A** 看到 `S3 is ready`，确认 `ready: S1, S2, S3` 后，在三个独立终端分别启动 C1、C2、C3。

```bash
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C1 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C2 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C3 --interval 0.2
```

## 三轮故障

每轮在目标 **server 窗口**按 `Control+C`，或点红色关闭按钮并确认“终止”。保持 LFD 和客户端运行。**A** 在 RM 只剩两个成员时，给 TA 看 C1、C2、C3 各自收到的新回复。等 RM 再次显示 **`ready: S1, S2, S3`**，A 再通知开始下一轮。

### Active 演示

三个客户端都收到回复后开始。第一轮可任选副本，下面以 C 上的 S2 为例。

1. **C 停 S2。** LFD2 报告故障，GFD 更新成员。A 展示 RM 的 `asking LFD2 to ensure replica S2 is running`，C 展示自动弹出的 S2 窗口和 LFD2 打印的新 PID。
2. **C 在第一轮弹出的新窗口里再停一次 S2。** C 展示这次重启的 S2 先记录请求，打印 `installed checkpoint`，回放遗漏的请求后恢复回复。
3. **D 停另一个副本 S3。** D 展示 S3 自动重启并同步状态。A 确认三个副本都 ready。客户端对同一请求只采纳一个回复，其余副本的回复值一致，日志显示 `Discarded duplicate reply`。

### Passive 演示

先按 **三个客户端 → RM → B/C/D 的 LFD → GFD** 停掉 Active。LFD 退出时会关闭本机 server。用 `configs/m5.passive.distributed.toml` 和日志目录 `logs/m5-passive/` 重新启动。

指定谁当初始主，就先启动谁的 LFD，等该副本 ready 再启动其余副本。本例由 B 先开 LFD1，让 S1 当主。第一轮前，A 展示三个客户端都有回复，B 展示 S1 向 S2、S3 发 checkpoint。

1. **B 停主副本 S1。** A 指出 RM 选出的新主，展示三个客户端的 `primary changed` 和后续回复。新主继续发 checkpoint。B 展示恢复的 S1 打印 `role = backup` 和 `installed checkpoint`。
2. **B 在新窗口里再停一次 S1。** 这次停的是刚恢复的 backup。当前主继续回复、向另一个 backup 发 checkpoint。B 展示 S1 再次自动恢复为 backup。
3. **A 根据 RM 的当前主指定另一个 backup。** 新主是 S2，就由 D 停 S3。新主是 S3，就由 C 停 S2。该机展示新 backup 安装 checkpoint，A 确认三个副本都 ready，再按上面的顺序停机。

### Passive 的一致性与延迟

默认每秒发一次 checkpoint。当前所有 backup（至少一个）安装了包含本次请求的 checkpoint 并确认后，主副本才回复成功。这样切主后能保住已确认的更新，代价是回复要等 checkpoint。

三个客户端持续循环，切主时用原请求号自动重试。验收时看两个成员期间的成功回复，以及切主前后每个客户端的计数是否都满足 `request_num N` 对应 `client_value = N`。[实测记录](m5-passive-commit-verification.md)

## 排错与本机自测

| 现象 | 看哪里 |
| --- | --- |
| `nc` 显示 `Connection refused` 或 `timed out` | 拒绝连接时查 A 上对应的 RM/GFD。超时时核对 A 的 IP、局域网和防火墙。 |
| LFD 已启动，但没有 server 窗口 | 看 RM 是否收到 GFD 的成员信息、LFD 是否注册。在本机“系统设置 → 隐私与安全性 → 自动化”允许启动 LFD 的应用控制“终端”。 |
| 三个成员但没全 ready | 查新副本的 `installed checkpoint`、发送方日志及 checkpoint 端口（8201–8203）。 |
| Passive 有心跳但回复一直等 | 查当前 backup 是否安装 checkpoint 并回 ACK。 |

无图形桌面时设 `[recovery].mac_terminal = false`。例如在 C 用 `tail -F logs/m5-active/S2.log` 看日志，用 `kill -KILL "$(cat logs/m5-active/S2.pid)"` 停 S2。

本机自测可跑 pytest 和 smoke。Smoke 日志保存在 `logs/m5-smoke/`。

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/m5_smoke.py --demo-timing
```

在已登录桌面的 Mac 上可单独测真实 Terminal 窗口。手动验证红色关闭按钮时，把环境变量设为 `M5_TEST_TERMINAL=confirm-close`。

```bash
M5_TEST_TERMINAL=1 .venv/bin/python -m pytest -q tests/integration/test_m5_mac_terminal.py
```
