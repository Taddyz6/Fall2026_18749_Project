# M5 pre-push review and verification

2026-11-09, macOS / Python 3.13.2. Review baseline: M5 implementation `57f3a34`,
based on `m4-fix` at `ee6bbc6`. Scope remains the agreed M5 automatic-recovery
rubric, tests and English/Chinese four-computer demo. This records the review
after the [initial implementation validation](m5-verification.md).

## Standards review

The implementation and requirements were reviewed independently, then the fixes
were re-reviewed. The following concrete defects were resolved without adding a
new framework, dependency or service:

| Finding | Change | Regression evidence |
| --- | --- | --- |
| P2: log/PID/config paths could directly overlap and damage restart inputs/output | A simple path-equality check rejects the four directly overlapping paths before launch | Four cases in `test_rejects_colliding_output_paths_without_modifying_files` |
| P2: the server treated an LFD-only command as a state transfer; wrong-destination control was accepted | Explicit server command dispatch and RM source/destination checks | Three cases in `test_server_rejects_lfd_only_or_misaddressed_control_messages` |
| P2: the smoke test could count a received but ignored checkpoint as installed state | Log the actual post-install/replay snapshot; read that evidence in the smoke test | `test_smoke_state_uses_installed_not_ignored_checkpoints`, active and passive |

The user requested minimal task-focused code. General hardlink/symlink alias
protection and its two exploratory cases were removed from scope; ordinary path
equality validation remains. This scope reduction is not a claim that arbitrary
filesystem aliases are supported. No unrelated refactor was introduced.

Standards re-review found no remaining blocking issue within this scope. The
reviewer inspected code and tests; executed results below come from the main run.

## Spec review

`18-749 _ The Project.docx` (Milestone 5) and `02-18749-ProjectOverview.pdf` were
rechecked as requirements sources, not as instructions to the agent. Stable requirements
and acceptance IDs remain in [m5-design.md](m5-design.md).

| Finding / acceptance | Resolution and evidence |
| --- | --- |
| P1: a buffered `ready=True` from an old server connection could cancel the replacement's state transfer | New registration clears prior readiness; only the current peer may report status. Two real-TCP regressions cover an old delayed frame and GFD membership arriving before the replacement's first status |
| REQ-001 / active M5 sequence | Kill any replica, kill its recovered incarnation again, then another replica; all three clients continue, new PIDs appear, membership returns to three, checkpoint/replay precedes replies and final states agree |
| REQ-002 / passive M5 sequence | Kill the primary, its recovered backup, then the other backup; a ready backup becomes primary, checkpoints continue and recovered replicas remain backups |
| Arbitrary initial primary | Full process scenarios start with each of S1, S2 and S3 in both modes |
| REQ-003 / REQ-004 | RM/LFD perform every restart; retries and owned-child lifecycle remain covered; existing M1–M4 tests remain in the full suite |
| REQ-005 | Both four-computer guides include launch, fault injection, readiness evidence and shutdown, plus accelerated and original-timing local tests |

Spec re-review found the original blocking defect fixed and no new blocking
issue. Physical four-computer execution remains pending. The broader overview's
runtime RM configuration changes remain explicitly deferred by the user's earlier
scope decision; this report does not claim the complete final-course feature list.

## Executed checks

Evidence is saved locally under Git-ignored `logs/m5-review/`. The initial bug
reproduction run had 12 failures and 9 passes. One of those failures was the
subsequently excluded hardlink case. The additional dangling-link case was also
reproduced before being excluded. All 11 retained regression cases failed before
their fixes. An intermediate targeted run passed 86 tests; the final full run
below is the acceptance result for the simplified implementation.

