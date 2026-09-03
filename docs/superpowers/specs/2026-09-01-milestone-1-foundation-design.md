# Milestone 1 可扩展基础设计

## 1. 目标

本设计实现 18-749 项目的 Milestone 1，并把通信、配置、日志、状态机和进程生命周期设计成可被 Milestone 2-5 直接复用的基础设施。

Milestone 1 的可验收能力包括：

- 一个有状态服务器副本 S1；
- 三个相互独立的客户端 C1、C2、C3；
- 一个与 S1 同机部署的本地故障检测器 LFD1；
- 可配置的心跳频率；
- 客户端请求和服务器响应；
- 服务器状态在处理请求前后的可见变化；
- S1 退出后，LFD1 通过单次心跳 ACK 超时检测故障；
- 使用不同的心跳频率重新运行系统。

系统必须同时支持：

1. 本机开发模式：所有进程运行在同一台机器上，使用不同端口；
2. 多机演示模式：服务器/LFD 和客户端分布在不同机器上，仅通过配置切换地址。

## 2. 技术选择与范围

- Python 3.11 或更高版本；
- 每个组件作为独立操作系统进程运行；
- 单个进程内部使用 `asyncio`；
- 使用 TCP 长连接；
- 使用一行一条 JSON 消息的 JSON Lines framing；
- 应用运行时只依赖 Python 标准库；
- 测试依赖 `pytest` 和 `pytest-asyncio`。

Milestone 1 不实现 GFD、RM、membership、多副本、主备角色、checkpoint、request log、状态传输、自动拉起进程、circuit breaker 或复杂重试状态机。

## 3. 总体架构

Milestone 1 包含五个独立进程：

```text
C1 --\
C2 ---- TCP client traffic ---- S1
C3 --/                          ||
                                || independent TCP heartbeat traffic
                                ||
                               LFD1
```

S1 的业务流量和心跳流量使用两个独立 TCP 端口和两个独立 listener：

```text
client_port     -> ClientListener    -> asyncio.start_server(...)
heartbeat_port  -> HeartbeatListener -> asyncio.start_server(...)
```

`ServerApp` 只负责组件组装和生命周期管理：

```text
ServerApp
|-- ClientListener
|-- HeartbeatListener
`-- DeterministicStateMachine
```

两个 listener 的连接和异常彼此隔离。`HeartbeatListener` 永远不读取或修改 `my_state`。

## 4. 建议的项目结构

```text
pyproject.toml
README.md
configs/
|-- local.toml
`-- distributed.example.toml
scripts/
src/ft_system/
|-- common/
|   |-- protocol.py
|   |-- transport.py
|   |-- config.py
|   |-- logging.py
|   `-- retry.py
|-- client/
|   |-- app.py
|   `-- main.py
|-- server/
|   |-- state_machine.py
|   |-- app.py
|   `-- main.py
`-- lfd/
    |-- app.py
    `-- main.py
tests/
|-- unit/
`-- integration/
```

模块边界：

- `common.protocol`：消息模型、类型检查、序列化和反序列化；
- `common.transport`：JSON Lines framing、消息大小限制和 asyncio stream 收发；
- `common.config`：TOML 加载、校验和命令行覆盖；
- `common.logging`：课程要求的时间戳和事件格式，可选 ANSI 颜色；
- `common.retry`：连接/读取超时、有上限的简单指数退避、干净重连；
- `client`、`server`、`lfd`：只处理各自的进程行为，不重复实现传输和配置逻辑。

## 5. 确定性状态机

S1 的初始状态为：

```python
my_state = {
    "C1": 0,
    "C2": 0,
    "C3": 0,
}
```

每个客户端只能执行 `increment` 操作，并且只递增自己的槽位。例如，C1 的请求只修改 `my_state["C1"]`。

这个约束使操作具有以下性质：

- 状态转换是确定性的；
- 每个客户端的 TCP 连接维持本客户端请求顺序；
- 不同客户端操作修改互不相交的状态槽位；
- Milestone 2 中，不同客户端在不同副本上的跨客户端到达顺序不一致，也不会造成最终状态分歧；
- 相同逻辑请求在不同副本上的响应仍可按客户端自己的计数进行比较。

