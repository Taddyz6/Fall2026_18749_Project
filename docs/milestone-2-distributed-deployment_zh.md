# Milestone 2 四台电脑分布式部署教程

本教程用于 **四台电脑、十个独立终端窗口**的 M2 现场演示。它使用仓库现有的分布式配置和单进程命令；`scripts/m2_demo.py` 只会在**本机**打开十个窗口，不能替四台电脑远程启动进程。

所有命令默认在各电脑的仓库根目录执行，使用 Python 3.11 或更新版本。正式演示前，先在四台真实电脑上完整走一遍启动、通信和两次人工故障流程；本仓库的自动 smoke test 只使用单机回环网络。

## 1. 确定机器分工和网络

| 电脑 | 运行的进程 | 需要保持打开的窗口 | 示例局域网 IP |
| --- | --- | ---: | --- |
| A | GFD、C1、C2、C3 | 4 | `192.168.1.10` |
| B | S1、LFD1 | 2 | `192.168.1.11` |
| C | S2、LFD2 | 2 | `192.168.1.12` |
| D | S3、LFD3 | 2 | `192.168.1.13` |

表里的 IP **只是示例**，必须换成现场四台电脑实际能互相访问的地址。三组服务器与各自的 LFD 必须同机；GFD 与三个客户端同机。四台电脑要处于能互访的局域网或可路由网络中。某些校园或访客 Wi-Fi 会隔离设备，即使都能上网，电脑之间仍可能无法建立 TCP 连接；遇到这种情况应换到允许互访的网络。

### 查出每台电脑的 IP

在**每台电脑**分别查看其正在使用的局域网 IPv4 地址，并把结果填入上表。macOS 可先查看网络设备，再按实际设备名查询：

```bash
networksetup -listallhardwareports
ipconfig getifaddr en0
```

`en0` 只是常见设备名；若 Wi-Fi 或有线网卡对应的是 `en1` 等设备，请替换。Linux 可用 `ip -4 -br addr`。不要使用 `127.0.0.1`、VPN 虚拟网卡地址，或把 `0.0.0.0` 当作供其他电脑连接的 IP。

最好在路由器上为四台电脑做 DHCP 地址保留，或按现场网络管理规则设置稳定地址。若使用会变化的动态 IP，**每次演示前重新核对四个 IP**。任何一台的 IP 变化，都要重新生成并分发配置，然后重启整套 M2 进程；只改其中一台的配置会让各进程连接到不同地址。

## 2. 四台电脑使用同一版代码

四台电脑都需要这份仓库的 `m2` 分支。首次安装时，在每台电脑运行下面的命令；若已有仓库，可继续使用原目录，但先确认四台电脑位于同一提交，不要覆盖未提交的工作。

```bash
git clone --branch m2 https://github.com/Taddyz6/Fall2026_18749_Project.git ~/Fall2026_18749_Project
cd ~/Fall2026_18749_Project
python3 --version
git rev-parse HEAD
```

如果仓库需要登录，请使用已有的 GitHub 访问方式。以下命令以 `~/Fall2026_18749_Project` 为统一路径；仓库放在其他位置时，相应替换路径。程序由标准库直接运行，下面的启动命令不要求先安装 Python 包或 pytest。

## 3. 在 A 上生成唯一的共享配置

在 **A 的仓库根目录**从示例创建现场配置（如果文件已存在则保留），然后打开它：

```bash
test -e configs/m2.distributed.toml || cp configs/m2.distributed.example.toml configs/m2.distributed.toml
nano configs/m2.distributed.toml
```

把文件中**四处** `advertised_host` 分别改成 A、B、C、D 的实际 IP：

| 配置节 | `advertised_host` 应填写 | 谁会连接它 |
| --- | --- | --- |
| `[gfd]` | A 的 IP | B/C/D 上的 LFD |
| `[server.S1]` | B 的 IP | A 上的客户端、B 上的 LFD1 |
| `[server.S2]` | C 的 IP | A 上的客户端、C 上的 LFD2 |
| `[server.S3]` | D 的 IP | A 上的客户端、D 上的 LFD3 |

`bind_host = "0.0.0.0"` 表示 GFD/服务器监听本机所有网卡，**不要**把它填入 `advertised_host`。保持三个 `[lfd.*]` 的 `heartbeat_freq = 1.0` 一致；客户端的 `server_ids` 应都是 `S1, S2, S3`。未占用时可保留示例端口：A 的 GFD 用 TCP `7000`；B/C/D 的客户端端口分别是 `5001`/`5002`/`5003`，本地心跳端口分别是 `6001`/`6002`/`6003`。如果现场修改端口，四台电脑仍须使用完全相同的新配置。

保存后在 A 上检查配置能加载，并核对打印的地址：

