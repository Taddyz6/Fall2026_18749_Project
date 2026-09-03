# Milestone 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a testable Python implementation of Milestone 1 with three clients, one stateful server, an isolated heartbeat listener, and one configurable local fault detector while preserving extension points for Milestones 2-5.

**Architecture:** Each component runs as its own process and uses `asyncio` with persistent JSON Lines TCP connections. S1 exposes separate client and heartbeat ports; shared modules own protocol validation, transport, configuration, logging, timeout, and reconnection behavior.

**Tech Stack:** Python 3.11+, standard-library runtime (`asyncio`, `dataclasses`, `json`, `tomllib`, `argparse`), pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-01-milestone-1-foundation-design.md`

## Global Constraints

- Python 3.11 or newer is required.
- Runtime code uses only the Python standard library.
- S1 client traffic and heartbeat traffic use separate TCP ports and separate `asyncio.start_server(handler, host, port)` listeners.
- `HeartbeatListener` must never read or mutate `my_state`.
- Messages use UTF-8 JSON Lines framing and are limited to 64 KiB per line.
- Every client request carries `client_id`, `replica_id`, `request_num`, and `request_id`.
- `request_id` is `<client_id>:<request_num>` and remains stable across replicas.
- The server logs the request tuple and state before and after every successful request.
- LFD1 retains `heartbeat_freq` and `heartbeat_count`, starts the count at 1, and logs every heartbeat.
- One missing heartbeat ACK is sufficient to mark a previously healthy S1 as failed.
- Reconnection uses connect/read timeouts and capped exponential backoff only; do not add a circuit breaker.
- Both local and distributed deployments use the same code and differ only by TOML configuration.
- Current workspace is not a Git repository. Run each listed commit step only after the user initializes or supplies a repository; otherwise record the task boundary without committing.

---

## File Map

Create the following files. Keep the responsibility of each file narrow.

```text
pyproject.toml                              packaging, entry points, test settings
README.md                                   setup, local demo, distributed demo
configs/local.toml                         loopback configuration
configs/distributed.example.toml           documented multi-machine template
scripts/m1_smoke.py                        automated rubric-oriented smoke run
src/ft_system/__init__.py                  package marker and version
src/ft_system/common/protocol.py           message builders and validation
src/ft_system/common/transport.py          bounded JSON Lines stream I/O
src/ft_system/common/config.py             TOML models, loading, validation
src/ft_system/common/logging.py            rubric-aligned event output
src/ft_system/common/retry.py              timeout and capped backoff helpers
src/ft_system/client/app.py                persistent client behavior
src/ft_system/client/main.py               client CLI and interactive input
src/ft_system/server/state_machine.py      deterministic per-client state
src/ft_system/server/app.py                ServerApp and both listeners
src/ft_system/server/main.py               server CLI and signal handling
src/ft_system/lfd/app.py                   heartbeat loop and failure detection
src/ft_system/lfd/main.py                  LFD CLI and signal handling
tests/unit/test_protocol.py                protocol contract tests
tests/unit/test_config.py                  config validation tests
tests/unit/test_logging.py                 exact log wording tests
tests/unit/test_state_machine.py           deterministic transition tests
tests/unit/test_transport.py               framing and size-limit tests
tests/unit/test_retry.py                   timeout/backoff tests
tests/integration/test_server.py           dual-listener and isolation tests
tests/integration/test_client.py           client request lifecycle tests
tests/integration/test_lfd.py              heartbeat/failure/reconnect tests
tests/integration/test_m1_system.py         three-client end-to-end test
tests/helpers.py                            async eventually helper and port utilities
```

## Task 1: Package Scaffold and Versioned Protocol

**Files:**
- Create: `pyproject.toml`
- Create: `src/ft_system/__init__.py`
- Create: `src/ft_system/common/__init__.py`
- Create: `src/ft_system/common/protocol.py`
- Test: `tests/unit/test_protocol.py`

**Interfaces:**
- Consumes: no project interfaces.
- Produces: `ProtocolError`, `MessageType`, `validate_message()`, `encode_message()`, `decode_message()`, `build_client_request()`, `build_client_reply()`, `build_heartbeat()`, `build_heartbeat_ack()`, and `build_error()`.

- [ ] **Step 1: Create packaging and test configuration**

Create `pyproject.toml` with the exact minimum project metadata and development dependencies:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "ft-system"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[project.scripts]
ft-server = "ft_system.server.main:main"
ft-client = "ft_system.client.main:main"
ft-lfd = "ft_system.lfd.main:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

Set `__version__ = "0.1.0"` in `src/ft_system/__init__.py`; leave `common/__init__.py` empty.

- [ ] **Step 2: Write failing protocol contract tests**

Create tests covering construction, round-trip encoding, and rejection of an inconsistent logical ID:

```python
import pytest

