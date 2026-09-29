# 18-749 Fault-Tolerant System

A Python client-server application for the 18-749 distributed systems milestones.

## Milestone 2

M2 runs **10 independent processes**: three active replicas (S1–S3), three local fault detectors (LFD1–LFD3), one global fault detector (GFD), and three clients (C1–C3). Each client broadcasts the same numbered increment to every connected replica, delivers the first valid reply, and logs later replies as duplicates. A mismatched duplicate value also produces a `replica state divergence` warning.

Each LFD reports its replica's health to the GFD. The GFD starts with zero members, adds replicas after successful local heartbeats, and removes them after failures. The clients continue with the surviving replicas when S1 and then S2 fail.

## One-command local demonstration

Run from the repository root with Python 3.11 or newer:

```bash
python3 scripts/m2_demo.py
```

This command needs no package installation. It runs the same scenario as `scripts/m2_smoke.py`, allocates seven available loopback ports, starts all 10 processes, and injects an S1 SIGINT followed by an S2 SIGKILL. It checks membership 0 → 1 → 2 → 3 → 2 → 1, duplicate replies, requests 1–60 delivered exactly once by each client, and S3's final state of `{"C1": 60, "C2": 60, "C3": 60}`. It cleans up child processes and writes per-process logs, the generated config, and `result.json` to `logs/m2-smoke/`. Use `--output PATH` to choose another evidence directory.

For a live demonstration in ten terminals, follow the [English M2 guide](docs/milestone-2-instructions.md) or [Chinese M2 guide](docs/milestone-2-instructions_zh.md). The [design notes](docs/DESIGN.md) describe request ordering and fault handling. `configs/m2.distributed.example.toml` shows the four-machine layout.

## Client and replica behavior

Clients send automatically by default in both M1 and M2 configurations; `--interactive` enables manual requests. `--count N` stops after N successful requests, which is useful for repeatable tests. After a replica connection fails, an M2 client continues with the remaining replicas and does not reconnect that replica during the run. Restart the **whole system** for a fresh demonstration: M2 does not transfer state to a restarted replica.

Each replica preserves the M1 per-client counter state machine. It remembers the last reply for each client so a retry of that client's latest request does not increment twice. Independent client counters converge after every surviving replica has processed the same requests. This design does not provide a general cross-client total order.

## Verify

```bash
python3 -m pytest -q
python3 scripts/m1_smoke.py
python3 scripts/m2_demo.py
```

The tests need pytest installed; the two demonstration scripts use the Python standard library. The one-computer run verifies process crashes over loopback. The course's four-machine setup still needs a separate network test.

The M1 single-server configuration remains at `configs/local.toml`. Its client still sends automatically by default; the [M1 guide](docs/milestone-1-instructions.md) documents that setup.

## Project structure

```text
src/ft_system/     protocol, replicas, clients, LFDs, GFD
configs/           M1 local, M2 local, and distributed examples
scripts/           M1 smoke and M2 one-command scenario
tests/             unit and integration tests
docs/              milestone guides, design, and demo checklist
```
