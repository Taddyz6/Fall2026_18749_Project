# Milestone 2: active replication

M2 uses three active server replicas (S1–S3), three local fault detectors (LFD1–LFD3), one global fault detector (GFD), and three clients (C1–C3). The ten processes communicate over TCP. This guide covers a one-computer run; the course demonstration places each server/LFD pair on a separate machine.

## Required observable behavior

1. GFD starts with `GFD: 0 members`. LFD1–3 register, but registration alone does not add a replica.
2. After each server starts, its LFD completes a local heartbeat and reports its health. GFD shows one, two, then three members.
3. Each client sends request number n to every connected replica before starting n+1. A separate reader handles each replica's replies. The first valid reply is delivered; later replies are printed and discarded. A duplicate carrying a different counter value also logs `replica state divergence`.
4. Stop S1. LFD1 detects the failure, and GFD shows `GFD: 2 members: S2, S3`. All clients continue. Stop S2 later; GFD shows `GFD: 1 member: S3`, and all clients still receive replies.
5. The surviving S3 processes each client's requests in order. The one-command check verifies requests 1–60 exactly once for each client and final state `{"C1": 60, "C2": 60, "C3": 60}`.

M2 does not implement a Replication Manager, state transfer, checkpointing, or automatic replica recovery. Do not restart an individual replica or client during this demo; restart the complete system for a fresh run.

## One command on one computer

From the repository root, with Python 3.11 or newer:

```bash
python3 scripts/m2_demo.py
```

This is an entry point for `scripts/m2_smoke.py`. It needs no package installation. The script allocates seven available loopback ports, creates a temporary deployment config in `logs/m2-smoke/`, starts all ten processes, injects S1 SIGINT and S2 SIGKILL in sequence, and waits for the checks above. It prints phase summaries and finishes with `PASS: evidence saved to ...`. Process logs and `result.json` are in that directory. It terminates remaining child processes on success, failure, or Ctrl-C.

To keep evidence elsewhere:

```bash
python3 scripts/m2_demo.py --output /tmp/m2-evidence
```

## Manual ten-terminal run

The checked-in [local config](../configs/m2.local.toml) uses fixed loopback ports: S1–S3 client ports 5001–5003, heartbeat ports 6001–6003, and GFD port 7000. Start one command per terminal in the order shown. Wait for each membership addition before starting the next server.

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

Clients send automatically once per second by default. `--interval 0.25` changes the interval, `--count 20` stops after 20 successful requests, and `--interactive` permits manual `increment` or Enter commands. The M1 single-server client also remains automatic by default.

When all clients show request traffic and duplicate replies, press Ctrl-C in S1's terminal. After GFD removes S1, let several more requests finish before stopping S2. Keep the clients running to observe replies from S3. Stop the remaining processes at the end.

## Four-machine deployment

Copy [the distributed example](../configs/m2.distributed.example.toml) to a deployment-specific TOML file. Replace all four example advertised addresses with reachable machine IP addresses, then copy the same completed file to every machine. Put GFD and C1–C3 on machine A; put each S/LFD pair on machines B, C, and D. Keep each LFD beside its server, bind listeners to `0.0.0.0`, and allow the configured ports through the firewalls. Run the same component commands with the deployment config path. The one-command local script does not launch across machines.

## Checks and limitations

```bash
python3 -m pytest -q
python3 scripts/m1_smoke.py
python3 scripts/m2_demo.py
```

The integration tests cover six orders of two sequential failures, three two-failure survivor choices, delayed duplicates, latest-request retransmission, and GFD/LFD failure handling. M1 smoke checks the original single-server path. Local loopback results do not establish that the four-machine network setup works.

Because the M1 application has one independent counter per client, increments from different clients commute. Replicas converge after processing the same unique requests, but the design does not impose a total order across clients. See [design details](DESIGN.md) and the [demo checklist](DEMO.md).
