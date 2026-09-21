# Milestone 1: Setup, Demo, and Verification

This guide covers **Milestone 1 (M1)** of the 18-749 Distributed Systems course project.

[Back to project overview](../README.md)

Run all commands below from the repository root, including commands shown in this document inside `docs/`.

M1 contains:

* Three independent clients: `C1`, `C2`, `C3`
* One stateful server: `S1`
* One Local Fault Detector: `LFD1`
* TCP request/reply communication
* Heartbeat-based failure detection
* Configurable heartbeat frequency and timeout

Later milestones can extend the current protocol and process structure with replication, GFD, RM, and checkpointing.

## Architecture

```text
C1 --\
C2 ---- TCP client_port ---- ClientListener ---- DeterministicStateMachine
C3 --/                              S1
                                     |
LFD1 ---- TCP heartbeat_port ---- HeartbeatListener
```

S1 uses two independent TCP ports:

* `client_port`: handles client requests and replies
* `heartbeat_port`: handles LFD heartbeat and ACK messages

Heartbeat handling does not modify the application state.

Messages use JSON Lines framing: one JSON object per line.

---

## Project Structure

```text
src/ft_system/
├── common/      # protocol, transport, config, logging, retry
├── server/      # server listeners and deterministic state machine
├── client/      # automatic and interactive clients
└── lfd/         # heartbeat and failure detection

tests/
├── unit/
└── integration/

configs/
├── local.toml
└── distributed.example.toml

scripts/
└── m1_smoke.py
```

### Main Components

`common/protocol.py`
Defines request, reply, heartbeat, and ACK messages.

`common/transport.py`
Implements JSON Lines communication over TCP.

`server/state_machine.py`
Maintains deterministic server state:

```python
{
    "C1": 0,
    "C2": 0,
    "C3": 0
}
```

Each client can increment only its own counter.

`server/app.py`
Runs separate client and heartbeat listeners.

`client/app.py`
Maintains a persistent TCP connection and sends numbered requests.

`lfd/app.py`
Periodically sends heartbeats to S1 and reports server failure.

---

## Setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install -e '.[dev]'
```

---

## Automated Tests

Run all tests:

```bash
.venv/bin/python -m pytest -v
```

Run the M1 smoke test:

```bash
.venv/bin/python scripts/m1_smoke.py
```

The smoke test verifies:

* C1, C2, and C3 can send requests
* Server state becomes:

```json
{"C1": 1, "C2": 1, "C3": 1}
```

* Heartbeat communication works
* LFD1 detects S1 failure after S1 shuts down

---

## Local Five-Terminal M1 Demo

Use:

```text
configs/local.toml
```

Open five terminals.

### Terminal 1 — S1

```bash
source .venv/bin/activate

ft-server --config configs/local.toml --server-id S1
```

### Terminal 2 — LFD1

```bash
source .venv/bin/activate

ft-lfd --config configs/local.toml --lfd-id LFD1
```

LFD1 should periodically print:

```text
[timestamp] [1] LFD1 sending heartbeat to S1
[timestamp] [1] LFD1 receives heartbeat from S1
```

### Terminal 3 — C1

```bash
source .venv/bin/activate

ft-client --config configs/local.toml --client-id C1
```

### Terminal 4 — C2

```bash
source .venv/bin/activate

ft-client --config configs/local.toml --client-id C2
```

### Terminal 5 — C3

```bash
source .venv/bin/activate