S1 内部必须串行执行完整的请求处理段：接收、记录 before state、修改状态、记录 after state、构造 reply。其他请求的状态日志不得插入这一段。

## 6. 消息协议

### 6.1 通用规则

- 每条消息是一行 UTF-8 JSON，以 `\n` 结束；
- 协议版本初始为整数 `1`；
- 单条消息最大 64 KiB；
- 每种消息类型有独立的必填字段校验；
- 未知消息类型或缺少必填字段属于协议错误。

### 6.2 客户端请求

```json
{
  "version": 1,
  "type": "client_request",
  "source": "C1",
  "destination": "S1",
  "client_id": "C1",
  "replica_id": "S1",
  "request_num": 7,
  "request_id": "C1:7",
  "payload": {
    "operation": "increment"
  }
}
```

字段语义：

- `request_num` 是每个 client 单调递增的课程术语和演示字段；
- `request_id` 是跨副本保持不变的逻辑请求标识，格式为 `<client_id>:<request_num>`；
- `replica_id` 标识当前消息实际发往的副本；
- 后续主动复制时，同一个逻辑请求发往不同副本，`request_id` 和 `request_num` 相同，`replica_id` 不同。

服务器 reply 必须保留相同的 `client_id`、`replica_id`、`request_num` 和 `request_id`，并在 payload 中返回对应客户端处理后的计数。

### 6.3 心跳消息

```json
{
  "version": 1,
  "type": "heartbeat",
  "source": "LFD1",
  "destination": "S1",
  "heartbeat_count": 42,
  "payload": {}
}
```

S1 返回 `heartbeat_ack`，并携带完全相同的 `heartbeat_count`。错误计数、错误消息类型或过期 ACK 均不算成功心跳。

## 7. 课程要求的日志接口

所有关键日志必须立即 flush，并以本机时间戳开头。S1 对请求严格输出以下语义事件：

```text
[timestamp] Received <C1, S1, 7, request>
[timestamp] my_state_S1 = {...} before processing <C1, S1, 7, request>
[timestamp] my_state_S1 = {...} after processing <C1, S1, 7, request>
[timestamp] Sending <C1, S1, 7, reply>
```

客户端必须打印请求发送和 reply 接收事件，并使用相同的四元组术语。

LFD1 必须维护：

- `heartbeat_freq`：可由配置或命令行设置；
- `heartbeat_count`：从 1 开始，每次尝试后递增。

LFD1 必须逐次打印 heartbeat 发送和 ACK 接收，并在超时时打印清晰的故障事件。颜色只增强可读性，不替代规定文本。

## 8. 配置

配置使用 Python 标准库 `tomllib`。示例：

```toml
[server.S1]
bind_host = "127.0.0.1"
advertised_host = "127.0.0.1"
client_port = 5001
heartbeat_port = 6001

[lfd.LFD1]
server_id = "S1"
heartbeat_freq = 2.0
connect_timeout = 2.0
read_timeout = 2.0
```

`bind_host` 是 S1 listener 的绑定地址；`advertised_host` 是其他进程用于连接 S1 的可访问地址。多机模式可以令 `bind_host = "0.0.0.0"`，同时将 `advertised_host` 设置为该机器的局域网地址。

运行入口：

```bash
ft-server --config configs/local.toml --server-id S1
ft-lfd --config configs/local.toml --lfd-id LFD1
ft-client --config configs/local.toml --client-id C1
```

命令行参数覆盖配置文件。启动时必须校验组件 ID、IP/主机名、端口范围、正数 heartbeat 频率和正数 timeout。无效配置立即退出。

## 9. 连接生命周期和故障处理

### 9.1 Server

- Client 和 LFD 各自使用持久 TCP 连接；
- 任一连接异常只清理该连接；
- 单个 listener 异常不得关闭另一个 listener；
- 收到 `SIGINT` 或 `SIGTERM` 后停止接收新连接、关闭现有 writer，并退出事件循环。

