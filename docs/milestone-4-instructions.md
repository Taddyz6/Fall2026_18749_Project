# Milestone 4: Replication Manager and manual recovery

M4 adds the Replication Manager (RM) and runs **11 processes**: RM, GFD, LFD1–LFD3, S1–S3, and C1–C3. The same code runs both required configurations: `mode = "active"` and `mode = "passive"`. A replica that is killed and relaunched by hand starts with `i_am_ready = 0`, gets correct state from a healthy replica, and then serves again. In passive mode, a dead primary is replaced by an elected backup.

## Layout and configuration

| Computer | Processes |
| --- | --- |
| A (sacred) | RM, GFD, C1, C2, C3 |
| B | S1, LFD1 |
| C | S2, LFD2 |
| D | S3, LFD3 |

| File | Use |
| --- | --- |
| `configs/m4.active.local.toml` / `configs/m4.passive.local.toml` | One computer, loopback addresses |
| `configs/m4.active.distributed.toml` / `configs/m4.passive.distributed.toml` | Four computers. Replace each `advertised_host` with that computer's current IPv4 address and copy the identical file to all four computers |

The two files of each pair differ only in `[replication]`. Ports: RM 7300, GFD 7200, client 5201–5203, heartbeat 6201–6203, checkpoint 8201–8203. Open them in each computer's firewall.

`[rm] transfer_delay = 2.0` makes the RM wait two seconds before sending state to a relaunched replica, so its "logged without replying" lines are easy to see. Use `--transfer-delay 0` on `ft_system.rm.main` for normal speed. `[replication] checkpoint_freq` sets the passive checkpoint period, and `--checkpoint-freq` on `ft_system.server.main` overrides it.

## Commands

Run each command in its own terminal from the project root. Use `configs/m4.passive.distributed.toml` for the passive demo.

```bash
# Computer A, in this order
PYTHONPATH=src python -m ft_system.rm.main  --config configs/m4.active.distributed.toml
PYTHONPATH=src python -m ft_system.gfd.main --config configs/m4.active.distributed.toml

# Computers B, C, D: the LFD first, then the replica (wait for each GFD/RM update before the next replica)
PYTHONPATH=src python -m ft_system.lfd.main    --config configs/m4.active.distributed.toml --lfd-id LFD1
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S1

# Computer A, after "RM: 3 members: S1, S2, S3"
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C1 --interval 1
```

To recover a replica manually, run the same `ft_system.server.main` command again on its computer. Leave the LFD running.

## Demo: active replication with manual recovery

1. Start the RM. It prints `RM: 0 members`.
2. Start the GFD. It prints `GFD: 0 members`, registers with the RM (`GFD: notified RM: 0 members`), and the RM prints `RM: 0 members` again.
3. Start LFD1–LFD3. The GFD prints `GFD: registered LFD1` and so on; GFD↔LFD heartbeats begin.
4. Start S1. It prints `S1: i_am_ready = 0 (waiting for the RM)`. After LFD1's first heartbeat: `LFD1: add replica S1`, then `GFD: 1 member: S1`, then `RM: 1 member: S1`. The RM sends `S1 is the first replica; asking it to set i_am_ready = 1`, and S1 prints `i_am_ready = 1 (set by the RM)`.
5. Start S2, then S3. The GFD and RM print 2, then 3 members. The RM prints `asking S1 to transfer its state to S2`. S1 prints `secondary channel to S2 open` and `S1 sending checkpoint <my_state = ..., checkpoint_count = N> to S2`. S2 prints the received checkpoint and `i_am_ready = 1 (checkpoint installed)`. This is the secondary channel of rubric step 9.
6. Start C1–C3. Each client prints the RM's view, sends every request to all three replicas, and discards two duplicates per request.
7. Press Ctrl-C in S2. LFD2 reports `S2 has died` and `delete replica S2`. The GFD and RM show `2 members: S1, S3`. The clients keep running with one duplicate per request.
8. Relaunch S2. LFD2 prints `S2 has recovered` and `add replica S2`; the GFD and RM show 3 members again. The clients print `reconnected to S2`.
9. While S2 waits for state (about `transfer_delay`), it prints `Received <C1, S2, n, request>` followed by `S2: i_am_ready = 0; logged <C1, S2, n, request> without replying (high_watermark_request_num[C1] = n)`. The clients receive no replies from S2.
10. S1 sends its checkpoint. S2 prints `i_am_ready = 1 (checkpoint installed)` and `replaying K logged requests`, then answers again. The clients go back to two duplicates per request. No client prints `replica state divergence`.

## Demo: passive replication with manual recovery

