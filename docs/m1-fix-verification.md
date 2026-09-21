# M1 Fix Verification — 2026-09-21

Scope: documentation organization and automatic client sending on branch
`origin/m1-fix`. This report evaluates this change, not production readiness.

## Requirements and Design

- GOAL-001: make the repository easy to navigate and run M1 clients without
  repeatedly entering commands.
- REQ-001 / AC-001: retain a project overview in README and link to the complete
  M1 instructions in `docs/`; all local document links must resolve.
- REQ-002 / AC-002: clients send immediately without stdin, then wait a configurable
  interval after each attempt; invalid intervals must fail at argument parsing.
- REQ-003 / AC-003: connection failures must not stop the automatic loop or cause
  busy retries; clients reconnect, stop on Ctrl-C, and retain optional manual mode.
- ASM-001: automatic mode is the default, with a one-second interval.
- CON-001: preserve the existing protocol, configuration schema, dependencies,
  and user changes to distributed configurations.
- OOS-001: request deduplication, persistence, replication, and failover.

MOD-001 (`client/app.py`) adds a sequential automatic runner around the existing
`send_increment` API. It logs expected transport/protocol failures and waits after
both successful and failed attempts. Cancellation propagates to the CLI, whose
existing `finally` block closes the client.

MOD-002 (`client/main.py`) selects automatic or interactive mode. IF-001 adds
optional `--interval` (finite positive floating-point seconds, default `1.0`) and
`--interactive` (boolean flag, default false). `--interval 0.5` is valid;
`--interval 0`, negative values, NaN, infinity, and nonnumeric values are rejected
with argparse exit status 2. Manual mode ignores the interval. Existing request
and reply log formats remain unchanged; Ctrl-C exits with status 0.

## Acceptance Evidence

| Case | Requirement | Level / priority | Execution and expected result | Result |
| --- | --- | --- | --- | --- |
| TC-001 | REQ-001 / AC-001 | Document check / P1 | Resolve README-to-guide and guide-to-README links; check balanced code fences and remove stale directory commands | Passed |
| TC-002 | REQ-002 / AC-002 | Unit / P1 | `test_cli_defaults_to_automatic_with_one_second_interval` and invalid-interval tests verify defaults and reject invalid values before sending | Passed |
| TC-003 | REQ-002, REQ-003 / AC-002, AC-003 | Unit / P1 | `test_loop_paces_success_and_failure_and_propagates_cancellation` injects a timeout followed by success; both attempts wait 0.5 seconds through the mocked sleep boundary; cancellation propagates | Passed |
| TC-004 | REQ-002, REQ-003 / AC-002, AC-003 | CLI integration / P1 | `test_cli_sends_and_exits_in_automatic_and_manual_modes[False]` runs a real client with stdin closed, observes repeated increments, disconnects and replaces S1 at the same endpoint, observes resumed increments, and sends SIGINT; exit status is 0 with no stderr | Passed |
| TC-005 | REQ-003 / AC-003 | CLI integration / P1 | The same CLI test with `[True]` sends `increment`, Enter, and `quit`; exactly two increments occur and the process exits cleanly | Passed |
| TC-006 | REQ-002, REQ-003 | Regression / P1 | Full pytest suite, including original protocol, server, LFD, client, and system behavior | 101 passed |
| TC-007 | REQ-003 | System smoke / P1 | `scripts/m1_smoke.py` verifies one increment per client, heartbeats, and failure detection | SMOKE PASS |

Commands executed from the repository root:

```bash
.venv/bin/python -m pytest -q tests/unit/test_client_loop.py tests/integration/test_client_cli.py tests/integration/test_client.py --tb=short
.venv/bin/python -m pytest -q --tb=short
.venv/bin/python scripts/m1_smoke.py
git diff --check
```

The targeted run passed 21 tests. After strengthening the restart scenario to use
a fresh server instance, the complete suite passed 101 tests. TCP tests and smoke
checks ran with permission to bind local loopback ports. No test failures occurred
during this change. Documentation links and fenced code blocks were also checked.

## Limits and Evidence Score

No physical multi-machine deployment or additional Python/platform combination
was tested. Validation used the existing macOS/Python 3.13 environment and local
TCP sockets. M1 still has in-memory counters and no request deduplication: a retry
after a lost reply can repeat an operation, and a new server resets its counters.

| Dimension | Score | Evidence / deduction |
| --- | --- | --- |
| Requirements and acceptance | 25/25 | AC-001 through AC-003 passed |
| Correctness and boundaries | 25/25 | Repeated sending, invalid inputs, failure pacing, restart recovery, manual mode, and cancellation covered |
| Test sufficiency and results | 16/20 | 101 tests and smoke passed; minus 2 for no physical multi-machine run and 2 for no additional Python/platform run |
| Architecture and interfaces | 10/10 | Existing request API reused; no protocol or configuration migration |
| Quality and maintainability | 10/10 | Small sequential runner, explicit validation, existing cleanup path, no dependencies added |
| Documentation and delivery | 10/10 | Overview, detailed instructions, behavior changes, limitations, and this evidence report |
| Total | 96/100 | For this change only |

Remaining validation opportunities are a physical LAN demo and a run on the
minimum supported Python 3.11 environment.