### 9.2 LFD

- 按 `heartbeat_freq` 发送 heartbeat；
- connect timeout 控制建立连接的等待时间；
- read timeout 控制单次 ACK 的等待时间；
- 单次 heartbeat ACK 超时即判定 S1 故障；
- EOF 或连接异常也视为当次 heartbeat 失败；
- 第一次确认失败时只打印一次 `S1 has died` 事件；
- 后续重连失败不重复刷同一故障事件；
- 重连成功时打印恢复事件并重置退避；
- LFD 进程在 S1 故障期间继续运行。

### 9.3 简单重连

`retry.py` 只实现：

- connect timeout；
- read timeout；
- 带上限的简单 exponential backoff：第 `attempt` 次重连前等待
  `min(initial_backoff * 2**attempt, max_backoff)`；
- `initial_backoff` 默认为 0.5 秒，`max_backoff` 默认为 5 秒；
- 失败时完整关闭旧 writer；
- 使用新 reader/writer 建立干净连接；
- 成功后重置退避计数。

不实现 circuit breaker 或复杂连接状态机。

### 9.4 协议错误

- 客户端通道遇到合法 framing 但非法业务消息时，返回结构化 `error` 消息；
- heartbeat 通道遇到非法协议时记录错误并关闭该连接；
- 无效 UTF-8、超长消息或无法解析的 JSON 会记录协议错误并关闭对应连接；
- 任何单连接错误均不得终止 S1 进程。

## 10. 客户端行为

Milestone 1 的客户端默认使用交互模式：用户每输入一次命令，客户端生成一个新的 `request_num` 并发送请求。

请求生成器和网络客户端必须分离。Milestone 2 添加自动请求循环时，只替换请求生成器，不修改协议、连接管理或 reply 处理。

每个客户端独立维护自己的 `request_num`，从 1 开始，并且不与其他客户端共享内存或状态文件。

## 11. 测试策略

### 11.1 单元测试

- 各消息类型的序列化、反序列化和字段校验；
- `request_num` 和 `request_id` 的生成；
- 状态机只修改对应客户端槽位；
- 不支持的 operation 不产生状态改变；
- TOML 加载、命令行覆盖和非法配置拒绝；
- timeout、简单退避和 clean reconnect。

### 11.2 集成测试

- 两个 S1 listener 可以同时工作；
- heartbeat 不改变 `my_state`；
- C1、C2、C3 分别发送请求并收到对应 reply；
- 并发客户端请求不产生丢失更新；
- 非法客户端消息只关闭对应连接；
- heartbeat ACK 超时后 LFD 只报告一次故障；
- S1 重启后 LFD 可以建立干净的新连接。

测试使用动态分配的空闲端口，避免依赖固定本机端口。

### 11.3 Milestone 1 端到端验收

1. 启动 S1；
2. 启动 LFD1，观察递增的 `heartbeat_count` 和连续 ACK；
3. 分别启动 C1、C2、C3；
4. 每个客户端发送请求；
5. 验证客户端和 S1 上的 request/reply 日志；
6. 验证 S1 在处理前后打印 `my_state`；
7. 终止 S1；
8. 验证 LFD1 在 `heartbeat_freq + read_timeout` 加少量调度误差的时间范围内报告故障；
9. 使用不同的 `heartbeat_freq` 重启整套系统并重复验证。

## 12. 后续里程碑扩展点

- Milestone 2：增加多个 ServerApp 实例、GFD、客户端广播和重复 reply 检测；
- Milestone 3：在 ServerApp 上增加 primary/backup 角色和独立 checkpoint 通道；
- Milestone 4：增加 RM、状态传输、`i_am_ready`、request log 和手动恢复；
- Milestone 5：由 RM 驱动进程自动恢复并持续保持目标副本数。

这些扩展必须复用本设计中的协议版本、传输 framing、配置加载、日志接口和进程边界，不把未来组件的职责提前塞入 Milestone 1。
