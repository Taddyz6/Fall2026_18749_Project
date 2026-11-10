# M5 automatic recovery design

GOAL-001: Extend `m4-fix` (`ee6bbc6`) so the RM restores crashed active and
warm-passive replicas without an operator, while clients keep running.

## Requirements and acceptance

| Requirement / acceptance | Observable result | Implementation / tests |
| --- | --- | --- |
| REQ-001 / AC-001 | In active mode, kill any replica, kill its replacement, then kill a different replica. After each round, three replicas are ready, client requests progress, and replies do not diverge. | RM recovery scheduler, LFD supervisor; TC-001 process scenarios |
| REQ-002 / AC-002 | In passive mode, kill the primary, then its recovered backup, then the other backup. A new primary checkpoints and answers clients; all recovered replicas remain backups. Any replica can be the initial primary. | Existing RM election and state transfer; TC-002 process scenarios with S1/S2/S3 first |
| REQ-003 / AC-003 | Repeated/concurrent launch commands create at most one owned child. Failed launches retry at the configured cadence; stale commands never kill a running process. | Supervisor and scheduler; TC-003 lifecycle and failure tests |
| REQ-004 / AC-004 | M1–M4 configs retain manual startup/recovery. Invalid recovery config/messages fail clearly. LFD shutdown terminates/reaps only its owned child. | Config/protocol/CLI; TC-004 contract tests, TC-005 cleanup, existing regression suite |
| REQ-005 / AC-005 | English and Chinese guides contain executable commands for four hosts, both three-fault sequences, log evidence, shutdown and troubleshooting. | Four configs, bilingual guides and smoke runner; TC-006 document/config review |

CON-001: Python >=3.11, asyncio/TCP, standard library only. Preserve M4 warm-passive
checkpoint semantics and per-client counter model. RM/GFD/LFD failures and network
partitions remain outside the crash-replica fault model.

ASM-001: Each LFD runs on its replica's machine, with the same code/config available
locally. All peers are on a trusted demonstration network. The RM and GFD stay up.
Initial primary selection uses startup order: start the desired primary's LFD and
wait until that replica is ready before starting the other LFDs.

OOS-001: The user selected the M5 rubric scope. Runtime changes to replication
style, replica count and timing through an RM admin API are a separate feature.
No GUI, SSH provisioning, persistent recovery after loss of all replicas, or
change to warm-passive durability is included.

## ADR-001: execute recovery on the replica's host

Two practical options are RM-to-host SSH and an RM-to-LFD command channel. SSH
requires credentials, per-host paths and OS-specific commands. Choose the LFD
channel: the LFD already runs locally, and its existing process can own the
server child. This adds no ports, dependencies or extra service processes.

The trade-off is that an LFD now owns process lifecycle as well as heartbeats.
Keep lifecycle in `lfd/supervisor.py`; GFD membership and RM readiness remain
independent from whether the OS process exists. An alive process is not proof of
a healthy or ready replica.

## Modules and behavior

- MOD-001: `common/config.py`, `common/protocol.py`: opt-in recovery configuration
  and validated JSON-line messages. Existing protocol version and messages remain
  unchanged; all hosts in an M5 deployment must use the M5 code.
- MOD-002: `rm/app.py`: receive membership from GFD; for missing configured replicas
  with a registered LFD, periodically send `ensure_replica`. Do not send recovery
  commands until the GFD has reported membership. Rejoining replicas use the
  existing checkpoint/readiness flow. Never use a process-launch acknowledgement
  to mark a replica as a member or ready.
  A new server registration clears that ID's previous readiness; only its current
  connection may update status, so buffered frames from an older process cannot
  cancel the replacement's state transfer.
- MOD-003: `lfd/supervisor.py`, `lfd/main.py`: connect outbound to RM, validate local
  ownership, launch only the configured Python server entry point, append stdout
  to a local log and publish its PID. Serialize launches, retain the child handle,
  reap exits, and terminate owned children on normal LFD shutdown. Probe the
  heartbeat endpoint before spawning to avoid replacing a manually running server.
- MOD-004: `scripts/m5_smoke.py`: launch real RM/GFD/LFD/client processes; only LFDs
  launch server processes. Inject actual process crashes; never relaunch servers
  from the test harness. Verify all six rubric scenarios and write JSON evidence.

## Input and output contracts

IN-001: optional TOML `[recovery]`, `enabled` boolean (default false),
`restart_delay` finite seconds >=0 (default 1), `restart_retry` finite seconds >0
(default 1). Enabled recovery requires RM, GFD, replication and exactly one LFD
per configured server. Example: `enabled = true`, `restart_delay = 1.0`,
`restart_retry = 1.0`; `restart_retry = 0` is invalid. Missing section keeps M4.

IF-001: LFD opens an existing RM TCP listener and sends `hello` with its configured
LFD ID and `kind = "lfd"`. Unknown or duplicate live LFD registrations are rejected.
No authentication is added to the existing trusted-network protocol.

IF-002: RM -> LFD `rm_command`, payload
`{"command":"ensure_replica","target":"S2"}`. The destination and target must
match that LFD's local configuration. No executable, shell command or path is
accepted over the wire. Duplicate commands are idempotent while a child is alive.
Timeout/lost commands are retried; a reconnect gets another attempt if membership
still lacks the replica. A stale command does not terminate a healthy process.

OUT-001 / IF-003: LFD -> RM `launch_result`, payload
`{"replica_id":"S2","status":"started","pid":1234,"detail":"..."}`.
Statuses: `started` or `running` require a positive integer PID; `external` means
an existing heartbeat responder was found, and `failed` means launch failed;
both use null PID. `detail` is a nonempty string. RM validates the LFD/replica
mapping, logs the result, and still waits for GFD membership and replica readiness.

OUT-002: per-replica append-only stdout log and adjacent `.pid` file (decimal PID
plus newline). PID files are local operational aids, removed when the owned child
exits; they are not liveness/ownership authority. Normal shutdown uses the child
handle, never a PID read from disk. Failed starts leave diagnostics and retry at
the RM cadence. Log files are retained for review.
Config, log, PID and temporary PID paths must be distinct. This is a simple path
equality check before creating a child or modifying files; general filesystem
alias handling is outside the requested scope.

## Implementation and verification gates

1. Baseline: clean `m4-fix`; 174 tests pass using `.venv/bin/python` with loopback
   network permissions. System Python has no pytest; sandboxed listeners fail
   with EPERM. These are environment issues, not code failures.
2. Contracts, supervisor and RM scheduling: validate input/identity, retry and
   lifecycle behavior before process scenarios.
3. Both mode scenarios and compatibility: real crash/relaunch, clients continuing,
   state/readiness, roles, checkpoint continuation, duplicate-command behavior.
4. Bilingual four-machine runbook/configs and evidence report. Local execution
   cannot establish physical four-host connectivity; record that separately.

See `m5-verification.md` for the final requirement/test matrix and evidence score.