from ft_system.common.protocol import (
    ProtocolError,
    build_client_request,
    decode_message,
    encode_message,
)


def test_client_request_preserves_rubric_identifiers():
    message = build_client_request("C1", "S1", 7)
    assert message["request_num"] == 7
    assert message["request_id"] == "C1:7"
    assert message["client_id"] == "C1"
    assert message["replica_id"] == "S1"
    assert decode_message(encode_message(message)) == message


def test_rejects_request_id_that_disagrees_with_request_num():
    message = build_client_request("C1", "S1", 7)
    message["request_id"] = "C1:8"
    with pytest.raises(ProtocolError, match="request_id"):
        encode_message(message)
```

Also test heartbeat/ACK count preservation, reply identifier preservation, unsupported version, missing required field, invalid JSON, and a final newline from `encode_message()`.

- [ ] **Step 3: Run the protocol tests and confirm failure**

Run: `python -m pytest tests/unit/test_protocol.py -v`

Expected: collection fails with `ModuleNotFoundError` for `ft_system.common.protocol`.

- [ ] **Step 4: Implement the minimal protocol module**

Expose these exact public names and signatures. `ProtocolError` is intentionally an empty exception subtype; the functions contain the validation and construction behavior:

```python
class ProtocolError(ValueError):
    pass

class MessageType(StrEnum):
    CLIENT_REQUEST = "client_request"
    CLIENT_REPLY = "client_reply"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    ERROR = "error"

def encode_message(message: Mapping[str, Any]) -> bytes:
    validated = validate_message(message)
    text = json.dumps(validated, separators=(",", ":"), sort_keys=True)
    return f"{text}\n".encode("utf-8")

