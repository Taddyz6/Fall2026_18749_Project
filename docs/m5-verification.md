# M5 implementation and verification

This is the initial implementation record at `57f3a34`. The subsequent
[pre-push review and final checks](m5-review-verification.md) supersede its test
count and readiness/checkpoint-evidence findings.
The later [passive reply confirmation fix](m5-passive-commit-verification.md)
supersedes the acknowledged-update loss limitation described in this historical record.

2026-11-09, macOS / Python 3.13.2. Branch `m5` starts from `m4-fix` commit
`ee6bbc6`. The agreed scope is the M5 automatic-recovery rubric, tests, and
English/Chinese four-computer instructions. RM runtime configuration changes
were explicitly deferred.

The initial local suite was **225 passed**
(51 more than the 174-test M4 baseline), including real subprocess crashes in both
modes with each of S1/S2/S3 starting first. Physical four-computer execution remains
unverified; this report does not claim that the LAN demo has passed.

## Requirement and test traceability

Contracts, module boundaries and decisions are in [m5-design.md](m5-design.md).

| Requirement / acceptance | Implementation and test evidence | Result |
| --- | --- | --- |
| REQ-001 / AC-001, TC-001 | `rm/app.py`, `lfd/supervisor.py`, existing M4 state transfer; `test_three_crashes_are_recovered_by_rm_and_lfd_with_continuous_clients` in active mode, first S1/S2/S3. Each run kills the same replica twice, then a different replica. Checks client progress while the failed replica is absent, new PIDs, `3 -> 2 -> 3`, all ready, log-before-reply, no divergence, exactly 240 ordered replies per client, and final counters 240 at every replica. | Passed, 3 process scenarios |
| REQ-002 / AC-002, TC-002 | Same process test in passive mode, initial primary S1/S2/S3. Checks new primary election, client redirection, continued checkpoints, old primary returning as backup twice, another backup's recovery, and final state convergence. | Passed, 3 process scenarios |
| REQ-003 / AC-003, TC-003 | `test_rm_recovery.py`: wait for GFD membership before issuing commands, retry failed commands at configured cadence, stop retries on membership, reconnect LFD, pause commands without GFD, reject duplicate/unknown LFDs and mismatched results. `test_supervisor.py`: ten concurrent commands yield one owned child, repeated crashes, pre-existing manual process preservation, log/spawn permission failures, retry after repair. | Passed |
| REQ-004 / AC-004, TC-004/TC-005 | `test_recovery_contract.py`: all four M5 configs, opt-in compatibility, invalid settings, source/target/status/PID validation including absent PID. Supervisor tests verify normal shutdown, late commands and cancellation during process creation do not leave owned children. Original M1–M4 tests remain unchanged. | Passed |
| REQ-005 / AC-005, TC-006 | Four M5 configs; bilingual guides cover per-host addresses/ports, all launch commands, arbitrary initial primary, both three-fault sequences, PID injection, log windows, teardown and failure diagnosis. Configs loaded; Markdown links/fences and paths reviewed. | Documentation passed; physical LAN run pending |

All tests use actual behavior or protocol boundaries. The process harness never
starts a server itself: the RM and LFD must create every initial/replacement server.
Clients run throughout the three fault rounds and terminate only after finishing
their test workload. Each run also checks LFD-owned process/PID-file cleanup.

## Executed checks

Evidence is retained locally in Git-ignored `logs/m5-validation/`. The machine-
readable command summary is `checks.json`; six pytest process-scenario summaries
are preserved in `pytest-scenarios/`.

| Check | Command / method | Actual result |
| --- | --- | --- |
| M4 baseline | `.venv/bin/python -m pytest -q --tb=short` before implementation | 174 passed, 6.46s |
| Final full regression | `.venv/bin/python -m pytest -q --tb=short` | **225 passed, 97.76s**; `pytest.log` |
| M1 smoke | `.venv/bin/python scripts/m1_smoke.py` | PASS; `m1.log` |
| M2 smoke | `.venv/bin/python scripts/m2_smoke.py --output logs/m5-validation/m2` | PASS; each client completed requests 1–60; surviving S3 state 60/60/60 |
| M3 compatibility | Original `m3_smoke.py`, temporary copy of its config with all bind/advertised addresses changed to loopback and `src` linked to the current checkout | PASS; `m3-loopback.log` and `m3-loopback/` |
| M4 smoke | `.venv/bin/python scripts/m4_smoke.py --output logs/m5-validation/m4` | Active PASS, passive PASS; `m4.log` |
| M5 smoke | `.venv/bin/python scripts/m5_smoke.py --output logs/m5-validation/m5` | Active PASS, passive PASS; `m5.log` and `m5/20261109-225037-382881/{active,passive}/result.json` |
| Python compatibility syntax | `ast.parse(..., feature_version=(3, 11))` on 11 changed/new Python files | PASS; Python 3.11 itself was not runtime-tested |
| Configs and documentation | Load all four M5 configs; inspect commands/relative links/fenced blocks | PASS |
| Diff and syntax | `git diff --check`; `compileall` on affected sources/tests | PASS |

