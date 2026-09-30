# Milestone 2 主动复制现场演示指南

M2 有 10 个独立进程：服务器副本 S1–S3、本地故障检测器 LFD1–LFD3、全局故障检测器 GFD，以及客户端 C1–C3。**验收时用 10 个可见的终端窗口，一进程一窗口**，最容易展示各角色的请求、回复、心跳、成员变化和去重信息。本指南先在一台 macOS 电脑上运行，供本地调试和观察；**四机现场部署**请直接使用[独立中文教程](milestone-2-distributed-deployment_zh.md)：一台运行 GFD 与 C1–C3，另外三台分别运行 S1/LFD1、S2/LFD2、S3/LFD3。

## 现场验收顺序

1. 启动 GFD，展示 `GFD: 0 members`；启动 LFD1–3。仅注册 LFD 不应把服务器加入成员表。再**依次**启动 S1、S2、S3，每次本地心跳成功后，GFD 的成员数应依次变为 1、2、3。
2. 启动 C1–3。每个客户端向所有仍连接的副本发送相同编号的请求，三个副本各自处理并回复。客户端交付第一条有效回复，打印并丢弃后续重复回复；若重复回复的计数值不一致，还会输出 `replica state divergence` 警告。
3. **亲手在 S1 窗口按 Ctrl-C。** LFD1 在自己的窗口报告心跳失败及删除通知；GFD 显示 `GFD: 2 members: S2, S3`。三个客户端在整个故障过程中不停顿，继续发送并接收。
4. 再运行一段时间后，**亲手在 S2 窗口按 Ctrl-C。** GFD 显示 `GFD: 1 member: S3`；三个客户端仍能收到 S3 的回复。

现场演示的客户端应当**自动、无限循环**发送请求。虽然 M2 概述说暂不必实现自动循环，但编号验收步骤明确要求自动发送、故障期间不暂停，因此现场按具体步骤准备。**现场没有“每个客户端恰好 60 次”的限制，也不会由脚本自动停止副本。**

M2 不要求 RM、checkpoint、状态转移或自动恢复。演示期间不要单独重启客户端或故障副本；要重新演示时重启整套系统。

## macOS 一键打开 10 个窗口

在仓库根目录使用 Python 3.11 或更新版本执行：

```bash
python3 scripts/m2_demo.py
```

脚本沿用[M1 的五窗口方式](milestone-1-instruction_zh.md)：通过 macOS Terminal 为 GFD、每个 LFD、每个服务器、每个客户端打开独立窗口，并将标题标为 `M2 - GFD`、`M2 - S1` 等。脚本先等待 GFD 成员数按 0 → 1 → 2 → 3 增长，再启动三个客户端。所有输出在对应窗口实时可见。首次运行如出现“允许控制 Terminal”的系统提示，请选择允许。本命令无需先安装 Python 项目包。

启动器使用[单机配置](../configs/m2.local.toml)的固定端口：客户端请求 5001–5003、服务器心跳 6001–6003、GFD 7000。若旧的 M1/M2 进程仍占用端口，先在其窗口停止；脚本会拒绝重复打开一套有端口冲突的窗口。

客户端默认每次请求后等待 1 秒；可调整为：

```bash
python3 scripts/m2_demo.py --interval 0.5
```

10 个窗口准备好后，按上一节**人工按 Ctrl-C**的顺序演示。结束时，在剩余各进程窗口按 Ctrl-C。启动器打开窗口后就退出，进程会继续运行，方便现场观察。

## 手动打开十个终端

也可以自己打开十个终端，在每个终端运行下列一条命令。启动下一台服务器前，先观察前一台已加入 GFD：

```bash
PYTHONPATH=src python3 -m ft_system.gfd.main --config configs/m2.local.toml
PYTHONPATH=src python3 -m ft_system.lfd.main --config configs/m2.local.toml --lfd-id LFD1
PYTHONPATH=src python3 -m ft_system.lfd.main --config configs/m2.local.toml --lfd-id LFD2
PYTHONPATH=src python3 -m ft_system.lfd.main --config configs/m2.local.toml --lfd-id LFD3
PYTHONPATH=src python3 -m ft_system.server.main --config configs/m2.local.toml --server-id S1
PYTHONPATH=src python3 -m ft_system.server.main --config configs/m2.local.toml --server-id S2
PYTHONPATH=src python3 -m ft_system.server.main --config configs/m2.local.toml --server-id S3
PYTHONPATH=src python3 -m ft_system.client.main --config configs/m2.local.toml --client-id C1
PYTHONPATH=src python3 -m ft_system.client.main --config configs/m2.local.toml --client-id C2
PYTHONPATH=src python3 -m ft_system.client.main --config configs/m2.local.toml --client-id C3
```

客户端默认自动发送；`--interval 0.25` 可调整间隔，`--interactive` 可切换为手动输入，`--count 20` 会在成功完成 20 次请求后退出。**现场验收使用默认无限循环模式。**

## 单独运行自动回归测试

```bash
python3 -m pytest -q
python3 scripts/m1_smoke.py
python3 scripts/m2_smoke.py
```

`m2_smoke.py` 是**无界面自动检查**：自动选择可用的本机端口、启动十个子进程、依次注入 S1/S2 故障，验证每个客户端恰好交付 1–60 号请求以及 S3 的最终状态，把进程日志和 `result.json` 写入 `logs/m2-smoke/`，然后清理子进程。60 次仅是可重复测试的条件，**不是课程要求**。它与现场十窗口演示分开运行。第一条命令需安装 pytest；脚本本身只用标准库。

## 四台机器部署

请按[Milestone 2 四台电脑分布式部署教程](milestone-2-distributed-deployment_zh.md)操作。教程逐步说明如何确认四台电脑的 IP、制作并同步同一份配置、检查连接与端口、在各机器的十个窗口依次启动进程，以及人工停止 S1/S2。上面的 `m2_demo.py` 仅负责单机启动，不能在另外三台电脑远程打开窗口。

不同客户端的独立计数器可以交换处理顺序；幸存副本处理相同请求集合后状态收敛。本实现没有建立跨客户端的通用全序。详见[设计说明](DESIGN.md)和[演示清单](DEMO.md)。