def decode_message(line: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON message") from exc
    return validate_message(decoded)
```

Implement `validate_message(message)`, the five builders, and `build_error(source, destination, code, detail, request_id=None)` with the exact return types named in the Interfaces block. Validation must require protocol `version == 1`, positive integer counters, non-empty IDs, exact request ID consistency, and type-specific fields. For a client request, protocol validation requires a non-empty operation string but leaves supported-operation policy to the state machine. Builders return ordinary dictionaries and pass their result through `validate_message()` before returning.

- [ ] **Step 5: Run protocol tests**

Run: `python -m pytest tests/unit/test_protocol.py -v`

Expected: all protocol tests pass.

- [ ] **Step 6: Commit the protocol boundary**

```bash
git add pyproject.toml src/ft_system tests/unit/test_protocol.py
git commit -m "feat: define versioned milestone one protocol"
```

## Task 2: Validated Local and Distributed Configuration

**Files:**
- Create: `src/ft_system/common/config.py`
- Create: `configs/local.toml`
- Create: `configs/distributed.example.toml`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: no runtime interface from Task 1.
- Produces: immutable `ServerConfig`, `ClientConfig`, `LfdConfig`, `AppConfig`, and `load_config(path)`.

- [ ] **Step 1: Write failing configuration tests**

Tests must prove local config loads separate ports and bad values fail immediately:

```python
def test_loads_distinct_client_and_heartbeat_ports(tmp_path):
    path = write_config(tmp_path, client_port=5001, heartbeat_port=6001)
    config = load_config(path)
    assert config.server("S1").client_port == 5001
    assert config.server("S1").heartbeat_port == 6001


@pytest.mark.parametrize("field,value", [
    ("client_port", 0),
    ("heartbeat_port", 65536),
    ("heartbeat_freq", 0),
    ("read_timeout", -1),
])
def test_rejects_invalid_numeric_values(tmp_path, field, value):
    path = write_config(tmp_path, **{field: value})
    with pytest.raises(ConfigError):
        load_config(path)
```

Define `write_config(tmp_path, **overrides)` in the same test file. It writes a complete minimal TOML string for S1, C1, and LFD1, applies only the named scalar override, and returns the resulting `Path`.

Also test duplicate client/heartbeat ports on S1, missing referenced server, missing client ID, and a distributed config with `bind_host = "0.0.0.0"` plus a distinct `advertised_host`.

- [ ] **Step 2: Run configuration tests and confirm failure**

Run: `python -m pytest tests/unit/test_config.py -v`

Expected: import fails because `ft_system.common.config` does not exist.

- [ ] **Step 3: Implement immutable configuration models**

Use these exact models. Add `class ConfigError(ValueError): pass` and make every lookup method translate a missing dictionary key into `ConfigError("unknown <component> id: <id>")`:

```python
@dataclass(frozen=True)
class ServerConfig:
    replica_id: str
    bind_host: str
    advertised_host: str
    client_port: int
    heartbeat_port: int

@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    server_ids: tuple[str, ...]
    connect_timeout: float
    read_timeout: float

@dataclass(frozen=True)
class LfdConfig:
    lfd_id: str
    server_id: str
    heartbeat_freq: float
    connect_timeout: float
    read_timeout: float
    initial_backoff: float = 0.5
    max_backoff: float = 5.0

@dataclass(frozen=True)
class AppConfig:
    servers: Mapping[str, ServerConfig]
    clients: Mapping[str, ClientConfig]
    lfds: Mapping[str, LfdConfig]
    def server(self, replica_id: str) -> ServerConfig:
        try:
            return self.servers[replica_id]
        except KeyError as exc:
            raise ConfigError(f"unknown server id: {replica_id}") from exc
```

Implement `client()` and `lfd()` with the same lookup pattern. Implement `load_config(path: str | Path) -> AppConfig` using `tomllib`, validate ports in `1..65535`, require distinct S1 ports, require positive timing values, and verify every referenced server exists.

- [ ] **Step 4: Create both configuration files**

`configs/local.toml` defines S1 on loopback, C1-C3 targeting S1, and LFD1 targeting S1. `configs/distributed.example.toml` uses `bind_host = "0.0.0.0"`, an explicit example LAN address for `advertised_host`, and comments explaining that every machine receives the same edited file.

- [ ] **Step 5: Run configuration tests**

Run: `python -m pytest tests/unit/test_config.py -v`

Expected: all configuration tests pass.

- [ ] **Step 6: Commit configuration**

```bash
git add src/ft_system/common/config.py configs tests/unit/test_config.py
git commit -m "feat: add validated deployment configuration"
```

## Task 3: Rubric-Aligned Event Logging

**Files:**
- Create: `src/ft_system/common/logging.py`
- Test: `tests/unit/test_logging.py`

**Interfaces:**
- Consumes: protocol message mappings from Task 1.
- Produces: `EventLogger` methods used by server, client, and LFD.

- [ ] **Step 1: Write failing exact-output tests**

Inject `io.StringIO` and a fixed clock:

```python
def test_server_request_log_matches_rubric_wording():
    stream = io.StringIO()
    logger = EventLogger(stream=stream, clock=lambda: "2026-09-01 12:00:00", use_color=False)
    request = build_client_request("C1", "S1", 7)
    logger.received(request, "request")
    logger.state("S1", {"C1": 0, "C2": 0, "C3": 0}, "before", request)
    assert stream.getvalue().splitlines() == [
        "[2026-09-01 12:00:00] Received <C1, S1, 7, request>",
        "[2026-09-01 12:00:00] my_state_S1 = {\"C1\": 0, \"C2\": 0, \"C3\": 0} before processing <C1, S1, 7, request>",
    ]
```

Add tests for after-state, Sending reply, heartbeat sent/ACK received with count, one `S1 has died` event, and `flush()` being called.

- [ ] **Step 2: Run logging tests and confirm failure**

Run: `python -m pytest tests/unit/test_logging.py -v`

Expected: import fails because `EventLogger` does not exist.

- [ ] **Step 3: Implement `EventLogger`**

Expose the public methods listed in the Interfaces block with these exact signatures: `received(message, direction)`, `sending(message, direction)`, `state(replica_id, state, phase, request)`, `heartbeat_sent(lfd_id, replica_id, count)`, `heartbeat_ack(lfd_id, replica_id, count)`, `server_failed(replica_id)`, `server_recovered(replica_id)`, and `error(component_id, detail)`. Centralize output in this concrete helper:

```python
class EventLogger:
    def __init__(self, stream: TextIO = sys.stdout,
                 clock: Callable[[], str] = default_clock,
                 use_color: bool | None = None):
        self._stream = stream
        self._clock = clock
        self._use_color = stream.isatty() if use_color is None else use_color

    def _write(self, text: str) -> None:
        print(f"[{self._clock()}] {text}", file=self._stream, flush=True)
```

Serialize state keys deterministically, validate `phase` as `before` or `after`, and flush after every line. ANSI colors may be enabled only when `use_color` is true; required words must remain present.

- [ ] **Step 4: Run logging tests**

Run: `python -m pytest tests/unit/test_logging.py -v`

Expected: all logging tests pass.

- [ ] **Step 5: Commit logging contract**

```bash
git add src/ft_system/common/logging.py tests/unit/test_logging.py
git commit -m "feat: add rubric aligned event logging"
```

## Task 4: Deterministic Per-Client State Machine

**Files:**
- Create: `src/ft_system/server/__init__.py`
- Create: `src/ft_system/server/state_machine.py`
- Test: `tests/unit/test_state_machine.py`

**Interfaces:**
- Consumes: validated client IDs and operation strings.
- Produces: `StateMachineError`, immutable `Transition`, and `DeterministicStateMachine`.

- [ ] **Step 1: Write failing state transition tests**

```python
def test_increment_changes_only_requesting_client_slot():
    machine = DeterministicStateMachine(("C1", "C2", "C3"))
    transition = machine.apply("C1", "increment")
    assert transition.before == {"C1": 0, "C2": 0, "C3": 0}
    assert transition.after == {"C1": 1, "C2": 0, "C3": 0}
    assert transition.client_value == 1


def test_unsupported_operation_does_not_mutate_state():
    machine = DeterministicStateMachine(("C1", "C2", "C3"))
    with pytest.raises(StateMachineError):
        machine.apply("C1", "decrement")
    assert machine.snapshot() == {"C1": 0, "C2": 0, "C3": 0}
```

Also test unknown clients, independent C1/C2 ordering, and returned snapshots not aliasing internal state.

- [ ] **Step 2: Run state tests and confirm failure**

Run: `python -m pytest tests/unit/test_state_machine.py -v`

Expected: import fails because the state machine does not exist.

- [ ] **Step 3: Implement the state machine**

```python
@dataclass(frozen=True)
class Transition:
    before: dict[str, int]
    after: dict[str, int]
    client_value: int

class StateMachineError(ValueError):
    pass

class DeterministicStateMachine:
    def __init__(self, client_ids: Iterable[str]):
        self._state = {client_id: 0 for client_id in client_ids}

    def snapshot(self) -> dict[str, int]:
        return dict(self._state)

    def apply(self, client_id: str, operation: str) -> Transition:
        if client_id not in self._state:
            raise StateMachineError(f"unknown client: {client_id}")
        if operation != "increment":
            raise StateMachineError(f"unsupported operation: {operation}")
        before = self.snapshot()
        self._state[client_id] += 1
        after = self.snapshot()
        return Transition(before, after, after[client_id])
```

Allow only `increment`; copy before/after snapshots; never expose the mutable internal dict.

- [ ] **Step 4: Run state tests**

Run: `python -m pytest tests/unit/test_state_machine.py -v`

Expected: all state tests pass.

- [ ] **Step 5: Commit the state machine**

```bash
git add src/ft_system/server tests/unit/test_state_machine.py
git commit -m "feat: add deterministic client state machine"
```

## Task 5: Bounded Transport, Timeouts, and Simple Backoff

**Files:**
- Create: `src/ft_system/common/transport.py`
- Create: `src/ft_system/common/retry.py`
- Test: `tests/unit/test_transport.py`
- Test: `tests/unit/test_retry.py`

**Interfaces:**
- Consumes: `encode_message()` and `decode_message()` from Task 1.
- Produces: `read_message()`, `read_message_with_timeout()`, `write_message()`, `close_writer()`, `open_connection_with_timeout()`, and `ExponentialBackoff`.

- [ ] **Step 1: Write failing transport tests**

Use an `asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)` and feed two encoded lines directly so the test has no undefined fixture:

```python
@pytest.mark.asyncio
async def test_reads_two_json_lines_without_merging():
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    first = build_heartbeat("LFD1", "S1", 1)
    second = build_heartbeat("LFD1", "S1", 2)
    reader.feed_data(encode_message(first) + encode_message(second))
    reader.feed_eof()
    assert await read_message(reader) == first
    assert await read_message(reader) == second
