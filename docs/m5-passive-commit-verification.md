# Passive acknowledged-request retention

Follow-up to `7d098ac` on `m5`. The user requested a minimal fix for acknowledged
passive requests being lost on primary failure, retaining periodic checkpoints,
tests and bilingual demo documentation. This supersedes the loss-window
assumption in the earlier M5 reports; instructor acceptance of that loss was
never established.

## Change and contract

REQ-006 / AC-006 and ADR-002 are recorded in [m5-design.md](m5-design.md).
For RM-managed passive replication, a successful client reply is released only
after the next periodic checkpoint covering that request has been installed and
acknowledged by every backup in the current membership, with at least one backup.
The existing secondary TCP channel carries a `checkpoint_ack` with the checkpoint
number. The RM's election rule, checkpoint period, deployment ports, configs and
automatic recovery mechanism are unchanged. No new dependency or service is added.

This batches requests in the periodic snapshot, so reply latency/throughput now
depends on `checkpoint_freq`. In the example with a 1s period, each sequential
client normally receives one reply per second. A failed backup can temporarily
delay confirmation until GFD/RM remove it. With no backup, successful replies
wait for one to return. Legacy passive mode without an RM retains its original
reply behavior; current M4 configs with an RM also gain the new confirmation rule.

The guarantee concerns the existing single-replica crash model with RM/GFD/LFDs
alive. State remains in memory. Partitions, whole-host infrastructure failures,
simultaneous loss of every copy and mixed versions on different hosts are outside
this change. Requests whose success reply was not received may be retried using
the same request number; installed checkpoints retain the duplicate-reply state.

## Regression and acceptance evidence

The new TCP regression first ran against the old implementation: after request 1
succeeded and the primary was immediately killed, request 2 returned **1** instead
of **2** (`before.log`: one failed test). The same test passes with checkpoint
confirmation. An initial affected-module run passed 68 tests; a subsequent
protocol and confirmation run passed 47 tests before the lost-ACK variant was added.

| Acceptance / test | Observable assertion |
| --- | --- |
| TC-007: `test_acknowledged_request_survives_primary_crash_before_next_checkpoint` | After successful value 1, immediate primary crash and failover, the next increment returns 2 |
| TC-007: `test_success_waits_for_checkpoint_installation_ack_from_every_backup` | Real TCP backup delays its ACK; one other backup's ACK cannot release the client reply. Includes an installed checkpoint whose ACK is lost, followed by periodic retry |
| TC-007: `test_retry_while_waiting_for_checkpoint_and_after_failover_is_not_applied_twice` | Two connections retry request 1 while waiting; both return 1, then failover retry still returns 1 and the next request returns 2 |
| IF-004 protocol tests | ACK identifies source, destination and positive checkpoint number |
| TC-001 / TC-002 process scenarios | Both active and passive require every reply's counter to equal its request number, and all final replica counters to equal completed requests; no weaker passive convergence-only check remains |

The full suite passed: **242 tests in 120.27s**, including all six real-process
scenarios (active/passive with S1/S2/S3 first). Each process scenario completed
240 requests per client, every reply matched its request number, and every final
replica held C1/C2/C3 = **240/240/240**. These runs include the primary crash,
the recovered replica's second crash and another replica's crash.

Original-period active and passive runs both passed, as did the M4 active/passive
manual-recovery smoke. All use the updated production code. The initial active
run preceded the smoke sampling fix; the final passive run uses that fix.

| Original-period observation | Active | Passive |
| --- | --- | --- |
| Three fault targets | S1, S1, S2 | S1 primary, S1 backup, S3 backup |
| Successful replies per client | 1200 | 30 |
| Every reply value equals its request number | Passed | Passed |
| Final C1/C2/C3 counters on every replica | 1200/1200/1200 | 30/30/30 |
| Maximum observed reply gap, including faults | 0.039s | 2.004s |
| Successful replies per client during each two-member interval | Verified by original progress check | 1 in each of the three rounds |

Evidence paths relative to `logs/m5-passive-commit/`:
`demo-timing/20261109-235921-553844/active/result.json`,
`demo-timing-fixed/20261110-000228-897624/passive/result.json`, and `m4.log`.
The final six-scenario rerun after the smoke-only change passed: **6 passed in
110.42s** (`process-final.log`). Each client's reply count and every final state
remain 240; every round additionally records successful replies through surviving
replicas while membership is two. Production code was unchanged after the
242-test full-suite run.
Raw evidence is retained locally under Git-ignored `logs/m5-passive-commit/`.

The first original-period run passed active mode but exposed a smoke observation
race in passive mode. All three clients logged reply 6 at `00:00:21.990`; the
LFD's launch-completion log was at `21.993`. A later 20ms poll saw the new PID and
incorrectly inferred there had been no service while the replica was absent.
The completion log also cannot establish the exact process-creation instant.
The test now checks the actual service requirement: each client must receive a
successful reply from a surviving replica between RM's two-member report and
the replica's rejoin. It retains the correct-value checks and records those
degraded-service reply counts. This removes the unrelated requirement to beat
OS process creation; the TCP regression independently verifies service after a
primary crash with no replacement launched at all. The failed run is retained
at `demo-timing/20261109-235921-553844/passive/result.json`.

## Delivery

- [English four-computer demo](milestone-5-instructions.md)
- [中文四机演示文档](milestone-5-instructions_zh.md)
- [Design and wire contract](m5-design.md)

The guides explain the ACK/response ordering, unchanged periodic checkpoints,
new reply pacing, timeout settings and failure observations. The smoke runner's
default workload is still 240 requests per client in both modes. `--demo-timing`
uses 1200 active or 30 passive requests per client so the original 1s passive
checkpoint period can be tested in a comparable run duration.

Physical four-computer execution and Python 3.11 runtime validation remain
unperformed. Local tests use Python 3.13.2 on macOS; Python 3.11 syntax is checked.

## Independent review

Standards: no confirmed blocking issue in the diff. Request waits release the
processing lock, ACK reads are serialized per peer, role changes wake pending
requests, and shutdown cancels them. No additional abstraction/refactor was needed.
The reviewer identified original-period reply/restart timing as requiring an
actual demo run, rather than accepting accelerated-test evidence alone.

Spec: no missing, additional or incorrectly implemented requirement found.
The review checked current-membership confirmation after transmission, join/remove
behavior, the no-backup case, role changes, retry deduplication and both language
guides. Both reviews were read-only; executed tests are recorded above.

Engineering evidence score: **94/100**.
This is an evidence assessment, not an instructor grade or a guarantee of zero bugs.

| Dimension | Score | Evidence / remaining gap |
| --- | --- | --- |
| Requirements and acceptance | 24/25 | REQ-006 tested alongside existing M5 sequences; physical demo pending |
| Correctness and boundaries | 24/25 | Reproduced data loss fixed; ACK delay/loss and retries covered; physical network timing unverified |
| Tests and results | 18/20 | 242 tests and original-period runs pass; Python 3.11 runtime and four-host execution not tested |
| Architecture and interfaces | 9/10 | One ACK type on the existing connection; no new service or dependencies; adds checkpoint-dependent response latency |
| Code quality | 9/10 | Local server/protocol/client changes; original trusted-network and crash-model assumptions remain |
| Documentation and delivery | 10/10 | Bilingual procedures, explicit reply semantics and timing, reproducible regression and historical failure records |
