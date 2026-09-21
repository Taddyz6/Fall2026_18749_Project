# 18-749 Fault-Tolerant System

A distributed systems course project that incrementally builds a fault-tolerant
system in Python.

## Current Status

**Milestone 1** implements three independent clients (C1, C2, C3), one stateful
server (S1), and one Local Fault Detector (LFD1). Clients automatically send
counter-increment requests over TCP; LFD1 monitors S1 with configurable heartbeats.

The current implementation uses in-memory state and detects failures. Replication,
failover, GFD, RM, and checkpointing are reserved for later milestones. Requests
are not deduplicated, so retries after a lost reply can repeat an increment.

## Architecture

```text
C1 --\
C2 ---- TCP client_port ---- ClientListener ---- DeterministicStateMachine
C3 --/                              S1
                                     |
LFD1 ---- TCP heartbeat_port ---- HeartbeatListener
```

Client traffic and heartbeats use separate TCP ports and JSON Lines messages.
Each client increments only its own counter; heartbeats do not change application
state.

## Quick Start

Requires **Python 3.11+**. Run these commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

In separate terminals, activate the environment and start the server, detector,
and a client:

```bash
ft-server --config configs/local.toml --server-id S1
ft-lfd --config configs/local.toml --lfd-id LFD1
ft-client --config configs/local.toml --client-id C1
```

The client sends immediately, then waits 1 second after each completed or failed
attempt. Start C2 and C3 in additional terminals for the full demo.
Use `--interval 0.5` to change the delay in seconds, or `--interactive` to send
requests manually. Press Ctrl-C to stop an automatic client.

## Tests

```bash
.venv/bin/python -m pytest -v
.venv/bin/python scripts/m1_smoke.py
```

## Project Structure

```text
src/ft_system/
├── common/      # protocol, transport, configuration, logging, retry
├── server/      # TCP listeners and deterministic state machine
├── client/      # automatic and interactive clients
└── lfd/         # heartbeat-based failure detection
tests/          # unit and integration tests
configs/        # local configuration and distributed deployment template
scripts/        # M1 smoke check
docs/           # milestone instructions
```

## Documentation

- [Milestone 1: Setup, Demo, and Verification](docs/milestone-1-instructions.md)
  — five-terminal demo, multi-machine setup, failure injection, troubleshooting,
  and the verification checklist.
