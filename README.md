# 18-749 Fault-Tolerant System

A Python client-server application for the 18-749 distributed systems milestones.

## Milestone 2

M2 runs **10 independent processes**: three active replicas (S1–S3), three local fault detectors (LFD1–LFD3), one global fault detector (GFD), and three clients (C1–C3). Each client continuously broadcasts the same numbered increment to every connected replica, delivers the first valid reply, and prints and discards duplicates. A duplicate with a different counter value also produces a `replica state divergence` warning.

Each LFD reports its replica's health to the GFD. GFD starts with zero members, adds replicas after successful local heartbeats, and removes them after failures.

## Visual demo: ten Terminal windows on macOS

From the repository root, with Python 3.11 or newer:

```bash
python3 scripts/m2_demo.py
```

Like the [M1 five-window launcher](docs/milestone-1-instruction_zh.md), this opens **one visible Terminal window per process** and labels it `M2 - GFD`, `M2 - LFD1`, and so on. It waits for GFD membership to grow 0 → 1 → 2 → 3 before starting the three clients. The windows stay open, and clients send automatically in an infinite loop. The first run may prompt for permission to control Terminal.

For the live fault demonstration, **press Ctrl-C yourself** in the S1 window. Watch LFD1 report the failure, GFD show only S2 and S3, and all clients continue. After several more requests, press Ctrl-C in S2; GFD should show only S3 while clients keep receiving replies. Stop the remaining processes in their own windows when finished. `--interval 0.5` changes the client delay; the default is one second. The visual launcher uses the fixed ports in `configs/m2.local.toml` and refuses to start if they are occupied.

The [English M2 guide](docs/milestone-2-instructions.md) and [Chinese M2 guide](docs/milestone-2-instructions_zh.md) give the exact observation checklist and manual commands.

## Automated regression check

```bash
python3 scripts/m2_smoke.py
```

This separate, headless test chooses available loopback ports, launches ten child processes, automatically stops S1 and S2, and checks membership, duplicate replies, client progress, and S3 state. It finishes after each client delivers requests 1–60 exactly once, then cleans up. Per-process logs, the generated config, and `result.json` are saved in `logs/m2-smoke/`; use `--output PATH` to select another directory. The **60-request limit belongs to this test**, not to the live M2 demo or the course requirement.

## Client and replica behavior

Clients send automatically by default in both M1 and M2 configurations; `--interactive` enables manual requests. After a replica connection fails, an M2 client continues with the survivors and does not reconnect that replica during the run. Restart the **whole system** for a fresh M2 demonstration: M2 does not transfer state to a restarted replica.

Each replica preserves the M1 per-client counter state machine. It remembers the last reply for each client so retrying that client's latest request does not increment twice. Independent client counters converge after every surviving replica has processed the same requests. This design does not provide a general cross-client total order.

## Verify

```bash
python3 -m pytest -q
python3 scripts/m1_smoke.py
python3 scripts/m2_smoke.py
```

The tests need pytest installed; the two smoke scripts and visual launcher use the Python standard library. Local loopback testing does not establish that the course's four-machine network setup works. See [design notes](docs/DESIGN.md), the [demo checklist](docs/DEMO.md), and the [distributed config example](configs/m2.distributed.example.toml).

The M1 single-server configuration remains at `configs/local.toml`. Its client still sends automatically by default; the [M1 guide](docs/milestone-1-instructions.md) documents that setup.

## Project structure

```text
src/ft_system/     protocol, replicas, clients, LFDs, GFD
configs/           M1 local, M2 local, and distributed examples
scripts/           M1 smoke, M2 visual launcher, M2 smoke
tests/             unit and integration tests
docs/              milestone guides, design, and demo checklist
```