```

Add EOF, invalid JSON, line longer than `MAX_MESSAGE_BYTES = 65536`, writer closing, and read timeout tests.

- [ ] **Step 2: Write failing backoff tests**

```python
def test_backoff_doubles_and_caps_then_resets():
    backoff = ExponentialBackoff(initial=0.5, maximum=2.0)
    assert [backoff.next_delay() for _ in range(4)] == [0.5, 1.0, 2.0, 2.0]
    backoff.reset()
    assert backoff.next_delay() == 0.5
```

Also test invalid constructor values and connect timeout by patching `asyncio.open_connection` with a never-completing coroutine.

- [ ] **Step 3: Run transport/retry tests and confirm failure**

Run: `python -m pytest tests/unit/test_transport.py tests/unit/test_retry.py -v`

Expected: imports fail because both modules are absent.

- [ ] **Step 4: Implement the transport API**

```python
MAX_MESSAGE_BYTES = 65_536

async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        line = await reader.readline()
    except ValueError as exc:
        raise ProtocolError("message exceeds 65536 bytes") from exc
    if not line:
        raise EOFError("connection closed")
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds 65536 bytes")
    return decode_message(line)

async def read_message_with_timeout(reader: asyncio.StreamReader,
                                    timeout: float) -> dict[str, Any]:
    return await asyncio.wait_for(read_message(reader), timeout)
