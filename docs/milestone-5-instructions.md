# Milestone 5: automatic recovery on four computers

[中文版](milestone-5-instructions_zh.md) · [Design and contracts](m5-design.md) ·
[Verification evidence](m5-verification.md)

M5 extends M4 with RM-directed automatic process recovery in both active and
warm-passive replication. After each injected crash, the system returns to three
ready replicas while the three clients keep issuing requests. The demo performs
three successive crashes, including killing the same recovered replica twice.

The RM sends `ensure_replica` over an outbound LFD-to-RM TCP connection. That LFD
starts its local server using its own Python interpreter and absolute config
path. It does not need SSH, credentials, a shared filesystem, or a remote shell.
GFD heartbeats still establish membership; checkpoint installation establishes
readiness. An OS process having started is not enough to mark it ready.

## Files and setup

| File | Purpose |
| --- | --- |
| `configs/m5.active.local.toml` | All components on one computer, active |
| `configs/m5.passive.local.toml` | All components on one computer, passive |
| `configs/m5.active.distributed.toml` | Four-computer active demo |
| `configs/m5.passive.distributed.toml` | Four-computer passive demo |
| `scripts/m5_smoke.py` | Automated local crash/recovery acceptance test |

Use the same `m5` checkout on all four computers, with Python 3.11 or newer.
The commands below use macOS/Linux shells; use WSL on Windows for the same commands.
From the repository root on each computer:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Runtime code has no third-party dependencies. The `dev` extra is for pytest.
Each computer may use a different checkout path. Stop any earlier milestone
demo first because the example configs reuse its ports.

## Four-computer layout

| Computer | Example IPv4 | Components | Inbound TCP ports |
| --- | --- | --- | --- |
| A (sacred) | `192.168.1.10` | RM, GFD, C1, C2, C3 | 7300, 7200 |
| B | `192.168.1.11` | LFD1 and its child S1 | 5201, 6201, 8201 |
| C | `192.168.1.12` | LFD2 and its child S2 | 5202, 6202, 8202 |
| D | `192.168.1.13` | LFD3 and its child S3 | 5203, 6203, 8203 |

Replace **every `advertised_host`** in both distributed configs: `[rm]` and `[gfd]`
use A's current IPv4, and `[server.S1]`/S2/S3 use B/C/D's IPv4 respectively. Keep
`bind_host = "0.0.0.0"`. Find addresses with `ipconfig getifaddr en0` on a Mac
(choose the active interface if different), or `ip -4 addr` on Linux. Use a LAN
where the four machines can reach each other; permit the listed ports in their
firewalls. Copy each edited config identically to all four hosts.

All config paths in launch commands refer to files on the **local** host. Check
the configs before opening the demo windows:

```bash
.venv/bin/python -c 'from ft_system.common.config import load_config; c = load_config("configs/m5.active.distributed.toml"); print(c.recovery); print(c.servers)'
```

There are **11 application processes**: RM, GFD, three LFDs, three server children,
and three clients. Use 11 visible terminal windows: eight for directly launched
components and three `tail` windows displaying the server logs. The `tail`
processes are only log viewers, not additional fault-tolerance components.

## Start the active demo

Run each command in its own terminal, from that computer's repository root.

On **A**, start RM first, then GFD:

```bash
.venv/bin/python -m ft_system.rm.main --config configs/m5.active.distributed.toml
.venv/bin/python -m ft_system.gfd.main --config configs/m5.active.distributed.toml
```

