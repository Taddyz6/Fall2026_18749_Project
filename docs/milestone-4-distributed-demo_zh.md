# Milestone 4：四台电脑演示指南

本指南在四台电脑上演示 Milestone 4 的两种配置：

- **主动复制 + 人工恢复**：停止一个副本，再手动重新启动它。它先以 `i_am_ready = 0` 记录请求但不回复，收到健康副本的 checkpoint 后才变为就绪。
- **被动复制 + 人工恢复**：停止并重启一个 backup；停止 primary，观察 RM 选出新的 primary。

本指南还包括演示前的检查、演示中出问题时的排查方法，以及演示后的日志收集。完整的故障对照表见 [demo-troubleshooting.md](demo-troubleshooting.md)，设计说明见 [milestone-4-instructions.md](milestone-4-instructions.md)。

## 1. 电脑分工

| 电脑 | 运行的进程 | 窗口数 |
| --- | --- | ---: |
| A（sacred） | RM、GFD、C1、C2、C3 | 5 |
| B | LFD1、S1 | 2 |
| C | LFD2、S2 | 2 |
| D | LFD3、S3 | 2 |

共 **11 个进程、11 个窗口**。每个进程使用自己的终端窗口，不要把多条命令贴进同一个窗口。四台电脑必须在同一个能互相访问的网络中。访客 Wi-Fi 和部分校园网络会阻止设备之间的连接，第 4 节的 doctor 能检测出这种情况。

建议先在一台电脑上排练：`python scripts/m4_demo.py --autopilot` 会在同一块屏幕上打开全部 11 个窗口，并自动完成主动复制的演示（被动复制加 `--mode passive`，关闭所有窗口用 `--stop`）。

## 2. 准备每台电脑

在**每台电脑**的项目根目录执行一次：

```bash
git fetch origin
git switch m4
git pull
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

之后每打开一个新终端，先执行：

```bash
cd /path/to/Fall2026_18749_Project
source .venv/bin/activate
```

四台电脑必须使用同一个提交。doctor 和每个进程启动时都会打印 `build <提交号>`，漏了 `git pull` 的电脑一眼就能看出来。

> 如果有队员在 2026 年 11 月 1 日之前已经获取过 `m4`，请在 `m4` 分支上执行一次 `git fetch origin && git reset --hard origin/m4`。该分支当天被重写过。

## 3. 创建并共享配置

1. 在每台电脑上查出它的 Wi-Fi IPv4 地址并记下来：

   ```bash
   ipconfig getifaddr en0     # macOS（若无输出，试 en1）
   ```

2. 在电脑 **A** 上同时修改 `configs/m4.active.distributed.toml` 和 `configs/m4.passive.distributed.toml` **两个文件**。只改 `advertised_host`，所有 `bind_host = "0.0.0.0"` 保持不变。

   | 配置节 | `advertised_host` 填写 |
   | --- | --- |
   | `[rm]` 和 `[gfd]` | A 的 IP |
   | `[server.S1]` | B 的 IP |
   | `[server.S2]` | C 的 IP |
   | `[server.S3]` | D 的 IP |

   如果习惯从本机配置复制，请从 `configs/m4.active.local.toml` 开始，并把所有 `bind_host` 改成 `"0.0.0.0"`。漏改的地方 doctor 会指出来。

3. 把这两个文件**原样**复制到 B、C、D（AirDrop、`scp` 或 U 盘均可）。不要在每台电脑上分别手工修改。

4. 在每台电脑的防火墙中允许以下端口的 TCP 入站连接：

   | 电脑 | 端口 |
   | --- | --- |
   | A | 7300（RM）、7200（GFD） |
   | B | 5201、6201、8201 |
   | C | 5202、6202、8202 |
   | D | 5203、6203、8203 |

## 4. 演示前检查：doctor

在**四台电脑上同时**启动 doctor：

```bash
PYTHONPATH=src python -m ft_system.doctor --config configs/m4.active.distributed.toml
```

每个 doctor 会判断自己在哪台电脑上，监听这台电脑真实使用的端口，并持续连接这台电脑上各进程要用到的每个端口。让四个 doctor 一直运行，其他电脑的 doctor 启动后，失败项会自动变成 `[OK]`。电脑 B 的输出类似：

```text
[INFO] build a610a44, config 2dacac23 (m4.active.distributed.toml, active); every computer must show the same two values
[INFO] this computer is 172.26.97.249 and runs S1, LFD1
[OK]   listening on 3 port(s) that S1 will use
[OK]   S1 -> RM port 172.26.36.169:7300: reached RM, GFD, C1, C2, C3
[OK]   S1 -> S2 checkpoint port 172.26.75.34:8202: reached S2, LFD2
[OK]   LFD1 -> GFD port 172.26.36.169:7200: reached RM, GFD, C1, C2, C3
...
PASS: this computer can reach everything its processes need, with the same code and config. ...
```

**四台电脑都**显示 `PASS` 后，在每台电脑上按 Ctrl-C。出现 `[FAIL]` 时，下面缩进的几行会说明怎么修：

| doctor 显示 | 处理方法 |
| --- | --- |
| `none of the config's addresses ... belongs to this computer` | 这台电脑的 IP 变了。按它打印的地址在 A 上修正配置，再重新复制到各电脑 |
| `connection refused` | 在那台电脑上启动 doctor；若已在运行，说明那台电脑的防火墙拒绝了该端口 |
| `no answer within 4 s` | IP 错误、不在同一网络，或 Wi-Fi 阻止设备互访。先核对 IP；若跨电脑的检查全部超时，改用手机热点 |
| `cannot listen on ...` | 上一次运行的进程还占着端口。执行它打印的 `lsof` 命令，再 `kill` 该进程 |
| `MISMATCH` | 代码提交或配置不同。比较各电脑第一行 `[INFO]` 中的 `build`/`config`，修正与其他电脑不一致的那一台 |