```

Implement `write_message()` by encoding first, rejecting encoded data longer than `MAX_MESSAGE_BYTES`, then calling `writer.write(data)` followed by `await writer.drain()`. Implement `close_writer()` as a no-op for `None`; otherwise call `close()` and await `wait_closed()`, suppressing only connection-level `OSError`. Treat clean EOF before a message as `EOFError`.

- [ ] **Step 5: Implement timeout and backoff helpers**

```python
@dataclass
class ExponentialBackoff:
    initial: float = 0.5
    maximum: float = 5.0
    attempt: int = 0
    def next_delay(self) -> float:
        delay = min(self.initial * (2 ** self.attempt), self.maximum)
        self.attempt += 1
        return delay

    def reset(self) -> None:
        self.attempt = 0
```

Reject non-positive initial/maximum values and `initial > maximum` in `__post_init__()`. Implement `open_connection_with_timeout(host, port, timeout)` by awaiting `asyncio.wait_for(asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES + 1), timeout)`. Do not add states, policies, jitter, or circuit-breaking behavior.

- [ ] **Step 6: Run transport/retry tests**

Run: `python -m pytest tests/unit/test_transport.py tests/unit/test_retry.py -v`

Expected: all tests pass.

- [ ] **Step 7: Commit transport primitives**

```bash
git add src/ft_system/common/transport.py src/ft_system/common/retry.py tests/unit/test_transport.py tests/unit/test_retry.py
git commit -m "feat: add bounded async transport and reconnect helpers"
```

## Task 6: S1 with Independent Client and Heartbeat Listeners

**Files:**
- Create: `src/ft_system/server/app.py`
- Create: `src/ft_system/server/main.py`
- Test: `tests/integration/test_server.py`
- Create: `tests/helpers.py`

**Interfaces:**
- Consumes: `ServerConfig`, protocol builders, transport helpers, `EventLogger`, and `DeterministicStateMachine`.
- Produces: `ClientListener`, `HeartbeatListener`, `ServerApp.start()`, `ServerApp.close()`, `ServerApp.serve_forever()`, and `server.main.main()`.

- [ ] **Step 1: Add an async eventual assertion helper**

```python
async def eventually(predicate: Callable[[], bool], timeout: float = 1.0,
                     interval: float = 0.01) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(interval)
```

- [ ] **Step 2: Write failing dual-listener tests**

Construct `ServerConfig` with port `0` programmatically so the OS chooses test ports. Define these test-local helpers first: `test_server_config()` returns that config; `send_heartbeat(port, count)` opens a loopback connection, writes one heartbeat, reads one ACK, and closes; `send_request(port, client_id, request_num)` does the same for a client request. Verify:

```python
@pytest.mark.asyncio
async def test_heartbeat_and_client_listeners_are_independent():
    app = ServerApp(
        test_server_config(),
        ("C1", "C2", "C3"),
        EventLogger(stream=io.StringIO(), use_color=False),
    )
    await app.start()
    heartbeat_before = app.state_machine.snapshot()
    ack = await send_heartbeat(app.bound_heartbeat_port, count=1)
    assert ack["type"] == "heartbeat_ack"
    assert app.state_machine.snapshot() == heartbeat_before
    reply = await send_request(app.bound_client_port, "C1", request_num=1)
    assert reply["payload"]["client_value"] == 1
    await app.close()
```

Add tests for exact before/after logs, independent failure of one malformed connection, unsupported operations returning an `error` message without state mutation, and two concurrent clients producing complete non-interleaved state log groups.

- [ ] **Step 3: Run server integration tests and confirm failure**

Run: `python -m pytest tests/integration/test_server.py -v`

Expected: import fails because `server.app` does not exist.

- [ ] **Step 4: Implement the listeners**

Implement `ClientListener(replica_id, state_machine, logger)` with `handle(reader, writer)` and `HeartbeatListener(replica_id, logger)` with `handle(reader, writer)`. Only the client listener receives the state machine:

```python
class ClientListener:
    def __init__(self, replica_id: str, state_machine: DeterministicStateMachine,
                 logger: EventLogger):
        self._replica_id = replica_id
        self._state_machine = state_machine
        self._logger = logger
        self._processing_lock = asyncio.Lock()

class HeartbeatListener:
    def __init__(self, replica_id: str, logger: EventLogger):
        self._replica_id = replica_id
        self._logger = logger
