# 18-749 Fault-Tolerant System — Milestone 1

This repository implements **Milestone 1 (M1)** of the 18-749 Distributed Systems course project.

Milestone 1 contains:

* Three independent clients: `C1`, `C2`, and `C3`
* One stateful server replica: `S1`
* One Local Fault Detector: `LFD1`
* TCP-based client/server communication
* TCP-based heartbeat monitoring between `LFD1` and `S1`
* Configurable heartbeat frequency and timeout
* Deterministic server-side state management
* Automatic unit, integration, and smoke tests

The protocol, configuration system, and process boundaries are designed so that later milestones can extend the system with components such as GFD, RM, replication, and checkpointing without requiring major changes to the M1 codebase.

No M2–M5 functionality is implemented in this milestone.

---

## 1. System Architecture

```text
C1 --\
C2 ---- TCP client_port ---- ClientListener ---- DeterministicStateMachine
C3 --/                              S1
                                     |
LFD1 ---- TCP heartbeat_port ---- HeartbeatListener
```

`S1` exposes two independent TCP ports:

* `client_port`

  * Handles client requests and replies.
  * Requests are processed by the deterministic state machine.

* `heartbeat_port`

  * Handles heartbeat messages from `LFD1`.
  * Sends heartbeat acknowledgements.
  * Does not access or modify application state.

This separation ensures that failure detection traffic is isolated from normal client traffic.

---

## 2. M1 Components

### Clients — C1, C2, C3

Each client is an independent process.

A client maintains its own monotonically increasing `request_num`. Every request contains both the course-required request number and an internal logical request identifier:

```text
request_id = <client_id>:<request_num>
```

For example:

```text
C1:1
C1:2
C2:1
```

The clients maintain persistent TCP connections to `S1` and send commands such as:

```text
increment
```

Each client is only allowed to increment its own server-side counter.

---

### Stateful Server — S1

`S1` maintains the deterministic application state:

```python
{
    "C1": 0,
    "C2": 0,
    "C3": 0
}
```

If `C1` sends an `increment` request, the state becomes:

```python
{
    "C1": 1,
    "C2": 0,
    "C3": 0
}
```

If all three clients send one request, the state becomes:

```python
{
    "C1": 1,
    "C2": 1,
    "C3": 1
}
```

The state machine is deterministic: the same request applied to the same state produces the same resulting state and reply.

---

### Local Fault Detector — LFD1

`LFD1` periodically sends heartbeat messages to `S1`.

A normal heartbeat exchange looks like:

```text
LFD1  ---- heartbeat ---->  S1
LFD1  <------ ACK --------  S1
```

`LFD1` considers a previously healthy server failed when a heartbeat attempt encounters one of the following:

* ACK timeout
* TCP EOF / closed connection
* Connection exception

The failure transition is logged only once:

```text
[timestamp] S1 has died
```

Repeated reconnect failures do not repeatedly print the same server-death event.

---

## 3. Communication Protocol

Messages use **JSON Lines framing**.

Each TCP message is encoded as one JSON object followed by a newline:

```text
{...}\n
```

This provides explicit message boundaries on top of TCP streams.

A client request logically contains fields such as:

```json
{
  "source": "C1",
  "destination": "S1",
  "request_num": 1,
  "request_id": "C1:1",
  "type": "request",
  "command": "increment"
}
```

Heartbeat traffic uses the same transport abstraction but a separate TCP connection and server listener.

---

## 4. Project Structure

```text
.
├── configs/
│   ├── local.toml
│   └── distributed.example.toml
│
├── docs/
│   └── superpowers/
│       ├── specs/
│       │   └── 2026-09-01-milestone-1-foundation-design.md
│       └── plans/
│           └── 2026-09-01-milestone-1-foundation.md
│
├── scripts/
│   └── m1_smoke.py
│
├── src/
│   └── ft_system/
│       ├── common/
│       │   ├── config.py
│       │   ├── logging.py
│       │   ├── protocol.py
│       │   ├── retry.py
│       │   └── transport.py
│       │
│       ├── client/
│       │   ├── app.py
│       │   └── main.py
│       │
│       ├── server/
│       │   ├── app.py
│       │   ├── main.py
│       │   └── state_machine.py
│       │
│       └── lfd/
│           ├── app.py
│           └── main.py
│
└── tests/
    ├── unit/
    └── integration/
```