在 macOS 上，如果弹窗询问是否允许 Python 接受传入的网络连接，请点**允许**。在一个空闲终端中运行 `caffeinate -dis`，防止电脑休眠。

## 5. 启动顺序

每个进程启动后先打印一行：`build ..., config ... (m4.active.distributed.toml, active)`。11 个窗口的这两个值应该完全相同。

**电脑 A**：窗口 1 启动 RM，窗口 2 启动 GFD：

```bash
PYTHONPATH=src python -m ft_system.rm.main  --config configs/m4.active.distributed.toml
PYTHONPATH=src python -m ft_system.gfd.main --config configs/m4.active.distributed.toml
```

**电脑 B、C、D**：先启动 LFD：

```bash
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m4.active.distributed.toml --lfd-id LFD1   # B
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m4.active.distributed.toml --lfd-id LFD2   # C
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m4.active.distributed.toml --lfd-id LFD3   # D
```

然后**逐个**启动副本，每次等 RM 显示 `RM: N members` 后再启动下一个：

```bash
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S1   # B
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S2   # C
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S3   # D
```

**电脑 A**：RM 显示 `RM: 3 members: S1, S2, S3` 后，在窗口 3–5 启动客户端：

```bash
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C1 --interval 1
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C2 --interval 1
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C3 --interval 1
```

被动复制演示时，所有命令都改用 `configs/m4.passive.distributed.toml`。

## 6. 演示流程和检查项

### 主动复制

1. **RM**：`RM: 0 members`。
2. **GFD**：`GFD: 0 members` 和 `GFD: notified RM: 0 members`。**RM** 打印 `RM: GFD registered`，并再次打印 `RM: 0 members`。
3. **LFD1–3**：GFD 依次打印 `GFD: registered LFD1`、`LFD2`、`LFD3`。副本启动前，成员数保持为 0。
4. **S1**：打印 `S1: i_am_ready = 0 (waiting for the RM)`。LFD1 打印 `LFD1: add replica S1`，接着 GFD 打印 `GFD: 1 member: S1`，RM 打印 `RM: 1 member: S1`。RM 打印 `S1 is the first replica; asking it to set i_am_ready = 1`，S1 打印 `S1: i_am_ready = 1 (set by the RM)`。
5. **S2、S3**：GFD 和 RM 依次显示 2、3 个成员。每个副本加入约 2 秒后，RM 打印 `asking S1 to transfer its state to S2`。S1 打印 `secondary channel to S2 open` 和 `S1 sending checkpoint <my_state = ..., checkpoint_count = 1> to S2`。S2 打印 `received checkpoint` 和 `i_am_ready = 1 (checkpoint installed)`。
6. **C1–3**：每个客户端把每个请求发给 S1、S2、S3，交付最先到达的回复，并且每个请求打印两次 `Discarded duplicate reply`。
7. **注入故障**：在电脑 C 的 S2 窗口按 Ctrl-C。LFD2 打印 `S2 has died` 和 `LFD2: delete replica S2`。GFD 和 RM 显示 `2 members: S1, S3`。客户端继续运行，每个请求只剩一个重复回复。
8. **人工恢复**：在 S2 窗口按 ↑ 再按回车，重新运行同一条命令。
   - LFD2 打印 `S2 has recovered` 和 `add replica S2`，GFD 和 RM 重新显示 3 个成员。
   - 客户端打印 `reconnected to S2`。
   - 约 2 秒内，S2 每收到一个请求打印 `Received <C1, S2, n, request>`，接着打印 `S2: i_am_ready = 0; logged <C1, S2, n, request> without replying (high_watermark_request_num[C1] = n)`。这就是 rubric 第 21 步。
   - 随后 S1 发来 checkpoint。S2 打印 `i_am_ready = 1 (checkpoint installed)` 和 `replaying K logged requests`，之后恢复正常服务。客户端重新变为每个请求两个重复回复。
9. 在 **RM 窗口按回车**，展示状态快照：成员、就绪副本，以及已注册的组件。

演示结束后，在 11 个窗口中逐一按 Ctrl-C。

### 被动复制

先停止全部 11 个进程，再按相同顺序、改用 `configs/m4.passive.distributed.toml` 重新启动。启动行此时应显示 `passive`；如果有进程仍在使用主动复制的配置，RM 会打印红色的 `MISMATCH`。