```bash
PYTHONPATH=src python3 - <<'PY'
from ft_system.common.config import load_config

config = load_config("configs/m2.distributed.toml")
print("GFD/A:", config.gfd.advertised_host, config.gfd.port)
for replica_id in ("S1", "S2", "S3"):
    server = config.server(replica_id)
    print(replica_id, server.advertised_host, server.client_port, server.heartbeat_port)
PY
```

输出中的四个 IP 应与第 1 节记录的一致；不能还留着与现场无关的示例地址。

## 4. 把同一份配置同步到 B、C、D

**先确保四台电脑都有相同版本的项目代码，再分发配置。**若 B/C/D 开启了 SSH 服务（macOS 的“系统设置 → 通用 → 共享 → 远程登录”），可在 A 上用 `scp`。下面的用户名和 IP 仍是示例，执行前请替换；命令假设另外三台都把仓库克隆到了各自的 `~/Fall2026_18749_Project`：

```bash
scp configs/m2.distributed.toml userB@192.168.1.11:Fall2026_18749_Project/configs/m2.distributed.toml
scp configs/m2.distributed.toml userC@192.168.1.12:Fall2026_18749_Project/configs/m2.distributed.toml
scp configs/m2.distributed.toml userD@192.168.1.13:Fall2026_18749_Project/configs/m2.distributed.toml
```

若不能使用 SSH，也可以用 AirDrop、共享文件夹或 U 盘复制**同一个文件**到 B/C/D 的 `configs/` 目录。不要在四台电脑上分别手工编辑四份配置。

复制后，在**四台电脑各自的仓库根目录**运行同一条命令，核对 SHA-256 输出完全一致：

```bash
python3 - <<'PY'
import hashlib
from pathlib import Path

path = Path("configs/m2.distributed.toml")
print(hashlib.sha256(path.read_bytes()).hexdigest(), path)
PY
```

若哈希不同，先重新复制，**不要启动进程**。也再次用 `git rev-parse HEAD` 确认四台代码提交相同。只要任一 IP、端口或其他配置发生变化，就以 A 上的文件为准，重新复制到 B/C/D 并再次核对哈希。

## 5. 检查网络和端口

确认四台电脑使用同一个可互访网络，且防火墙允许下列 TCP 流量：

| 来源 | 目标 | 用途 |
| --- | --- | --- |
| B/C/D 的 LFD | A:`7000` | 注册、成员变化、GFD 心跳 |
| A 的 C1/C2/C3 | B:`5001`、C:`5002`、D:`5003` | 客户端请求与服务器回复 |
| B 的 LFD1、C 的 LFD2、D 的 LFD3 | 各自同机的 `6001`、`6002`、`6003` | LFD 到本地服务器的心跳 |

第三行是**同一台电脑内**的连接，仍使用配置中的该机器局域网 IP。先用 `ping <目标IP>` 排查基本连通性；有些网络禁用 ICMP，因此 ping 失败并不能单独证明 TCP 不通。相应服务启动后，可在连接方用 `nc -vz <目标IP> <端口>` 检查 TCP；例如在 B 上测试 A 的 GFD，可用 `nc -vz 192.168.1.10 7000`（替换实际 IP）。这类原始 TCP 探测可能在服务窗口留下一条协议错误日志，属于探测连接，并不表示 M2 本身失败。

如果端口已被旧 M1/M2 进程占用，先在旧进程的窗口按 Ctrl-C；不要同时运行单机 `m2_demo.py` 和这套四机部署。

## 6. 按顺序打开十个独立窗口

**每条进程命令都在它自己的终端窗口执行，运行后保持窗口打开；不要把多条命令依次贴进同一个窗口。**每个新窗口先执行 `cd ~/Fall2026_18749_Project`；下文命令均从该目录运行。没有使用虚拟环境时，确认终端的 `python3` 为 3.11 或更新版本。

### 第一步：A 上启动 GFD（A 的第 1 个窗口）

```bash
PYTHONPATH=src python3 -m ft_system.gfd.main --config configs/m2.distributed.toml
```

先看到 `GFD: 0 members`。GFD 必须在 LFD 注册前启动。

### 第二步：B/C/D 各启动自己的 LFD（各 1 个窗口）

在 **B** 的 LFD1 窗口运行：

```bash
PYTHONPATH=src python3 -m ft_system.lfd.main --config configs/m2.distributed.toml --lfd-id LFD1
```

在 **C** 的 LFD2 窗口运行：

```bash
PYTHONPATH=src python3 -m ft_system.lfd.main --config configs/m2.distributed.toml --lfd-id LFD2
```

在 **D** 的 LFD3 窗口运行：

```bash
PYTHONPATH=src python3 -m ft_system.lfd.main --config configs/m2.distributed.toml --lfd-id LFD3
```