---

# 5. Code Organization

## `src/ft_system/common/`

Contains functionality shared by all processes.

### `protocol.py`

Defines the logical message types used by:

* clients
* servers
* local fault detectors

It is responsible for converting structured protocol messages to and from their JSON representation.

---

### `transport.py`

Implements the TCP transport layer.

Messages are sent using JSON Lines framing:

```text
JSON object + "\n"
```

The transport layer is responsible for:

* reading complete messages from TCP streams
* serializing outgoing messages
* deserializing incoming messages
* detecting EOF and connection failures

Application components therefore do not need to directly manipulate raw TCP bytes.

---

### `config.py`

Loads and validates TOML configuration files.

Configuration includes information such as:

```toml
client_port = 5001
heartbeat_port = 6001
heartbeat_freq = 2.0
heartbeat_timeout = 1.0
```

The configuration layer validates conditions such as:

* ports must be between `1` and `65535`
* client and heartbeat ports must be different
* heartbeat frequency must be positive
* timeout values must be positive

---

### `logging.py`

Provides consistent timestamped logging for all M1 processes.

Examples:

```text
[2026-09-03 18:30:00.101] Received <C1, S1, 1, request>
```

and

```text
[2026-09-03 18:30:05.500] S1 has died
```

---

### `retry.py`

Contains reusable timeout, reconnection, and retry helpers.

This keeps connection recovery logic separate from the application logic.

---

# 6. Server Implementation

## `server/state_machine.py`

Contains the deterministic application state.

Conceptually:

```python
my_state = {
    "C1": 0,
    "C2": 0,
    "C3": 0,
}
```

When the server receives:

```text
<C1, S1, 1, request>
```

with the `increment` command, it performs:

```python
my_state["C1"] += 1
```

The other client counters are unchanged.

---

## `server/app.py`

Runs the two independent TCP listeners:

```text
ClientListener
HeartbeatListener
```

Conceptually:

```python
ClientListener
    |
    +---- DeterministicStateMachine

HeartbeatListener
```

`HeartbeatListener` does not receive a reference to the state machine and therefore cannot modify `my_state`.

The server uses asynchronous TCP listeners so client requests and heartbeat traffic can be processed independently.

---

## `server/main.py`

Provides the command-line entry point:

```bash
ft-server
```

Example:

```bash
ft-server --config configs/local.toml --server-id S1
```

---

# 7. Client Implementation

## `client/app.py`

Implements the interactive client process.

Each client:

1. Connects to `S1`.
2. Maintains its local request number.
3. Reads commands from the terminal.
4. Creates a request message.
5. Sends the request to `S1`.
6. Waits for the corresponding reply.
7. Prints the result.

---

## `client/main.py`

Provides the command-line entry point:

```bash
ft-client
```

Example:

```bash
ft-client --config configs/local.toml --client-id C1
```

---

# 8. LFD Implementation

## `lfd/app.py`

Implements heartbeat-based server failure detection.

For each heartbeat iteration, `LFD1`:

1. Sends a heartbeat to `S1`.
2. Waits for the ACK.
3. Records the heartbeat number.
4. Sleeps according to the configured heartbeat frequency.
5. Repeats.

Example:

```text
[1] LFD1 sending heartbeat to S1
[1] LFD1 receives heartbeat from S1
```

If the heartbeat fails:

```text
[2] LFD1 heartbeat to S1 failed: connection closed
S1 has died
```

---

## `lfd/main.py`

Provides the command-line entry point:

```bash
ft-lfd
```

Example:

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1
```

The configured heartbeat interval can also be overridden from the command line:

```bash
ft-lfd \
  --config configs/local.toml \
  --lfd-id LFD1 \
  --heartbeat-freq 0.5
```

---

# 9. Environment Setup

Python **3.11 or newer** is required.

The runtime implementation uses only the Python standard library. `pytest` is included only for development and testing.

From the project directory:

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
```

Create a virtual environment:

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

Install the project in editable mode with development dependencies:

```bash
python -m pip install -e '.[dev]'
```

---

# 10. Automated Tests

Before running the manual five-terminal demonstration, verify the implementation using the automated tests.

## Run the complete pytest suite

```bash
.venv/bin/python -m pytest -v
```