```

`ClientListener` owns one `asyncio.Lock` that encloses received log, before snapshot, synchronous state transition, after log, reply construction, and Sending log. Network write may occur after the locked block. `HeartbeatListener` validates only heartbeat messages and never receives a state-machine reference.

- [ ] **Step 5: Implement `ServerApp` lifecycle**

```python
class ServerApp:
    def __init__(self, config: ServerConfig, client_ids: Iterable[str],
                 logger: EventLogger):
        self.config = config
        self.state_machine = DeterministicStateMachine(client_ids)
        self.client_listener = ClientListener(config.replica_id, self.state_machine, logger)
        self.heartbeat_listener = HeartbeatListener(config.replica_id, logger)
```

Expose read-only `bound_client_port` and `bound_heartbeat_port` properties plus async `start()`, `serve_forever()`, and `close()` methods. Create two separate `asyncio.start_server()` instances with `limit=MAX_MESSAGE_BYTES + 1`. Wrap each listener callback with a `ServerApp` callback that adds the writer to `_active_writers` before handling and removes it in `finally`; `close()` uses that set to shut down live connections. `start()` returns after both sockets bind; `serve_forever()` awaits both server objects.

- [ ] **Step 6: Add the server CLI**

`server.main` parses `--config` and `--server-id`, loads config, passes `tuple(app_config.clients)` as `client_ids`, constructs the app, installs SIGINT/SIGTERM handlers where supported, and guarantees `await app.close()` in `finally`.

- [ ] **Step 7: Run server tests and the complete unit suite**

Run: `python -m pytest tests/integration/test_server.py tests/unit -v`

Expected: all tests pass.

- [ ] **Step 8: Commit S1**

```bash
git add src/ft_system/server tests/helpers.py tests/integration/test_server.py
git commit -m "feat: add isolated server client and heartbeat listeners"
```

## Task 7: Persistent Interactive Clients

**Files:**
- Create: `src/ft_system/client/__init__.py`
- Create: `src/ft_system/client/app.py`
- Create: `src/ft_system/client/main.py`
- Test: `tests/integration/test_client.py`

**Interfaces:**
- Consumes: `ClientConfig`, `ServerConfig`, protocol/transport helpers, and `EventLogger`.
- Produces: `ClientApp.connect()`, `ClientApp.send_increment()`, `ClientApp.close()`, `run_interactive()`, and client CLI.

- [ ] **Step 1: Write failing client lifecycle tests**

Define test-local `started_server()` to start S1 with dynamic ports, `endpoint_for(server)` to return `ServerEndpoint("S1", "127.0.0.1", server.bound_client_port)`, `timeout_config()` to return `(0.2, 0.2)`, and `test_logger()` to return a no-color logger backed by `StringIO`. Start a real `ServerApp`, then verify request numbering:

```python
@pytest.mark.asyncio
async def test_client_increments_request_num_after_valid_reply():
    server = await started_server()
    connect_timeout, read_timeout = timeout_config()
    client = ClientApp(
        "C1", endpoint_for(server), connect_timeout, read_timeout, test_logger()
    )
    first = await client.send_increment()
    second = await client.send_increment()
    assert first["request_num"] == 1
    assert second["request_num"] == 2
    assert first["request_id"] == "C1:1"
    assert second["request_id"] == "C1:2"
```

Also assert request number is not advanced after timeout or mismatched reply, client logs Sent/Received tuples, a persistent connection serves multiple requests, and `close()` is idempotent.

- [ ] **Step 2: Run client tests and confirm failure**

Run: `python -m pytest tests/integration/test_client.py -v`

Expected: import fails because `client.app` does not exist.

- [ ] **Step 3: Implement `ClientApp`**

```python
@dataclass(frozen=True)
class ServerEndpoint:
    replica_id: str
    host: str
    port: int

class ClientApp:
    def __init__(self, client_id: str, endpoint: ServerEndpoint,
                 connect_timeout: float, read_timeout: float,
                 logger: EventLogger):
        self.client_id = client_id
        self.endpoint = endpoint
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.logger = logger
        self._next_request_num = 1
        self._reader = None
        self._writer = None
```

Expose the read-only `next_request_num` property and async `connect()`, `send_increment()`, and `close()` methods. Keep one TCP connection. `send_increment()` calls `connect()` when no live writer exists, constructs and logs a request, writes it, reads with timeout, validates matching IDs, logs reply, then increments the local counter. On EOF/timeout/protocol error, close the stale writer and preserve the request number so the next user command can reconnect and retry logically.

- [ ] **Step 4: Implement interactive input separately from networking**

```python
async def run_interactive(client: ClientApp,
                          input_fn: Callable[[str], str] = input) -> None:
    while True:
        command = (await asyncio.to_thread(input_fn, "increment or quit> ")).strip().lower()
        if command in {"quit", "exit"}:
            return
        if command in {"", "increment"}:
            await client.send_increment()