GFD 应分别显示 `registered LFD1`、`registered LFD2`、`registered LFD3`，但成员数仍为 **0**：只有 LFD 注册、服务器尚未启动时，不能把服务器计入成员表。此时 LFD/GFD 之间的心跳会持续打印。

### 第三步：依次启动 B、C、D 上的服务器（各再开 1 个窗口）

先在 **B** 的 S1 窗口运行：

```bash
PYTHONPATH=src python3 -m ft_system.server.main --config configs/m2.distributed.toml --server-id S1
```

等 LFD1 收到 S1 的心跳回复、GFD 显示 `GFD: 1 member: S1` 后，再在 **C** 的 S2 窗口运行：

```bash
PYTHONPATH=src python3 -m ft_system.server.main --config configs/m2.distributed.toml --server-id S2
```

等 GFD 显示 `GFD: 2 members: S1, S2` 后，再在 **D** 的 S3 窗口运行：

```bash
PYTHONPATH=src python3 -m ft_system.server.main --config configs/m2.distributed.toml --server-id S3
```

继续等 GFD 显示 `GFD: 3 members: S1, S2, S3`。若成员没有增加，先不要启动客户端，应检查对应 S/LFD 的窗口、配置 IP 和网络。

### 第四步：A 上分别启动三个客户端（A 再开 3 个窗口）

在 **A** 的 C1、C2、C3 窗口各运行一条：

```bash
# C1 窗口
PYTHONPATH=src python3 -m ft_system.client.main --config configs/m2.distributed.toml --client-id C1
```

```bash
# C2 窗口
PYTHONPATH=src python3 -m ft_system.client.main --config configs/m2.distributed.toml --client-id C2
```

```bash
# C3 窗口
PYTHONPATH=src python3 -m ft_system.client.main --config configs/m2.distributed.toml --client-id C3
```

客户端默认**自动、无限循环**发送请求，不需要输入。确认每个客户端能向 S1/S2/S3 发同一编号请求、交付最先到达的回复，并打印后续重复回复；三个服务器各自打印收到的请求与发出的回复。此时四台电脑共运行 **4 + 2 + 2 + 2 = 10** 个进程。

## 7. 人工故障演示与结束

1. **只在 B 的 S1 窗口按 Ctrl-C**，保持其余九个进程运行，包括 B 的 LFD1。LFD1 应打印一次 S1 心跳失败、`S1 has died` 和 `LFD1: delete replica S1`；GFD 应显示 `GFD: 2 members: S2, S3`。客户端继续自动发送并接收 S2/S3 的回复。
2. 等客户端又完成几轮请求后，**只在 C 的 S2 窗口按 Ctrl-C**，保持 LFD2 运行。GFD 应显示 `GFD: 1 member: S3`；三个客户端继续收到 S3 的回复。故障后 GFD 与 LFD1/LFD2 的心跳仍持续，表示检测器活着，**不表示 S1/S2 仍是成员**。
3. 演示结束后，在所有剩余进程窗口按 Ctrl-C。若要重演，先停止所有旧进程，再从第 6 节重新启动整套系统；M2 没有运行中的副本状态转移或自动恢复，不要只重启已故障的单个副本或客户端。

现场故障必须由人操作；`scripts/m2_smoke.py` 会自动注入故障并在每个客户端完成 60 次请求后退出，它只用于单机回归，不是四机现场演示命令。

## 8. 常见问题

| 现象 | 首先检查 |
| --- | --- |
| LFD 一直未在 GFD 注册 | A 的 `7000` 是否监听、A 的 `advertised_host` 是否为可达 IP、B/C/D 到 A 的 TCP 与防火墙、四份配置哈希是否一致 |
| LFD 已注册但 GFD 长期保持 0 个成员 | 对应服务器是否启动、其 `heartbeat_port` 是否监听、同机 LFD 是否指向该服务器实际 IP、相应终端有无心跳回复 |
| GFD 已有 3 个成员，但客户端连接失败 | A 到 B/C/D 的 `5001`/`5002`/`5003`、服务器的 `bind_host`、三台服务器窗口和防火墙 |
| `Address already in use` | 是否有旧 M1/M2 进程占用本机对应端口；确认没有重复启动同一角色 |
| 某台电脑的 IP 变化 | 在 A 更新四个地址中的对应项，重新同步整个配置到 B/C/D，核对哈希并重启全部进程 |
| GFD 的成员变化日志找不到 | 心跳日志每秒刷新，可在 GFD 窗口向上查找 `GFD: ... members` 或 `delete replica`；LFD 心跳继续不等于服务器还活着 |

相关文档：[M2 验收行为与单机演示](milestone-2-instructions_zh.md) · [分布式示例配置](../configs/m2.distributed.example.toml) · [演示检查表](DEMO.md)。