The test suite contains:

```text
tests/unit/
tests/integration/
```

Unit tests verify pure protocol, configuration, and state-machine behavior.

Integration tests use real loopback TCP connections to verify communication between components.

---

## Run the M1 Smoke Test

```bash
.venv/bin/python scripts/m1_smoke.py
```

The smoke test automatically starts the required components using dynamically allocated ports.

It verifies that:

1. `S1` starts successfully.
2. All three clients can connect.
3. `C1`, `C2`, and `C3` can each send one request.
4. The resulting state is:

```json
{
  "C1": 1,
  "C2": 1,
  "C3": 1
}
```

5. `LFD1` can successfully exchange heartbeats with `S1`.
6. After `S1` is stopped, `LFD1` detects the failure.

The smoke test is intended for automated verification. It does not replace the manual five-process classroom demonstration.

---

# 11. Running M1 Locally with Five Terminals

The primary M1 demonstration runs five independent processes:

```text
Terminal 1: S1
Terminal 2: LFD1
Terminal 3: C1
Terminal 4: C2
Terminal 5: C3
```

The local configuration file is:

```text
configs/local.toml
```

Start the processes in the order below.

---

## Terminal 1 — Start S1

Open the first terminal:

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate
```

Start the server:

```bash
ft-server --config configs/local.toml --server-id S1
```

S1 should begin listening on both:

```text
client_port
heartbeat_port
```

Leave this terminal running.

---

## Terminal 2 — Start LFD1

Open another terminal:

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate
```

Start the Local Fault Detector:

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1
```

After connecting to `S1`, the terminal should periodically show output similar to:

```text
[timestamp] [1] LFD1 sending heartbeat to S1
[timestamp] [1] LFD1 receives heartbeat from S1

[timestamp] [2] LFD1 sending heartbeat to S1
[timestamp] [2] LFD1 receives heartbeat from S1
```

At the same time, the S1 terminal should show heartbeat reception and ACK transmission.

Heartbeat processing must not change the application state.

---

## Terminal 3 — Start C1

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate

ft-client --config configs/local.toml --client-id C1
```

---

## Terminal 4 — Start C2

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate

ft-client --config configs/local.toml --client-id C2
```

---

## Terminal 5 — Start C3

```bash
cd /Users/zhangjunhao/Desktop/18749/18749_project
source .venv/bin/activate

ft-client --config configs/local.toml --client-id C3
```

At this point, the complete M1 system is running:

```text
C1 --------\
C2 ----------> S1 <---------- LFD1
C3 --------/
```

---

# 12. Testing Client Requests

In the `C1` terminal, enter:

```text
increment
```

or simply press Enter.

S1 should log something similar to:

```text
[timestamp] Received <C1, S1, 1, request>
[timestamp] my_state_S1 = {"C1": 0, "C2": 0, "C3": 0} before processing <C1, S1, 1, request>
[timestamp] my_state_S1 = {"C1": 1, "C2": 0, "C3": 0} after processing <C1, S1, 1, request>
[timestamp] Sending <C1, S1, 1, reply>
```

Now enter:

```text
increment
```

in `C2`.

The server state should become:

```json
{
  "C1": 1,
  "C2": 1,
  "C3": 0
}
```

Then enter:

```text
increment
```

in `C3`.

The state should become:

```json
{
  "C1": 1,
  "C2": 1,
  "C3": 1
}
```

---

# 13. Testing Multiple Requests

Send several additional requests from `C1`.

For example, enter three more times:

```text
increment
increment
increment
```

The `C1` request numbers should increase monotonically:

```text
<C1, S1, 2, request>
<C1, S1, 3, request>
<C1, S1, 4, request>
```

and the state should change only for `C1`:

```json
{
  "C1": 4,
  "C2": 1,
  "C3": 1
}
```

This verifies both:

* per-client request numbering
* deterministic per-client state updates

---

# 14. Verifying Heartbeats Do Not Modify State

While the clients remain idle, allow several heartbeat exchanges to occur.

LFD1 should continue printing:

```text
[timestamp] [5] LFD1 sending heartbeat to S1
[timestamp] [5] LFD1 receives heartbeat from S1
```

The server may log the corresponding heartbeat request and ACK.

However, the server application state should remain unchanged.

For example:

```json
{
  "C1": 4,
  "C2": 1,
  "C3": 1
}
```

This verifies that `HeartbeatListener` is isolated from `DeterministicStateMachine`.

---

# 15. Fault Injection Test

The main M1 failure-detection test intentionally terminates `S1`.

Keep the following processes running:

```text
LFD1
C1
C2
C3
```

Go to the `S1` terminal and press:

```text
Ctrl-C
```

S1 should shut down.

On the next heartbeat attempt, `LFD1` should observe a failure.

For example:

```text
[timestamp] [7] LFD1 sending heartbeat to S1
[timestamp] [7] LFD1 heartbeat to S1 failed: connection closed
[timestamp] S1 has died
```

Depending on exactly when the server is stopped, the reason may instead be:

```text
timeout
```

or a TCP connection exception.

All of these cases are considered heartbeat failures.

---

# 16. Verify the Failure Is Reported Only Once

After the first failed heartbeat, leave `LFD1` running for several more heartbeat periods.

The important behavior is that:

```text
S1 has died
```

is printed only once for the transition from:

```text
healthy -> failed
```

Subsequent connection failures may occur while LFD1 attempts to reconnect, but they must not repeatedly generate new server-death events.

Incorrect behavior would look like:

```text
S1 has died
S1 has died
S1 has died
S1 has died
```

The M1 implementation prevents this duplicate failure notification.

---

# 17. Testing Different Heartbeat Frequencies

Stop the remaining processes:

```text
LFD1
C1
C2
C3
```

Restart `S1`:

```bash
ft-server --config configs/local.toml --server-id S1
```

Then restart `LFD1` with a command-line heartbeat override.

For example:

```bash
ft-lfd \
  --config configs/local.toml \
  --lfd-id LFD1 \
  --heartbeat-freq 0.5