1. 启动后：RM 打印 `RM: primary is S1`，S1 打印 `S1: role = primary`。S2、S3 打印 `role = backup (primary is S1)`，并在收到 S1 的第一个 checkpoint 后变为就绪。
2. 客户端的所有回复都来自 S1（`Received <C1, S1, ...>`），S2、S3 从不回复。S1 每秒打印 `S1 sending checkpoint <my_state = ..., checkpoint_count = N> to S2`（S3 同理），backup 打印每次的 `received checkpoint`。
3. **backup 故障与恢复**：在 S2 窗口按 Ctrl-C。成员变为 `S1, S3`，S1 继续回复客户端，并继续向 S3 发送 checkpoint。重新启动 S2：它以 backup 身份、`i_am_ready = 0` 加入，收到 S1 的下一个 checkpoint 后变为就绪。
4. **primary 故障与重新选举**：在 S1 窗口按 Ctrl-C。
   - RM 打印 `primary S1 failed; new primary is S3`。新 primary 是最早加入且已就绪的 backup；S2 刚重启过，所以选中的是 S3。如果希望由 S2 接任，就先做第 4 步、再做第 3 步。
   - 新 primary 打印 `role = primary`，它的 `checkpoint_count` 从收到的最后一个编号继续往下编。
   - 客户端打印 `primary changed S1 -> S3`，并用同一个 `request_num` 重发进行到一半的请求。
5. 重新启动 S1。它打印 `role = backup (primary is S3)`，收到 S3 的 checkpoint 后变为就绪。

### 给项目经理的讲解要点

- **分工**：GFD 负责上报成员变化；RM 决定哪些副本就绪、谁是 primary，并把这个视图推送给所有副本和客户端。
- **恢复时既不丢请求也不重复**：副本只有收到 checkpoint 后才变为就绪。checkpoint 包含 `my_state`、`checkpoint_count` 和每个客户端最后的 `request_num`，所以新副本只重放 checkpoint 尚未包含的已记录请求。
- **被动复制的取舍**：旧 primary 在最后一次 checkpoint 之后处理的请求会丢失，最多相当于一个 `checkpoint_freq` 的时间。新 primary 会打印丢失了哪些请求编号。

## 7. 演示中出问题时

**在任意进程窗口按回车**，会打印一段 `STATUS`。以 `hint:` 开头的行说明最可能的原因。先看 **RM 窗口**，因为它能看到整个系统：

| 现象 | RM 状态显示 | 处理方法 |
| --- | --- | --- |
| 某个副本一直 `i_am_ready = 0` | `hint: S2 is alive ... but never registered with the RM` | S2 所在电脑连不上 A:7300（防火墙或 IP） |
| 同上 | `hint: S2 is running but is not a member` | LFD2 未运行，或收不到 S2 的心跳回复 |
| 同上 | `S2 waiting for state from S1: sent 3 time(s)` | 在 S1 窗口找 `checkpoint to S2` 报错；电脑 C 的 8202 端口被拦截 |
| `RM: N members` 一直不变 | `GFD NOT connected` | 先启动 RM，再启动 GFD |
| 红色的 `RM ERROR: MISMATCH: ...` | `MISMATCH S2 runs build ...` | 那台电脑的代码或配置不同：停止该进程，`git pull` 或重新复制配置后再启动 |
| 被动复制下客户端一直不打印 `primary changed` | 显示当前 primary 和就绪副本 | 在客户端窗口按回车；若显示 `RM NOT connected`，说明该客户端连不上 RM |

用 `--interactive` 启动的客户端要从窗口读取命令，因此在那里按回车不会打印状态。

## 8. 演示后收集日志

每个进程会把窗口输出同时写入 `logs/console/<日期>/<进程名>.log`，启动后第二行会显示具体路径。排练失败后：

1. 把每台电脑的 `logs/console/<日期>/` 复制到同一台电脑上，分别放进 `run1/A`、`run1/B`、`run1/C`、`run1/D` 这样的文件夹。
2. 合并成一条时间线：

   ```bash
   python scripts/merge_logs.py run1 --since 14:05 --events        # 只看事件，隐藏请求/回复流量
   python scripts/merge_logs.py run1 --grep "S2|RM:"                # 只看某个副本相关的经过
   ```

输出是纯文本，可以直接贴到聊天或问题报告里。

## 9. 常见问题

- **`Address already in use`**：上一次运行的进程还在。用 `lsof -nP -iTCP:<端口> -sTCP:LISTEN` 找到它后 `kill`，或者关闭它的窗口。
- **某台电脑的 IP 变了**（休眠或 Wi-Fi 重连后）：在 A 上修改两个配置中对应的地址，复制到所有电脑，重启全部进程，再运行一次 doctor。
- **单机能跑通，四台电脑跑不通**：运行 doctor。四台电脑演示中的问题几乎都出在网络、防火墙、IP、代码提交或配置上，doctor 会逐项报告并给出处理方法。
- **Windows PowerShell**：把 `PYTHONPATH=src python ...` 换成 `$env:PYTHONPATH = "src"; python ...`。
