# Milestone 2 主动复制演示指南

M2 包含 10 个独立进程：服务器副本 S1–S3、本地故障检测器 LFD1–LFD3、全局故障检测器 GFD，以及客户端 C1–C3。进程通过 TCP 通信。本指南先在一台电脑上完成全部流程；课程的物理部署则把三组服务器和 LFD 分别放在三台机器上。

## 应观察到的行为

1. GFD 启动时输出 `GFD: 0 members`。三个 LFD 可以先注册，但仅注册不会把服务器加入成员表。
2. 依次启动 S1、S2、S3。对应 LFD 首次成功探测服务器后报告健康状态，GFD 的成员数依次变为 1、2、3。
3. 每个客户端先把编号 n 的请求发给所有仍连接的副本，再开始 n+1。每个副本有独立的回复读取任务。客户端只交付第一条有效回复，打印并丢弃后续回复。如果重复回复的计数值不同，还会打印 `replica state divergence` 警告。
4. 停止 S1 后，LFD1 发现故障，GFD 显示 `GFD: 2 members: S2, S3`，三个客户端继续收发请求。稍后停止 S2，GFD 显示 `GFD: 1 member: S3`，三个客户端仍从 S3 获得回复。
5. 一键脚本还检查三个客户端各自的 1–60 号请求恰好交付一次，并检查 S3 的最终状态为 `{"C1": 60, "C2": 60, "C3": 60}`。

M2 不实现 RM、状态转移、checkpoint 或自动恢复。演示期间不要单独重启客户端或故障副本；需要重演时重启整套系统。

## 单机一键运行

在仓库根目录使用 Python 3.11 或更新版本执行：

```bash
python3 scripts/m2_demo.py
```

这个入口调用 `scripts/m2_smoke.py`，无需先安装项目。脚本自动选择七个可用的本机端口，在 `logs/m2-smoke/` 生成运行配置，启动全部 10 个进程，先向 S1 发送 SIGINT、再以 SIGKILL 停止 S2，并验证上述结果。终端显示各阶段摘要，成功时输出 `PASS: evidence saved to ...`。各进程日志和 `result.json` 保存在该目录。成功、失败或按 Ctrl-C 时都会清理尚在运行的子进程。

可自行指定证据目录：

```bash
python3 scripts/m2_demo.py --output /tmp/m2-evidence
```

## 手动运行十个终端

仓库中的[单机配置](../configs/m2.local.toml)使用固定端口：S1–S3 的客户端端口为 5001–5003，心跳端口为 6001–6003，GFD 使用 7000。按下列顺序在十个不同终端各运行一条命令；启动下一台服务器前，先观察前一台已加入 GFD。

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

客户端默认每秒自动发送一次请求。`--interval 0.25` 可调整间隔；`--count 20` 在成功完成 20 次请求后退出；`--interactive` 改为手动输入 `increment` 或直接回车。M1 单服务器客户端同样保留默认自动发送行为。

待 GFD 显示三个成员、客户端打印重复回复后，在 S1 的终端按 Ctrl-C。看到 GFD 移除 S1 后，让客户端继续运行几个请求，再停止 S2。此时应能持续看到 S3 的回复。演示结束后停止其余进程。

## 四台机器的部署

把[分布式示例配置](../configs/m2.distributed.example.toml)复制为实际部署文件，将其中四个示例 `advertised_host` 改为各机器可达的 IP 地址，再把同一份配置复制到全部机器。机器 A 运行 GFD 与 C1–C3；机器 B、C、D 分别运行一组 S/LFD。LFD 必须和对应服务器同机，监听地址设为 `0.0.0.0`，并放行相关端口。使用上一节的单进程命令，指定新的配置路径。单机一键脚本不负责跨机器启动。

## 验证与边界

```bash
python3 -m pytest -q
python3 scripts/m1_smoke.py
python3 scripts/m2_demo.py
```

集成测试覆盖两次先后故障的六种顺序、两台同时故障时的三种幸存副本、迟到的重复回复、最近一次请求的重传，以及 GFD/LFD 的断连处理。M1 smoke 检查原单服务器流程。单机回环测试不能代替四台机器的网络验证。

M1 的状态由每个客户端各自的独立计数器组成，因此不同客户端的 increment 可以交换顺序；副本在处理相同请求集合后收敛。本实现没有为一般状态机建立跨客户端全序。详见[设计说明](DESIGN.md)和[演示清单](DEMO.md)。
