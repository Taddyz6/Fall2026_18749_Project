# Milestone 1 操作指南（中文简版）

**macOS 用户完成环境安装后，可使用[一键打开指令](#方式-a一键打开macos)，同时启动五个终端窗口。**

[英文原版](milestone-1-instructions.md) · [项目首页](../README.md)

本指南沿用英文版的操作顺序。M1 运行 5 个进程：服务端 S1、故障检测器 LFD1，以及客户端 C1、C2、C3。客户端各自累加自己的计数，心跳不改变计数。

**所有命令都在仓库根目录执行，不要进入 `docs/`。需要 Python 3.11 或更新版本。**

## 1. 安装环境（首次运行）

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## 2. 先运行自动检查

依次执行，等待上一条结束：

```bash
.venv/bin/python -m pytest -v
.venv/bin/python scripts/m1_smoke.py
```

预期：测试全部通过，第二条命令最后显示 `SMOKE PASS`。它会自动检查三个客户端各发送一次后的状态 `{"C1": 1, "C2": 1, "C3": 1}`，以及心跳和服务端故障检测。

## 3. 本机演示：打开 5 个终端

### 方式 A：一键打开（macOS）

完成第 1、2 步后，在**仓库根目录**将下面整段一次性粘贴到终端执行。它会用 macOS 自带的 Terminal 打开 5 个窗口，自动进入项目、激活环境，并分别启动 S1、LFD1、C1、C2、C3；窗口标题会标出进程名称。

**只执行一次；如果已有演示进程在运行，先在对应窗口按 Ctrl-C 停止。** 首次运行若系统询问是否允许控制 Terminal，选择允许。

```bash
osascript - "$PWD" <<'APPLESCRIPT'
on run argv
    set projectRoot to item 1 of argv
    set jobs to {¬
        {"S1", "ft-server --config configs/local.toml --server-id S1"}, ¬
        {"LFD1", "ft-lfd --config configs/local.toml --lfd-id LFD1"}, ¬
        {"C1", "ft-client --config configs/local.toml --client-id C1"}, ¬
        {"C2", "ft-client --config configs/local.toml --client-id C2"}, ¬
        {"C3", "ft-client --config configs/local.toml --client-id C3"}}
    tell application "Terminal"
        repeat with job in jobs
            set launchCommand to "cd " & quoted form of projectRoot & " && source .venv/bin/activate && " & item 2 of job
            set demoTab to do script launchCommand
            set custom title of demoTab to "M1 - " & item 1 of job
        end repeat
        activate
    end tell
end run
APPLESCRIPT
```

打开后，按下面顺序检查：

1. **看正常运行：**三个客户端持续出现 `Sending` / `Received`，S1 的计数不断增加，LFD1 持续收到心跳回复。若启动瞬间出现连接失败，等待客户端自动重试。
2. **测试故障：**只在 **M1 - S1** 窗口按 Ctrl-C；LFD1 应显示 `S1 has died`，客户端继续重试。
3. **测试恢复：**在原来的 S1 窗口执行 `ft-server --config configs/local.toml --server-id S1`；客户端应恢复发送，服务端从零重新计数。
4. **结束演示：**在这 5 个窗口分别按 Ctrl-C，再关闭窗口。

完成一键启动后，**跳过下面的手动启动**。调整发送模式见第 4 步，完整故障测试说明见第 5 步。

### 方式 B：手动打开

每个终端都进入仓库根目录，按下面顺序启动。启动后保留终端运行，不要关闭。

**终端 1：服务端 S1**

```bash
source .venv/bin/activate
ft-server --config configs/local.toml --server-id S1
```

**终端 2：故障检测器 LFD1**

```bash
source .venv/bin/activate
ft-lfd --config configs/local.toml --lfd-id LFD1
```

预期：持续出现 `LFD1 sending heartbeat to S1` 和 `LFD1 receives heartbeat from S1`。

**终端 3：客户端 C1**

```bash
source .venv/bin/activate
ft-client --config configs/local.toml --client-id C1
```

**终端 4：客户端 C2**

```bash
source .venv/bin/activate
ft-client --config configs/local.toml --client-id C2
```

**终端 5：客户端 C3**

```bash
source .venv/bin/activate
ft-client --config configs/local.toml --client-id C3
```

## 4. 观察发送结果，按需调整模式

客户端启动后**立即自动发送，不需要按回车**。每次请求完成或失败后等待 1 秒，再发下一次；每个客户端同一时间只处理一个请求。

预期：客户端持续显示 `Sending` / `Received`；终端 1 持续显示 `my_state_S1` 的变化。三个计数会不断增加，不要求相等，也不会停在全为 1 的状态。

**调整间隔：**先在对应客户端终端按 Ctrl-C 停止，再执行：

```bash
ft-client --config configs/local.toml --client-id C1 --interval 0.5
```

`--interval` 的单位为秒，必须是有限的正数；它表示每次尝试后的等待时间。C2、C3 替换对应的 `--client-id` 即可。

**可选：手动发送。**先停止对应的自动客户端，再执行：

```bash
ft-client --config configs/local.toml --client-id C1 --interactive
```

输入 `increment` 或直接按回车发送一次，输入 `quit` / `exit` 退出。此模式不使用 `--interval`。

若要验证精确计数，先停止三个客户端并重启 S1，再将 C1、C2、C3 全部以手动模式启动。每个客户端发送一次后，服务端应显示 `{"C1": 1, "C2": 1, "C3": 1}`；每次请求只改变发送方自己的计数。

## 5. 验证故障检测和重连

1. 保持 LFD1 和三个自动客户端运行。
2. 在**终端 1（S1）**按 Ctrl-C，停止服务端。
3. 查看**终端 2（LFD1）**：下次心跳尝试失败后，应显示 `S1 has died`。每次从正常变为故障只报告一次；错误原因可能是连接关闭、超时或其他 TCP 错误。
4. 客户端会输出连接错误并按间隔重试。在终端 1 重新运行第 3 步的 S1 启动命令，客户端应自动重连并继续发送。

注意：S1 重启后计数清零。当前没有请求去重，若请求已被处理但回复丢失，重试可能重复累加。

## 6. 比较不同心跳间隔

停止 S1 和 LFD1，先重新启动 S1，再在终端 2 **任选一条**启动 LFD1：

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 0.5
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 1.0
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 2.0
```

`--heartbeat-freq` 的单位是**秒，不是 Hz**。间隔越短，检查越频繁。每次更换参数都先停止旧的 LFD1，再启动新的；重复第 5 步，对比故障检测表现。

## 7. 可选：多机运行

每台机器都需要项目代码和第 1 步的环境。先准备配置；如果已有 `configs/distributed.toml`，直接编辑，避免覆盖：

```bash
cp configs/distributed.example.toml configs/distributed.toml
```

在配置的 `[server.S1]` 中，将 `advertised_host` 改为运行 S1 的机器的局域网 IP，并设置：

```toml
bind_host = "0.0.0.0"
```

把同一份配置复制到所有机器。机器 A 运行 S1、LFD1；机器 B 运行 C1、C2、C3。

沿用第 3 步的五条启动命令，将每条命令中的 `configs/local.toml` 替换为 `configs/distributed.toml`。放行配置中 `client_port` 和 `heartbeat_port` 对应的入站 TCP 端口。

## 排障和结束

| 现象 | 检查方式 |
| --- | --- |
| `Address already in use` | 停止占用端口的旧进程，或修改配置中的端口 |
| `Connection refused` | 确认 S1 已启动，IP 和端口正确 |
| 心跳超时 | 确认 LFD1 连接的是 `heartbeat_port` |
| 客户端超时 | 确认客户端连接的是 `client_port` |

演示结束后，在所有仍运行的终端按 Ctrl-C。

## 最后检查

- [ ] pytest 全部通过，smoke test 显示 `SMOKE PASS`。
- [ ] S1 正常启动，LFD1 持续收到心跳回复。
- [ ] 三个客户端无需输入即可连续发送，各自只改变自己的计数；心跳不改变计数。
- [ ] `--interval` 能调整发送间隔，Ctrl-C 能停止客户端，`--interactive` 能手动发送和退出。
- [ ] 停止 S1 后，LFD1 只报告一次故障；重启 S1 后，自动客户端恢复发送。
- [ ] `--heartbeat-freq` 能调整心跳间隔。