```

This sends approximately one heartbeat every:

```text
0.5 seconds
```

You should observe output arriving more frequently:

```text
[timestamp] [1] LFD1 sending heartbeat to S1
[timestamp] [1] LFD1 receives heartbeat from S1

[timestamp] [2] LFD1 sending heartbeat to S1
[timestamp] [2] LFD1 receives heartbeat from S1

[timestamp] [3] LFD1 sending heartbeat to S1
[timestamp] [3] LFD1 receives heartbeat from S1
```

Terminate `S1` again with:

```text
Ctrl-C
```

Because the heartbeat interval is shorter, LFD1 should attempt its next heartbeat sooner and therefore detect the failure sooner.

---

# 18. Comparing Heartbeat Frequencies

A useful demonstration is to repeat the fault test using several heartbeat frequencies.

For example:

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 0.5
```

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 1.0
```

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1 --heartbeat-freq 2.0
```

The expected tradeoff is:

| Heartbeat Frequency | Failure Detection | Heartbeat Traffic |
| ------------------- | ----------------- | ----------------- |
| `0.5 s`             | Faster            | Higher            |
| `1.0 s`             | Medium            | Medium            |
| `2.0 s`             | Slower            | Lower             |

This demonstrates the standard failure-detector tradeoff between detection latency and monitoring overhead.

---

# 19. Recommended M1 Manual Demonstration Sequence

For a complete classroom demonstration, the following sequence covers the main M1 requirements.

### Step 1 — Start S1

```bash
ft-server --config configs/local.toml --server-id S1
```

### Step 2 — Start LFD1

```bash
ft-lfd --config configs/local.toml --lfd-id LFD1
```

Verify several successful heartbeat exchanges.

### Step 3 — Start C1

```bash
ft-client --config configs/local.toml --client-id C1
```

### Step 4 — Start C2

```bash
ft-client --config configs/local.toml --client-id C2
```

### Step 5 — Start C3

```bash
ft-client --config configs/local.toml --client-id C3
```

### Step 6 — Send One Request from Each Client

Run:

```text
C1 -> increment
C2 -> increment
C3 -> increment
```

Verify:

```json
{
  "C1": 1,
  "C2": 1,
  "C3": 1
}
```

### Step 7 — Send Additional Requests

Send several more `increment` requests and verify that only the corresponding client's state changes.

### Step 8 — Observe Heartbeats

Verify that heartbeats continue while client requests are being processed.

Verify that heartbeat traffic does not modify `my_state`.

### Step 9 — Kill S1