```

Accept Enter or `increment` to send, and `quit`/`exit` to stop. Use `asyncio.to_thread(input_fn, prompt)` so terminal input does not block the event loop. Do not add automatic request generation in M1.

- [ ] **Step 5: Add client CLI and run tests**

The CLI parses `--config` and `--client-id`, resolves the first configured S1 endpoint, runs interactive mode, and always closes the app.

Run: `python -m pytest tests/integration/test_client.py tests/unit -v`

Expected: all tests pass.

- [ ] **Step 6: Commit client**

```bash
git add src/ft_system/client tests/integration/test_client.py
git commit -m "feat: add persistent interactive milestone one client"
```

## Task 8: LFD Heartbeats, Failure Detection, and Clean Reconnect

**Files:**
- Create: `src/ft_system/lfd/__init__.py`
- Create: `src/ft_system/lfd/app.py`
- Create: `src/ft_system/lfd/main.py`
- Test: `tests/integration/test_lfd.py`

**Interfaces:**
- Consumes: `LfdConfig`, `ServerConfig`, heartbeat protocol, transport, `ExponentialBackoff`, and `EventLogger`.
- Produces: `LfdApp.run()`, `LfdApp.stop()`, observable `heartbeat_count`/`healthy`, and LFD CLI.

- [ ] **Step 1: Write failing successful-heartbeat tests**

Define test-local `started_server()` and `endpoint_for_heartbeat(server)` using `bound_heartbeat_port`. Define `test_lfd_config(freq)` with 0.1-second connect/read timeouts and 0.01/0.05-second backoff. Use a short frequency and real S1 heartbeat port:

```python
@pytest.mark.asyncio
async def test_lfd_counts_and_logs_each_successful_heartbeat():
    server = await started_server()
    stream = io.StringIO()
    lfd = LfdApp(test_lfd_config(freq=0.02), endpoint_for_heartbeat(server),
                 EventLogger(stream=stream, use_color=False))
    task = asyncio.create_task(lfd.run())
    await eventually(lambda: lfd.heartbeat_count >= 4)
    assert lfd.healthy is True
    assert "[1] LFD1 sending heartbeat to S1" in stream.getvalue()
    assert "[1] LFD1 receives heartbeat from S1" in stream.getvalue()
    assert "[3] LFD1 sending heartbeat to S1" in stream.getvalue()
    await lfd.stop()
    await task
```

- [ ] **Step 2: Write failing failure and recovery tests**

Start healthy, close S1, wait for `healthy is False`, and assert `S1 has died` occurs exactly once despite multiple reconnect attempts. Restart S1 on the same heartbeat port, wait for `healthy is True`, assert one recovery log, assert backoff reset, and confirm heartbeat counts remain monotonic.

Add an ACK mismatch server that returns the wrong count; verify one mismatched ACK is treated as failure. Add a startup test where LFD starts before S1 and does not print `has died` before it has ever observed S1 healthy.

- [ ] **Step 3: Run LFD tests and confirm failure**

Run: `python -m pytest tests/integration/test_lfd.py -v`

Expected: import fails because `lfd.app` does not exist.

- [ ] **Step 4: Implement the heartbeat loop with only simple state**

```python
@dataclass(frozen=True)
class HeartbeatEndpoint:
    replica_id: str
    host: str
    port: int

class LfdApp:
    def __init__(self, config: LfdConfig, endpoint: HeartbeatEndpoint,
                 logger: EventLogger):
        self.config = config
        self.endpoint = endpoint
        self.logger = logger
        self._heartbeat_count = 1
        self._healthy = False
        self._ever_healthy = False
        self._stop_event = asyncio.Event()
