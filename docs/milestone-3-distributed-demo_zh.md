# Milestone 3: 四台电脑被动复制演示指南

本指南演示三副本被动复制：S1 是 primary，S2/S3 是 backup。客户端请求只发给 S1；S1 定期通过独立 checkpoint 通道把状态复制到两个 backup。演示中依次停止两个 backup，验证客户端与 S1 持续工作。

## 1. 电脑分工

| 设备 | 运行的组件 |
| --- | --- |
| 电脑 A | GFD、C1、C2、C3 |
| 电脑 B | S1、LFD1 |
| 电脑 C | S2、LFD2 |
| 电脑 D | S3、LFD3 |

每台电脑必须使用同一版本的项目代码和同一份 `configs/m3.distributed.toml`。所有电脑应位于同一个可互相访问的局域网；不要使用访客 Wi-Fi 或阻止设备互访的网络。

## 2. 准备 Python 环境

在每台电脑的项目根目录执行一次：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

之后每次打开新终端，都先进入项目目录并激活环境：

```bash
cd /path/to/Fall2026_18749_Project
source .venv/bin/activate
```

## 3. 创建并共享配置

从 `configs/m3.local.toml` 复制一份配置，命名为 `configs/m3.distributed.toml`，再将下列 IP 地址替换为四台设备的实际局域网 IPv4 地址。

在 macOS 上可用以下命令查看无线网卡 IP：

```bash
ipconfig getifaddr en0
```

配置的关键部分如下；其余 `[client.*]` 和 `[lfd.*]` 区块保持与 `m3.local.toml` 相同。

```toml
[gfd]
bind_host = "0.0.0.0"
advertised_host = "<电脑A-IP>"
port = 7100
heartbeat_freq = 1.0
read_timeout = 2.0

[passive]
primary_id = "S1"
checkpoint_freq = 0.5

[server.S1]
bind_host = "0.0.0.0"
advertised_host = "<电脑B-IP>"
client_port = 5101
heartbeat_port = 6101
checkpoint_port = 8101

[server.S2]
bind_host = "0.0.0.0"
advertised_host = "<电脑C-IP>"
client_port = 5102
heartbeat_port = 6102
checkpoint_port = 8102

[server.S3]
bind_host = "0.0.0.0"
advertised_host = "<电脑D-IP>"
client_port = 5103
heartbeat_port = 6103
checkpoint_port = 8103
```

将该文件无修改地复制到四台电脑。确保每台设备的防火墙允许局域网 TCP 访问端口 `7100`、`5101-5103`、`6101-6103` 与 `8101-8103`。

## 4. 启动顺序

在电脑 A：

```bash
PYTHONPATH=src python -m ft_system.gfd.main --config configs/m3.distributed.toml
```

GFD 应打印 `GFD: 0 members`。

在电脑 B、C、D 分别启动本机 LFD：

```bash
# 电脑 B
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m3.distributed.toml --lfd-id LFD1

# 电脑 C
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m3.distributed.toml --lfd-id LFD2

# 电脑 D
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m3.distributed.toml --lfd-id LFD3
```

然后依次启动 S1、S2、S3：

```bash
# 电脑 B
PYTHONPATH=src python -m ft_system.server.main --config configs/m3.distributed.toml --server-id S1

# 电脑 C
PYTHONPATH=src python -m ft_system.server.main --config configs/m3.distributed.toml --server-id S2

# 电脑 D
PYTHONPATH=src python -m ft_system.server.main --config configs/m3.distributed.toml --server-id S3
```

GFD 应依次显示 `S1`、`S1, S2`、`S1, S2, S3`。如果 S1 在 S2/S3 启动前短暂打印 checkpoint connection refused，这是预期的；backup 开始监听后，S1 会自动在下一个周期连接。

最后在电脑 A 的三个终端分别启动客户端：

```bash
PYTHONPATH=src python -m ft_system.client.main --config configs/m3.distributed.toml --client-id C1 --interval 1
PYTHONPATH=src python -m ft_system.client.main --config configs/m3.distributed.toml --client-id C2 --interval 1
PYTHONPATH=src python -m ft_system.client.main --config configs/m3.distributed.toml --client-id C3 --interval 1
```

## 5. 演示流程和检查项

1. 在 GFD 窗口展示成员从 0 增加到 `S1, S2, S3`。
2. 在客户端与 S1 窗口展示请求与回复均为 `C? -> S1` 和 `S1 -> C?`。S2/S3 不应处理客户端请求或发送回复。
3. 在 S1 窗口展示 `S1 sending checkpoint`；在 S2/S3 窗口展示各自 `received checkpoint`。日志中会包含 `my_state` 和 checkpoint 编号。
4. 在电脑 C 的 S2 窗口按 `Ctrl-C`。LFD2 应报告 heartbeat 失败，GFD 应变为 `GFD: 2 members: S1, S3`。客户端仍持续收到 S1 回复，S3 继续收到 checkpoint。
5. 等待几秒后，在电脑 D 的 S3 窗口按 `Ctrl-C`。LFD3 应报告失败，GFD 应变为 `GFD: 1 member: S1`。客户端仍持续收到 S1 回复。

不要停止 S1：Milestone 3 不要求 primary 故障转移、Replication Manager 或自动恢复。

## 6. 常见问题

- `checkpoint ... connection refused`：对应 backup 尚未启动、已停止，或 `advertised_host`/`checkpoint_port` 不一致。检查 S2/S3 的 server 进程及共享配置。
- GFD 没有成员：检查 LFD 是否使用正确的 `--lfd-id`，并确认电脑 A 的 `advertised_host` 可从其他电脑访问。
- 客户端无法连接 S1：确认电脑 B 的 S1 正在运行，且防火墙未拦截 5101 端口。
- 客户端向 S2/S3 发请求：确认三个设备加载的是含 `[passive]` 且 `primary_id = "S1"` 的 M3 配置，而不是 M2 配置。