Press:

```text
Ctrl-C
```

in the S1 terminal.

### Step 10 — Verify Failure Detection

LFD1 should eventually print:

```text
S1 has died
```

exactly once.

### Step 11 — Restart the System

Stop the remaining processes and restart all components.

### Step 12 — Change Heartbeat Frequency

Run:

```bash
ft-lfd \
  --config configs/local.toml \
  --lfd-id LFD1 \
  --heartbeat-freq 0.5
```

Repeat the server failure test and compare the detection behavior.

---

# 20. Distributed / Multi-Machine Demonstration

The same M1 implementation can run across multiple machines.

Copy:

```text
configs/distributed.example.toml
```

to a team-specific configuration file.

For example:

```bash
cp configs/distributed.example.toml configs/distributed.toml
```

Set the server machine's LAN address as its advertised host.

For example:

```toml
advertised_host = "192.168.1.100"
```

S1 should continue binding to:

```toml
bind_host = "0.0.0.0"
```

This allows remote machines to connect.

The same configuration file should be copied to all participating machines.

For the required deployment:

```text
Machine A:
    S1
    LFD1

Machine B:
    C1
    C2
    C3
```

The server machine must allow inbound TCP connections to:

```text
client_port
heartbeat_port
```

The default example ports are:

```text
client_port    = 5001
heartbeat_port = 6001
```

The same command-line programs are used in both local and distributed modes.

Only the configuration file changes.

---

# 21. Common Problems

## `Address already in use`

Another process is already using one of the configured ports.

Stop the old process or change the configured ports.

Also verify that:

```text
client_port != heartbeat_port
```

---

## `Connection refused`

Possible causes include:

* S1 is not running.
* The configured IP address is incorrect.
* The configured port is incorrect.
* A firewall is blocking the connection.
* A client is accidentally using `heartbeat_port`.

---

## Heartbeat timeout

Verify that LFD1 is connecting to:

```text
heartbeat_port
```

and not:

```text
client_port
```

Also verify that S1 is running and accepting heartbeat connections.

---

## Client timeout

Verify that the client is connecting to:

```text
client_port
```

Check the S1 terminal to determine whether the request was received.

---

## Configuration validation error

All frequency and timeout values must be positive:

```text
heartbeat_freq > 0
heartbeat_timeout > 0
```

Ports must satisfy:

```text
1 <= port <= 65535
```

The client and heartbeat ports must also be different.

---

# 22. Graceful Shutdown

The processes support normal shutdown using:

```text
Ctrl-C
```

The server handles shutdown signals and closes its listeners cleanly.

Clients may also exit interactively by entering:

```text
quit
```

or:

```text
exit
```

---

# 23. Milestone 1 Scope

M1 implements only the foundation required for a single server replica and local failure detection:

```text
C1
C2  ---> S1 <--- LFD1
C3
```

The implementation intentionally does not include:

* Global Fault Detector
* Replication Manager
* Multiple server replicas
* Primary/backup replication
* Active replication
* Checkpoint transfer
* State recovery
* Replica membership management

Those features are reserved for later milestones.

The M1 protocol, configuration, state-machine boundary, and networking abstractions are structured so that these components can be added incrementally in subsequent milestones.

---

# 24. Additional Design Documentation

Detailed architectural design:

```text
docs/superpowers/specs/2026-09-01-milestone-1-foundation-design.md
```

Detailed implementation and acceptance plan:

```text
docs/superpowers/plans/2026-09-01-milestone-1-foundation.md
```

---

# 25. Quick Verification Checklist

Before demonstrating M1, verify all of the following:

```text
[ ] pytest passes
[ ] scripts/m1_smoke.py passes
[ ] S1 starts successfully
[ ] LFD1 connects to S1
[ ] Heartbeat ACKs are received
[ ] C1 connects successfully
[ ] C2 connects successfully
[ ] C3 connects successfully
[ ] C1 can increment C1 state
[ ] C2 can increment C2 state
[ ] C3 can increment C3 state
[ ] Request numbers increase monotonically
[ ] Heartbeats do not modify application state
[ ] Killing S1 is detected by LFD1
[ ] "S1 has died" is printed only once
[ ] LFD1 can be restarted with a different heartbeat frequency
[ ] Shorter heartbeat frequency results in more frequent failure checks
```