The standalone M5 smoke used shorter test periods than the distributed demo:
heartbeat 0.05s, restart delay 0.5s, retry 0.2s, active transfer delay 0.2s and
passive checkpoint period 0.05s. These single-run loopback observations are not
four-machine latency guarantees:

| Observation | Active | Passive |
| --- | --- | --- |
| Fault targets | S1, S1, S2 | S1 primary, S1 backup, S3 backup |
| Three recovery observations | 0.843s / 0.882s / 0.858s | 0.649s / 0.670s / 0.658s |
| Replies completed by each client | 240 | 240 |
| Maximum observed gap between replies | 0.036s | 0.053s |
| Final state at all three replicas | C1/C2/C3 = 240/240/240 | C1/C2/C3 = 238/238/238 |

The passive counter difference is the existing warm-passive loss window: two
increments per client in this run were acknowledged after the last checkpoint
before primary failure. Clients still delivered request IDs 1–240 in order, and
all replicas converged. This implementation does **not** promise zero loss of
acknowledged updates in warm-passive mode.

## Resolved failures and execution limits

- System `python3` lacks pytest; validation used the repository's `.venv`.
  Sandboxed TCP listeners initially failed with EPERM. The same baseline passed
  with approved local-network execution permissions.
- Early generated local config templates incorrectly replaced a comment containing
  `[replication]`, producing invalid TOML. Restricting insertion to the actual table
  header fixed the issue; all four config-validation cases now pass.
- Protocol review found that `failed`/`external` launch results without a `pid`
  field passed validation but were not safe for RM logging. Validation now requires
  explicit null PID and two regression cases cover this condition.
- The pre-existing `configs/m3.local.toml` contains LAN addresses, as already noted
  in the M4 verification report. The raw M3 smoke command was not counted as passing.
  Only a temporary loopback adaptation was executed; original M3 code/configs are
  untouched and the temporary directory was removed.
- Real four-host connectivity, firewalls, different host speeds, Linux/WSL runtime
  behavior and native Windows execution were not tested. The new process smoke
  runner explicitly requires macOS/Linux/WSL; it does not use POSIX PID probing
  on native Windows.
- RM, GFD and LFDs are assumed alive. A whole-machine/LFD crash, a partition, a hung
  child, and losing every in-memory state copy are outside this increment's fault
  model. Control channels retain the project's trusted-LAN assumption.

## Delivery and evidence score

- [English four-computer guide](milestone-5-instructions.md)
- [中文四机演示文档](milestone-5-instructions_zh.md)
- [Design, requirements and wire contracts](m5-design.md)
- `src/ft_system/lfd/supervisor.py`, RM/config/protocol integration,
  `configs/m5.*.toml`, `scripts/m5_smoke.py`, four added test files, and README.

Engineering evidence score: **93/100**. This is an implementation assessment,
not a course-grade prediction or a physical four-computer acceptance certificate.

| Dimension | Score | Evidence / deduction / next improvement |
| --- | --- | --- |
| Requirements and acceptance | 24/25 | All agreed local behaviors and bilingual instructions covered; execute the four-host acceptance sequence to close the remaining evidence gap |
| Correctness and boundaries | 24/25 | Real repeated crashes, ordering/readiness, role restoration, idempotence and failure cleanup pass; verify host/network timing under the physical topology |
| Tests and results | 17/20 | 225 tests plus all local smoke paths pass; no physical LAN or Python 3.11 runtime evidence, and M3 needs a temporary loopback config |
| Architecture and interfaces | 9/10 | Fixed local argv, opt-in config, isolated supervisor, existing transport/state transfer reused; LFD now has a wider lifecycle responsibility |
| Code quality and safety | 9/10 | No new runtime dependencies, owned-child handles, bounded retries and protocol validation; control connections still rely on a trusted network |
| Documentation and delivery | 10/10 | Equivalent English/Chinese procedures, four configs, traceable contracts/results, explicit failure and durability limits |