```

Expose read-only `heartbeat_count` and `healthy` properties plus async `run()` and `stop()` methods. Use only booleans `healthy`, `ever_healthy`, and a stop event; do not introduce a connection-state enum. Each heartbeat cycle logs the current count, successfully writes it, immediately increments the stored count, then waits for the ACK carrying the sent count. Thus the counter counts transmitted heartbeats even when their ACK is lost. A failure closes the writer and logs death only on a healthy-to-failed transition. Reconnect waits on capped backoff; a successful ACK resets backoff and logs recovery only after a prior failure. `stop()` sets the event and closes the current writer so a pending read exits promptly.

- [ ] **Step 5: Add LFD CLI with heartbeat override**

Parse `--config`, `--lfd-id`, and optional `--heartbeat-freq`. If provided, create a replaced frozen `LfdConfig` using `dataclasses.replace`. Resolve `advertised_host` and `heartbeat_port` from the target S1 config. Install graceful signal handling and always call `stop()`.

- [ ] **Step 6: Run LFD and regression tests**

Run: `python -m pytest tests/integration/test_lfd.py tests/integration/test_server.py tests/unit -v`

Expected: all tests pass.

- [ ] **Step 7: Commit LFD**

```bash
git add src/ft_system/lfd tests/integration/test_lfd.py
git commit -m "feat: add configurable local fault detector"
```

## Task 9: Three-Client System Test, Smoke Script, and Operator Guide

**Files:**
- Create: `tests/integration/test_m1_system.py`
- Create: `scripts/m1_smoke.py`
- Create: `README.md`

**Interfaces:**
- Consumes: all prior public application interfaces and console entry points.
- Produces: one automated end-to-end acceptance test, one smoke command, and exact manual demo instructions.

- [ ] **Step 1: Write the failing three-client acceptance test**

Start S1, LFD1, and three `ClientApp` instances against dynamic ports. Send two requests per client concurrently and assert:

```python
assert server.state_machine.snapshot() == {"C1": 2, "C2": 2, "C3": 2}
assert all(client.next_request_num == 3 for client in clients)
assert lfd.healthy is True
```

Then close S1, use `eventually()` to observe `lfd.healthy is False`, assert the failure log occurs once, and cleanly close every task and stream in `finally`.

- [ ] **Step 2: Run acceptance test and fix only integration gaps**

Run: `python -m pytest tests/integration/test_m1_system.py -v`

Expected before final wiring: FAIL on the first missing cleanup or composition behavior. Make minimal corrections in the owning modules; do not add new architectural layers.

- [ ] **Step 3: Create a smoke runner using public APIs**

`scripts/m1_smoke.py` must:

1. load `configs/local.toml`;
2. start S1 and LFD1 in the same test event loop using their public app classes;
3. create C1-C3 and send one request each;
4. print the resulting state;
5. close S1 and wait for LFD1 to report failure;
6. close all clients and LFD in `finally`;
7. exit nonzero if any expected state or health transition is absent.

The smoke runner is an automated verification utility, not the manual milestone demonstration.

- [ ] **Step 4: Write the operator README**

Document:

- Python 3.11 virtual environment creation and `pip install -e '.[dev]'`;
- `python -m pytest -v` and `python scripts/m1_smoke.py`;
- five-terminal local demo commands for S1, LFD1, C1, C2, C3;
- entering requests and identifying exact required log lines;
- killing S1 with Ctrl-C and observing one timeout failure;
- rerunning LFD1 with `--heartbeat-freq 0.5`;
- copying and editing `distributed.example.toml` on every machine;
- firewall requirements for `client_port` and `heartbeat_port`;
- the rule that S1 and LFD1 must be colocated for the course demo;
- troubleshooting address-in-use, connection-refused, and timeout errors.

- [ ] **Step 5: Run all automated checks**

Run: `python -m pytest -v`

Expected: every unit and integration test passes.

Run: `python scripts/m1_smoke.py`

Expected: exits 0 after showing three request/reply state transitions, healthy heartbeats, and one S1 failure detection.

Run: `python -m compileall -q src tests scripts`

Expected: exits 0 with no syntax errors.

- [ ] **Step 6: Perform the manual rubric rehearsal**

In five terminals, run the documented local commands. Verify each rubric line visually, kill S1, confirm the LFD timeout, then repeat with a different heartbeat frequency. Record any wording mismatch as a test failure and correct `EventLogger` before proceeding.

- [ ] **Step 7: Commit the complete Milestone 1 deliverable**

```bash
git add README.md pyproject.toml scripts tests/integration/test_m1_system.py
git commit -m "feat: complete milestone one fault detection demo"
```

## Final Verification Checklist

- [ ] `python -m pytest -v` passes.
- [ ] `python scripts/m1_smoke.py` exits 0.
- [ ] `python -m compileall -q src tests scripts` exits 0.
- [ ] S1 binds two different ports.
- [ ] Heartbeats never change `my_state`.
- [ ] C1-C3 maintain independent `request_num` values.
- [ ] Required request/reply/state log wording matches the course rubric.
- [ ] LFD1 logs every heartbeat count.
- [ ] One ACK timeout marks a previously healthy S1 failed exactly once.
- [ ] Restarting S1 creates a fresh connection and restores healthy heartbeats.
- [ ] Local and distributed configurations are both documented and validated.
- [ ] No GFD, RM, replication, checkpointing, circuit breaker, or automated process restart has leaked into M1.