Check `RM: 0 members`, `GFD: 0 members`, and GFD's notification of RM. From B/C/D,
`nc -vz 192.168.1.10 7300` and `nc -vz 192.168.1.10 7200` can check connectivity
to the running listeners (substitute A's actual address).

On **B**, start LFD1; it will receive an RM command and launch S1 automatically:

```bash
.venv/bin/python -m ft_system.lfd.main --config configs/m5.active.distributed.toml --lfd-id LFD1 --replica-log logs/m5-active/S1.log
```

Wait for `RM: S1 is ready (i_am_ready = 1)`. On **C**, then on **D**, start the
corresponding LFD, waiting for readiness after each one:

```bash
# C
.venv/bin/python -m ft_system.lfd.main --config configs/m5.active.distributed.toml --lfd-id LFD2 --replica-log logs/m5-active/S2.log
# D
.venv/bin/python -m ft_system.lfd.main --config configs/m5.active.distributed.toml --lfd-id LFD3 --replica-log logs/m5-active/S3.log
```

On B/C/D, open a second terminal for that server's output:

```bash
# B; use S2 on C and S3 on D
tail -n 80 -F logs/m5-active/S1.log
```

Wait for the RM's latest view to show `members: S1, S2, S3; ready: S1, S2, S3`.
Then start each client in a separate terminal on **A**:

```bash
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C1 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C2 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C3 --interval 0.2
```

Clients run indefinitely. Active requests go to all replicas; each client delivers
one reply and discards duplicates. The recovered server's log is appended to the
same file so its `tail` window follows successive incarnations.
Use distinct paths for the config, server log, adjacent `.pid` file and `.pid.tmp`
file; the LFD rejects directly repeated paths before launching.

## Active fault sequence

Use the current PID written by the local LFD; it changes after every relaunch.
For example, on **C**:

```bash
cat logs/m5-active/S2.pid
ps -p "$(cat logs/m5-active/S2.pid)" -o pid=,command=
kill -KILL "$(cat logs/m5-active/S2.pid)"
```

The command should identify `ft_system.server.main --server-id S2`. Kill the
server, leaving its LFD running. **Ctrl-C in a `tail` window only stops the viewer**;
it does not inject a replica crash. Do not run a separate server launch command.

| Round | Action | Required evidence |
| --- | --- | --- |
| 1 | Kill any replica, e.g. S2 | LFD detects failure; GFD/RM show two members; clients progress; RM asks LFD2 to launch; a new S2 PID appears; state transfers; three replicas become ready |
| 2 | After full recovery, kill that same S2 again using its new PID | Repeat the full automatic cycle, including a second state transfer |
| 3 | After full recovery, kill another replica, e.g. S3 on D | Repeat; all three clients continue and all three replicas become ready again |

For every recovery, observe this order:

1. `S2 has died` and `delete replica S2`; membership becomes `S1, S3`.
2. `RM: asking LFD2 to ensure replica S2 is running`.
3. `LFD2: launched S2 pid=...`; the new S2 starts with `i_am_ready = 0`.
4. S2 logs incoming active requests without replying, receives a checkpoint,
   replays uncovered requests, and becomes ready.
5. The RM view includes all three ready replicas. Duplicate replies from S2 resume;
   no client should print `replica state divergence`.

Wait for **readiness**, not just `RM: 3 members`, before injecting the next crash.
For state evidence, use `installed checkpoint` and `my_state_S2` lines. A
`received checkpoint` line alone does not prove installation: a late checkpoint
can be received and ignored after the replica is already ready.

## Passive fault sequence

Shut down the active run using the sequence below. Repeat the startup commands
with `m5.passive.distributed.toml` and `logs/m5-passive/` in place of the active
paths. Do not run the modes concurrently on the same ports.

To designate **any** initial primary, start its LFD first and wait until its
replica is ready before starting the other two. For example, start LFD2 on C first
to make S2 primary. Primary selection is not hard-coded to S1. The following table
uses S1 as the initial primary; always follow the actual RM view.

| Round | Action | Required evidence |
| --- | --- | --- |
| 1 | Kill current primary S1 on B | RM elects a ready backup; clients follow the new primary; checkpoints continue; S1 is automatically restarted as a **backup**, receives the new primary's checkpoint, and becomes ready |
| 2 | Kill the recovered S1 backup again | RM/LFD1 recover it as a backup again; current primary continues replying and checkpointing |
| 3 | Kill the other backup (not the current primary) | That backup automatically returns, receives checkpoints, and all three replicas are ready again |

Use the same PID-based kill command with `logs/m5-passive/<replica>.pid`. Verify:

- `RM: primary S1 failed; new primary is ...`, plus each client's `primary changed`.
- The new primary sends checkpoints and answers all three clients.
- The recovered old primary logs `role = backup (primary is ...)`, installs a
  checkpoint, and does not take the primary role back.
- Each round produces membership `3 -> 2 -> 3`, then three ready replicas.
- Clients keep running throughout; do not stop/restart them between rounds.
- For each client, `request_num N: client_value = N` continues across failover.
  The next successful increment after a primary crash must not repeat an earlier value.

Checkpointing remains periodic. The primary applies a request, then waits for
the next checkpoint that covers it. Each backup acknowledges only after installing
that checkpoint. Once all backups in the current RM membership have confirmed
(at least one), the primary logs `checkpoint N confirmed by ...; client replies
released` and sends the successful replies. A successful reply therefore survives
a subsequent single primary crash; a retried request is still applied only once.

With the example's 1s checkpoint period, each client's replies normally arrive
about once per second. All three clients can be covered by one checkpoint. No
per-request checkpoint is added. Requests without a successful reply can be
retried after failover. If no backup is available, successful replies wait until
one returns; a single in-memory copy cannot promise survival of its own crash.
Keep the client read timeout longer than the checkpoint period plus transfer time
when changing demo settings. All hosts must run this same version of the code.
Detection/election/reconnection can also delay responses; record response gaps
instead of claiming literally zero latency.

### Explaining consistency, latency and continuous operation

In `18-749 _ The Project.docx`, the FAQ **“What do you mean by trade-offs in the
system?”** explicitly discusses consistency versus performance, including
“a blocking implementation that then affects latency”. The FAQ **“Should the
clients be automated, and running in a loop?”** requires the application to keep
running during the demo. M5's passive rubric also expects clients to receive
responses while the group temporarily has two replicas and RM recovers the third.
Together, these support the following interpretation of continuous operation:
clients keep their automatic loops running, and surviving replicas keep completing
requests throughout recovery. The supplied rubric specifies no fixed request rate
or maximum response latency.

Each client uses a sequential request–reply loop: it waits for the current reply
before sending the next request. Checkpoint confirmation therefore lowers its
request rate and adds response latency. The wait releases the server's request
processing lock, allowing other requests, checkpoints and heartbeats to proceed.
Clients automatically retry through failover without being manually paused or
restarted. In return, a successful reply represents state already installed on
the backups eligible for promotion, preserving acknowledged updates after a
single primary crash under the fault model described below.

For acceptance, show successful replies from surviving replicas **during each
two-member interval**, progressing per-client counters and continuing checkpoints,
followed by automatic recovery to three ready replicas. Keep response timestamps
to explain any gaps. In the [local verification run](m5-passive-commit-verification.md)
with the original 1s checkpoint period, all three clients received a successful
reply in each of the three two-member intervals. The maximum observed reply gap,
including failover, was **2.004s**; each client completed 30 requests and every
replica ended at **30/30/30**. These are local observations, not a latency bound;
repeat the check on the four demo computers.

Suggested explanation during the demo:

> Our clients keep running their automatic request–reply loops throughout the
> failures. We retain periodic checkpoints and reply successfully only after the
> backups install the request's state, trading response latency for preservation
> of acknowledged updates. While one replica is recovering, the surviving two
> continue completing requests, and no client needs to be paused or restarted.

## Recovery settings and shutdown

```toml
[recovery]
enabled = true
restart_delay = 1.0
restart_retry = 1.0
```

`restart_delay` is the number of seconds RM waits after an eligible missing
replica is observed; `restart_retry` is the retry period until GFD sees it again.
Repeated commands reuse a live owned child. An existing manually launched
heartbeat responder is left alone. A failed start is logged and retried; a live
but hung child is not duplicated. `rm.transfer_delay` retains the active demo's
state-transfer delay, and `replication.checkpoint_freq` controls passive snapshots.
The recovery section is opt-in; existing M1–M4 configs remain manual.

Stop clients, then **RM before LFDs**, then stop each LFD with Ctrl-C, then GFD.
LFD shutdown terminates and reaps its owned server and removes its PID file.
Stop the `tail` viewers last. Do not use `kill -9` on an LFD for normal teardown;
the LFD needs to run its cleanup. Logs remain available for review.

## Tests and troubleshooting

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/m5_smoke.py
.venv/bin/python scripts/m5_smoke.py --mode passive --first S2
.venv/bin/python scripts/m5_smoke.py --demo-timing
```

The smoke runner allocates loopback ports, launches all 11 application processes,
and injects real crashes. Only the LFDs launch the server children; the harness
never relaunches them. By default, it shortens timers and each client completes
240 requests. `--demo-timing` preserves the demo config's periods/timeouts and
uses 1200 active or 30 passive requests per client, accounting for checkpoint-paced
passive replies. It checks membership, readiness, roles, checkpoints, ordered
client progress, every reply value and final counter in both modes, and
cleanup. Output goes into a fresh timestamped directory under `logs/m5-smoke/`,
with configs, process logs and per-mode `result.json` files. pytest also runs both
modes starting with each of S1/S2/S3. These are local tests; complete the four-host
sequences above separately and keep the console logs as physical-network evidence.

| Symptom | Check |
| --- | --- |
| LFD registered but no replica starts | RM/GFD running; identical config; `[recovery].enabled`; RM's GFD membership report and LFD registration |
| Repeated `launch ... failed` or rapid child exits | LFD diagnostics and server log; interpreter/install; local config path; writable log directory; occupied ports |
| RM receives `external` launch status | A manually started server is responding; stop that earlier demo before beginning a managed M5 run |
| Three members but fewer ready | Check checkpoint ports/connectivity, donor/primary logs and `i_am_ready`; do not inject another fault yet |
| Clients cannot reach replicas | Use advertised LAN addresses, not `0.0.0.0`/loopback; verify firewall and network isolation |
| PID file briefly absent after a kill | The child was reaped and replacement is pending; wait for the new launch and ready view |
| Passive replies wait while heartbeats continue | Check the checkpoint period, backup installation ACKs and current membership; replies require at least one backup and confirmation from all current backups |

The M5 rubric covers successive replica-process crashes with RM/GFD/LFDs alive.
Machine/LFD crashes, partitions, all-replica state loss and RM runtime configuration
changes are not covered by this increment. Runtime configuration was explicitly
deferred for this branch.
