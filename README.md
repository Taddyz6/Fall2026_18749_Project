# 18-749 Fault-Tolerant System — Milestone 1

本目录实现课程项目 Milestone 1：三个独立客户端、一个有状态服务器副本 S1，以及一个通过可配置 heartbeat 检测 S1 故障的 LFD1。当前代码的协议、配置和进程边界为 Milestone 2–5 预留扩展点，但没有提前加入 GFD、RM、复制或 checkpoint。

## 架构

```text
C1 --\
C2 ---- TCP client_port ---- ClientListener ---- DeterministicStateMachine
C3 --/                              S1
                                     |
LFD1 ---- TCP heartbeat_port ---- HeartbeatListener
```

S1 使用两个相互独立的 TCP 端口：

- `client_port`：只处理 client request/reply；
- `heartbeat_port`：只处理 LFD heartbeat/ACK，且 `HeartbeatListener` 无权访问 `my_state`。

消息使用一行一条 JSON 的 JSON Lines framing。每个请求同时携带课程要求的 `request_num` 和内部逻辑标识 `request_id = <client_id>:<request_num>`。

## 环境准备

需要 Python 3.11 或更高版本。应用运行时只使用标准库；pytest 依赖只用于开发和验证。

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## 自动验证

运行全部测试：

```bash
.venv/bin/python -m pytest -v
```

运行自动 smoke check：

```bash
.venv/bin/python scripts/m1_smoke.py
```

Smoke check 使用动态端口，自动验证三个客户端各发送一次请求、状态变为 `{"C1": 1, "C2": 1, "C3": 1}`、heartbeat 正常，以及关闭 S1 后 LFD1 检测到故障。它不是课堂手工演示的替代品。

## 本机五终端演示

配置文件为 `configs/local.toml`。按以下顺序打开五个终端。

终端 1 — S1：

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate
ft-server --config configs/local.toml --server-id S1
```

终端 2 — LFD1：

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate
ft-lfd --config configs/local.toml --lfd-id LFD1
```

终端 3、4、5 — C1、C2、C3（每个终端替换对应 ID）：

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate
ft-client --config configs/local.toml --client-id C1
```

在每个 client 提示符输入 `increment` 或直接按 Enter。输入 `quit` 或 `exit` 可正常退出。

S1 对一次 C1 请求应打印：

```text
[timestamp] Received <C1, S1, 1, request>
[timestamp] my_state_S1 = {"C1": 0, "C2": 0, "C3": 0} before processing <C1, S1, 1, request>
[timestamp] my_state_S1 = {"C1": 1, "C2": 0, "C3": 0} after processing <C1, S1, 1, request>
[timestamp] Sending <C1, S1, 1, reply>
```

LFD1 会逐次打印：

```text
[timestamp] [1] LFD1 sending heartbeat to S1
[timestamp] [1] LFD1 receives heartbeat from S1
```

S1 同时打印 heartbeat 的接收和 ACK 发送，但 heartbeat 不改变 `my_state`。

## 故障注入与不同 heartbeat 频率

1. 保持 LFD1 与三个 clients 运行。
2. 在 S1 终端按 Ctrl-C。
3. LFD1 应输出一次失败 heartbeat 及死亡事件，例如：

   ```text
   [timestamp] [2] LFD1 heartbeat to S1 failed: connection closed
   [timestamp] S1 has died
   ```

   如果连接保持但 S1 未返回 ACK，失败原因会显示为 `timeout`。后续重连失败不会重复刷这些故障事件。
4. 停止剩余进程并重新启动整套系统。
5. 使用命令行覆盖心跳周期：

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 0.5
```

单次 heartbeat ACK timeout、EOF 或连接异常均会使一个此前健康的 S1 被判定为故障；失败日志同时包含对应的 `heartbeat_count` 和原因。

## 多机演示

1. 复制 `configs/distributed.example.toml` 为团队自己的配置文件。
2. 把 S1/LFD1 所在机器的局域网 IP 写入 `advertised_host`。
3. S1 保持 `bind_host = "0.0.0.0"`，允许其他机器连接。
4. 将完全相同的配置文件复制到所有机器。
5. S1 和 LFD1 按课程要求运行在同一物理机器；C1、C2、C3 可以运行在另一台机器。
6. 在 S1 所在机器的防火墙中允许 `client_port`（默认 5001）和 `heartbeat_port`（默认 6001）的入站 TCP 流量。

多机模式与本机模式使用完全相同的三个命令行入口，只替换 `--config` 路径。

## 常见问题

- `Address already in use`：已有进程占用配置端口；结束旧进程或修改两个端口，且二者不能相同。
- `Connection refused`：确认 S1 已启动、IP/端口正确，并检查防火墙。
- heartbeat timeout：检查 LFD1 是否指向 S1 的 `heartbeat_port`，不要误用 `client_port`。
- client timeout：检查 client 是否指向 `client_port`，以及 S1 控制台是否显示请求。
- 配置启动失败：所有频率和 timeout 必须为正数，端口必须在 1–65535 之间。

## 项目结构

```text
src/ft_system/common/   protocol、transport、config、logging、retry
src/ft_system/server/   双 listener 与确定性状态机
src/ft_system/client/   交互式持久连接客户端
src/ft_system/lfd/      heartbeat、故障检测和干净重连
tests/unit/             纯逻辑测试
tests/integration/      真实 loopback TCP 集成测试
configs/                本机配置与多机模板
scripts/m1_smoke.py     自动 smoke check
```

详细架构见 `docs/superpowers/specs/2026-09-01-milestone-1-foundation-design.md`，逐步实施与验收计划见 `docs/superpowers/plans/2026-09-01-milestone-1-foundation.md`。