Use the passive config and the same start sequence. The RM prints `RM: primary is S1`; S1 prints `role = primary`; S2 and S3 print `role = backup (primary is S1)` and become ready on their first checkpoint. Every client reply comes from S1, and S1 sends `checkpoint <my_state, checkpoint_count>` to S2 and S3 every `checkpoint_freq`.

**Backup failure and recovery.** Press Ctrl-C in S2. Membership drops to `S1, S3`, and S1 keeps replying and checkpointing to S3. Relaunch S2. It rejoins with `i_am_ready = 0` as a backup and becomes ready on S1's next checkpoint.

**Primary failure and re-election.** Press Ctrl-C in S1. After detection the RM prints `primary S1 failed; new primary is S2`. The new primary is the ready backup that joined earliest; if S2 was just relaunched, S3 wins instead. The new primary prints `role = primary` and continues `checkpoint_count` from the last number it received. The clients print `primary changed S1 -> S2` and resend the request that was in progress, using the same `request_num`. Relaunch S1. It returns as a backup of the new primary and becomes ready on that primary's checkpoint.

## How it works

- **RM view.** The RM owns `{view_num, mode, members, ready, primary}`. Membership comes only from the GFD. Readiness comes from each replica's own report. The RM pushes every new view to replicas and clients, which hold one TCP connection to the RM each. Clients learn membership from the RM rather than the GFD because only the RM knows the passive primary. The guide allows alternative designs as long as the demonstrated behavior is preserved.
- **i_am_ready.** With an RM, every replica starts not ready. The first replica of its kind is told to become ready as it is. In active mode every later replica gets a checkpoint from the earliest ready replica, sent on the RM's `transfer_state` command. In passive mode a backup becomes ready on the primary's first periodic checkpoint.
- **Catch-up without losing requests.** A checkpoint carries `my_state`, `checkpoint_count`, and the last `request_num` applied for each client. The replica restores the state, rebuilds its duplicate-reply cache, and replays its logged requests in arrival order. It skips requests the state already includes, answers the boundary request from the cache, and applies newer requests. Each client's counter changes only on that client's requests, and each client's requests arrive in order, so this matches the donor exactly.
- **Late requests and reconnects.** An active replica also discards already-applied increments that arrive after checkpoint installation, keeping the client connection open. Active clients retry missing connections to current RM members every 0.5 seconds after each connection attempt completes, even when the view does not change. Requests to connected replicas continue during retries.
- **Gap detection.** An active replica that sees `request_num` jump (it missed requests another replica applied) sets `i_am_ready = 0` and asks for state again. A checkpoint older than the start of a replica's log is refused and retried. This handles a race between a client reconnecting and the snapshot being taken.
- **Election.** The new primary is the ready member that joined earliest; replicas are never hard-coded as primary. A relaunched replica always returns as a backup.

## Trade-offs and limitations (for Q&A)

- **Passive reply latency on the current M5 branch.** RM-managed passive replies wait until a periodic checkpoint covering the request is installed on all current backups (at least one). This also applies when running M4 configs with this code. Successful replies survive a primary crash; replies normally follow the checkpoint period. The earlier M4 implementation replied before this confirmation and had a loss window. Legacy M3 without an RM keeps its original behavior.
- **The RM is the authority on readiness.** If the RM is down, a new replica cannot become ready. The RM, GFD, and LFDs are accepted single points of failure. Components reconnect to a restarted RM, but restarting the RM mid-run is not part of the demo.
- **Catch-up relies on the application.** It depends on per-client independent state and per-connection FIFO order. A general application would need a total order across clients and a full message log.
- **Membership vs. readiness.** `RM: N members` counts live replicas, matching the GFD. A relaunched replica is a member for about `transfer_delay` before it is ready; the view lists both.
- **Manual recovery only.** M5 adds automatic relaunch by the RM or LFD.

## Verify

```bash
python3 -m pytest -q
python3 scripts/m4_smoke.py              # both modes; or --mode active / --mode passive
```

`m4_smoke.py` runs the 11-process topology on loopback with short timers. It kills and relaunches S2 in both modes, kills S1 in both modes (in passive mode this exercises primary election), and relaunches S1 in passive mode. It checks the RM, replica, and client logs, and saves them to `logs/m4-smoke/<mode>/`. It uses a hard kill, so it also runs on Windows. The pytest suite includes in-process versions of both scenarios (`tests/integration/test_m4_system.py`).

The Milestone 1–3 configs and demos are unchanged. A config without `[rm]` keeps the earlier behavior: replicas are ready at start and the passive primary is fixed by `primary_id`.