| Final check | Actual result | Local evidence |
| --- | --- | --- |
| `.venv/bin/python -m pytest -q --tb=short` | **236 passed, 99.14s**; 11 more regressions than the initial 225 | `pytest.log` |
| Both modes × first S1/S2/S3, real subprocess crashes | All six scenarios passed, three crashes each, 240 replies per client | `pytest-scenarios/{active,passive}-{S1,S2,S3}.json` |
| `scripts/m4_smoke.py --output logs/m5-review/m4` | Active and passive passed | `m4.log`, `m4/` |
| Unchanged M3 smoke with temporary loopback config | Passed; temporary copy removed, tracked LAN config unchanged | `m3-loopback.log`, `m3-loopback/` |
| Four M5 configs and demo timing equivalence | All loaded; local/distributed/executed periods and timeouts agree | `static-checks.json` |
| Python 3.11 syntax, Markdown links/fences, diff whitespace | 14 affected Python files parsed; six documents checked; `git diff --check` passed | `static-checks.json`, terminal checks |

M1/M2 behavior is also covered by the full regression suite; their earlier
standalone smoke results are in the initial report. TCP test fixtures require
local-network execution permission: one sandboxed exploratory check failed at
socket bind (EPERM), then reproduced the intended case with the same approved
permissions used for the rest of the suite. It was not counted as a code failure.

The original-timing test was run with:

```bash
.venv/bin/python scripts/m5_smoke.py --demo-timing --output logs/m5-review/demo-timing
```

The generated configs match the distributed configs except for hosts and ports:
heartbeat/restart/retry/checkpoint periods remain 1s, and active transfer delay
remains 2s. Each client sends 1200 requests at a 0.025s interval. These are real
processes on loopback, not a physical-network measurement.

| Observation | Active | Passive |
| --- | --- | --- |
| Result | Passed | Passed |
| Fault targets | S1, S1, S2 | S1 primary, S1 backup, S3 backup |
| Observed time until ready, three rounds | 4.265s / 4.410s / 4.089s | 2.796s / 2.018s / 2.009s |
| Ordered replies per client | 1200 | 1200 |
| Maximum observed reply gap | 0.040s | 0.823s |
| Final state on each replica, C1/C2/C3 | 1200 / 1200 / 1200 | 1195 / 1195 / 1195 |

Evidence: `demo-timing/20261109-231303-873501/{active,passive}/result.json` and
adjacent process logs. Both runs also verified PID changes and process cleanup.
The passive difference is the inherited warm-passive checkpoint loss window:
five acknowledged requests per client preceded the failed primary's next
checkpoint. All surviving/recovered states converge, but zero acknowledged-update
loss is not promised. Client operation continues through a finite failover delay;
the course's continuous-service requirement is not a zero-latency claim.

## Limits and evidence score

Actual four-host connectivity/firewalls and Linux/WSL execution remain untested.
Python 3.11 syntax compatibility was checked; the runtime used Python 3.13.2.
RM/GFD/LFD failures, partitions and simultaneous loss of every state copy remain
outside this increment. Native Windows process-smoke execution is not supported.

Engineering evidence score: **94/100**. This assesses available evidence, not the
course grade or certainty of zero bugs. Local acceptance for the agreed M5 scope
passed; the four-host acceptance remains a separate execution step.

| Dimension | Score | Basis / remaining gap |
| --- | --- | --- |
| Requirements and acceptance | 24/25 | Rubric mapped to both-mode scenarios and bilingual guides; physical demo pending |
| Correctness and boundaries | 24/25 | Reproduced readiness race and evidence defects fixed; LAN timing unverified |
| Tests and results | 18/20 | 236 tests, six process scenarios and original-period runs pass; no Python 3.11 runtime or physical LAN run |
| Architecture and interfaces | 9/10 | Existing transport/state transfer reused, fixed local launch argv; LFD also owns child lifecycle |
| Code quality | 9/10 | Small scoped fixes, no dependencies/frameworks; existing trusted-network assumption |
| Documentation and delivery | 10/10 | Equivalent bilingual demo procedures, reproducible commands and disclosed limits |