ft-client --config configs/local.toml --client-id C3
```

---

## Automatic Client Requests

Each client starts sending `increment` requests immediately, without keyboard input.
After each reply or failed attempt, it waits **1 second** before the next attempt.
Only one request is in flight per client; this is a delay between attempts, not a
fixed requests-per-second rate.

To change the delay, launch a client with a finite positive interval in seconds:

```bash
ft-client --config configs/local.toml --client-id C1 --interval 0.5
```

Use the same option for C2 and C3 as needed. Press **Ctrl-C** in each client
terminal to stop its loop and close its connection.

S1 should continuously log requests and state changes. Each successful operation
increments only the requesting client's counter. The counters need not be equal,
and the automatic demo will not remain at `{"C1": 1, "C2": 1, "C3": 1}`.
The smoke test above deliberately sends one request per client to verify that
exact state.

### Optional Manual Mode

For a step-by-step demonstration, start each client with `--interactive`:

```bash
ft-client --config configs/local.toml --client-id C1 --interactive
```

Use C2 and C3 in their respective terminals. Enter `increment` or press Enter to
send one request; enter `quit` or `exit` to stop. `--interval` does not affect manual
mode.

Starting from a fresh server, after C1 sends its first request, S1 prints output
similar to:

```text
[timestamp] Received <C1, S1, 1, request>
[timestamp] my_state_S1 = {"C1": 0, "C2": 0, "C3": 0} before processing <C1, S1, 1, request>
[timestamp] my_state_S1 = {"C1": 1, "C2": 0, "C3": 0} after processing <C1, S1, 1, request>
[timestamp] Sending <C1, S1, 1, reply>
```

After each client sends exactly one request, the state is:

```json
{"C1": 1, "C2": 1, "C3": 1}
```

---

## Failure Injection Test

Keep LFD1 and the three clients running.

In the S1 terminal, press:

```text
Ctrl-C
```

When the next heartbeat attempt fails, LFD1 should detect the failure:

```text
[timestamp] [2] LFD1 heartbeat to S1 failed: connection closed
[timestamp] S1 has died
```

The failure reason may also be `timeout` or another TCP connection error.

`S1 has died` should only be printed once per healthy-to-failed transition.
Later reconnect failures should not repeatedly generate the same failure event.

Automatic clients log connection errors and keep retrying after their configured
interval. Restart S1 with the same command to let them reconnect and resume sending.
S1 stores state in memory, so its counters reset on restart. Press Ctrl-C in the
client terminals when finished.

M1 has no request deduplication: if S1 processes a request but its reply is lost,
a later attempt can increment the counter again. Automatic retries preserve the
existing client request-number behavior; they do not provide exactly-once processing.

---

## Test Different Heartbeat Frequencies

Restart S1 and LFD1.

Override the configured heartbeat interval (`--heartbeat-freq` is measured in seconds, not Hz):

```bash
ft-lfd \
  --config configs/local.toml \
  --lfd-id LFD1 \
  --heartbeat-freq 0.5
```

Other examples:

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 1.0
```

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 2.0
```

A shorter heartbeat interval performs failure checks more frequently, while a longer interval generates less heartbeat traffic.

Repeat the S1 `Ctrl-C` failure test for different heartbeat frequencies and compare the detection behavior.

---

## Multi-Machine Mode

Copy the distributed configuration template:

```bash
cp configs/distributed.example.toml configs/distributed.toml
```

Set the S1/LFD1 machine's LAN IP as `advertised_host`.

S1 should use:

```toml
bind_host = "0.0.0.0"
```

Copy the same configuration file to all machines.

Typical deployment:

```text
Machine A:
  S1
  LFD1

Machine B:
  C1
  C2
  C3
```

Allow inbound TCP traffic for the configured `client_port` and `heartbeat_port`.

---

## Common Issues

**Address already in use**

Stop the old process or change the configured ports.

**Connection refused**

Check that S1 is running and that the configured IP and port are correct.

**Heartbeat timeout**

Verify LFD1 is connecting to `heartbeat_port`.

**Client timeout**

Verify clients are connecting to `client_port`.

---

## M1 Verification Checklist

```text
[ ] pytest passes
[ ] m1_smoke.py passes
[ ] S1 starts
[ ] LFD1 receives heartbeat ACKs
[ ] C1, C2, C3 connect and send repeatedly without keyboard input
[ ] --interval changes the delay between attempts
[ ] Ctrl-C stops each automatic client
[ ] --interactive retains Enter/increment and quit/exit commands
[ ] Automatic clients reconnect after S1 restarts
[ ] Each client updates its own state
[ ] Heartbeats do not change application state
[ ] Killing S1 is detected by LFD1
[ ] "S1 has died" is printed only once
[ ] Heartbeat frequency can be overridden from the CLI
```
