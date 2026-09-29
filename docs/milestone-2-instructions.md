# Milestone 2: active replication demonstration

M2 uses three active server replicas (S1–S3), three local fault detectors (LFD1–LFD3), one global fault detector (GFD), and three clients (C1–C3). The **10 processes should have 10 visible terminal windows** during the course demonstration. This guide first runs them on one macOS computer; the physical deployment places each server/LFD pair on a separate machine.

## Live acceptance sequence

1. Start GFD and show `GFD: 0 members`. Start LFD1–3. Registration alone must not add a server. Start S1, S2, and S3 **one at a time**; after each successful local heartbeat, GFD should show 1, 2, then 3 members.
2. Start C1–3. Each client keeps a connection to every live replica and sends the same request number to all of them before advancing. All three replicas process and reply. The client delivers the first valid reply, prints and discards later duplicates, and warns if a duplicate counter value differs.
3. **Manually press Ctrl-C in the S1 window.** LFD1 reports the failed heartbeat and deletion in its own window. GFD shows `GFD: 2 members: S2, S3`. Clients continue sending and receiving without pausing.
4. After several more requests, **manually press Ctrl-C in the S2 window**. GFD shows `GFD: 1 member: S3`. Clients continue to receive replies from S3.

The clients run automatically in an infinite loop for this live demo. A general M2 note defers client automation, but the numbered acceptance steps specifically require automatic requests and continued traffic during failures. The live demo has **no 60-request limit** and does **not** stop replicas for you.

M2 does not include a Replication Manager, checkpointing, state transfer, or automatic replica recovery. Do not restart an individual replica or client during this run; restart the whole system for a fresh demo.

## One-command visual launcher on macOS

From the repository root, with Python 3.11 or newer:

```bash
python3 scripts/m2_demo.py
```

The launcher follows the [M1 five-window approach](milestone-1-instruction_zh.md): it uses macOS Terminal to open a separate titled window for GFD, each LFD, each server, and each client. It waits for GFD membership to grow 0 → 1 → 2 → 3 before launching C1–3. All output remains visible in the corresponding windows. On the first run, allow the macOS request to control Terminal. Python package installation is not required for this command.

The launcher uses fixed loopback ports from [configs/m2.local.toml](../configs/m2.local.toml): server client ports 5001–5003, server heartbeat ports 6001–6003, and GFD port 7000. Stop any previous M1/M2 processes using those ports before launching. The command refuses to open another set of windows when a port is occupied.

The client delay defaults to one second. To use a different delay:

```bash
python3 scripts/m2_demo.py --interval 0.5
```

When the windows are ready, follow the manual Ctrl-C sequence above. To end the demo, press Ctrl-C in each remaining process window. The launcher exits after opening the windows; it deliberately leaves the processes running for observation.

## Manual ten-terminal alternative

Open ten terminals yourself and run one command per terminal in this order. Wait for each membership addition before starting the next server:

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

Clients send automatically by default; `--interval 0.25` changes the delay, `--interactive` enables manual input, and `--count 20` stops after 20 successful requests. Use the default infinite loop for acceptance.

## Separate automated regression test

```bash
python3 -m pytest -q
python3 scripts/m1_smoke.py
python3 scripts/m2_smoke.py
```

`m2_smoke.py` is **headless** and automatically injects S1 and S2 failures. It uses available loopback ports, verifies that each client delivers requests 1–60 exactly once, checks S3's final state, writes process logs and `result.json` to `logs/m2-smoke/`, then stops all child processes. The 60 requests are a repeatable test condition, not a course requirement. Run this check separately from the live visual demo. Pytest must be installed for the first command; the scripts use the standard library.

## Four-machine deployment

Copy [the distributed example](../configs/m2.distributed.example.toml) to a deployment-specific TOML file. Replace the four example advertised addresses with reachable machine IP addresses, then copy the same completed file to every machine. Put GFD and C1–3 on machine A; put each S/LFD pair on machines B, C, and D. Keep each LFD beside its server, bind listeners to `0.0.0.0`, and allow the configured ports through firewalls. Run the manual component commands with the deployment config path. The one-command visual launcher is for a single macOS computer.

Independent per-client counters converge once surviving replicas process the same unique requests; this design does not impose a total order across clients. See [design details](DESIGN.md) and the [demo checklist](DEMO.md).
