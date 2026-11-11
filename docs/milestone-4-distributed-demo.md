# Milestone 4: four-computer demo guide

This guide runs the Milestone 4 demo on four computers, in two configurations:

- **Active replication with manual recovery.** Kill a replica, relaunch it by hand, and watch it log requests without replying (`i_am_ready = 0`) until a checkpoint from a healthy replica makes it ready.
- **Passive replication with manual recovery.** Kill and relaunch a backup; kill the primary and watch the RM elect a new one.

It also covers the preflight check before the demo, how to diagnose problems during it, and how to collect logs afterwards. See [demo-troubleshooting.md](demo-troubleshooting.md) for the full symptom table and [milestone-4-instructions.md](milestone-4-instructions.md) for the design.

## 1. Computers and windows

| Computer | Processes | Windows |
| --- | --- | ---: |
| A (sacred) | RM, GFD, C1, C2, C3 | 5 |
| B | LFD1, S1 | 2 |
| C | LFD2, S2 | 2 |
| D | LFD3, S3 | 2 |

That is **11 processes in 11 windows**. Each process gets its own window; don't paste several commands into one window. All four computers must be on the same network and able to reach each other. Guest Wi-Fi and some campus networks block traffic between devices; the doctor in section 4 detects this.

Rehearse on one computer first: `python scripts/m4_demo.py --autopilot` opens all eleven windows on one screen and runs the active demo by itself (`--mode passive` for the passive one, `--stop` to close the windows).

## 2. Prepare each computer

Run once on **each** computer, from the project root:

```bash
git fetch origin
git switch m4
git pull
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

After that, every new terminal starts with:

```bash
cd /path/to/Fall2026_18749_Project
source .venv/bin/activate
```

All four computers need the same commit. The doctor and every process print `build <commit>`, so a computer that missed `git pull` stands out.

> If a teammate fetched `m4` before 1 November 2026, run `git fetch origin && git reset --hard origin/m4` once while on `m4`. The branch was rewritten that day.

## 3. Create and share the configuration

1. On each computer, find its Wi-Fi IPv4 address and write it down:

   ```bash
   ipconfig getifaddr en0     # macOS (try en1 if en0 prints nothing)
   ```

2. On computer **A**, edit **both** `configs/m4.active.distributed.toml` and `configs/m4.passive.distributed.toml`. Change only the `advertised_host` values; keep every `bind_host = "0.0.0.0"`.

   | Section | `advertised_host` |
   | --- | --- |
   | `[rm]` and `[gfd]` | A's IP |
   | `[server.S1]` | B's IP |
   | `[server.S2]` | C's IP |
   | `[server.S3]` | D's IP |

   If you prefer your usual routine of copying a local config, start from `configs/m4.active.local.toml` and also change every `bind_host` to `"0.0.0.0"`. The doctor flags any that you miss.

3. Copy both files **unchanged** to B, C, and D (AirDrop, `scp`, or a USB stick). Don't edit them separately on each computer.

4. Allow incoming TCP on these ports in each computer's firewall:

   | Computer | Ports |
   | --- | --- |
   | A | 7300 (RM), 7200 (GFD) |
   | B | 5201, 6201, 8201 |
   | C | 5202, 6202, 8202 |
   | D | 5203, 6203, 8203 |

## 4. Preflight check: the doctor

Start the doctor on **all four computers at the same time**:

```bash
PYTHONPATH=src python -m ft_system.doctor --config configs/m4.active.distributed.toml
```

Each copy works out which computer it is on, listens on that computer's real ports, and keeps connecting to every port its processes will use. Leave all four running. Failures turn into `[OK]` on their own as the other computers start their doctors. Computer B's output looks like this:

```text
[INFO] build a610a44, config 2dacac23 (m4.active.distributed.toml, active); every computer must show the same two values
[INFO] this computer is 172.26.97.249 and runs S1, LFD1
[OK]   listening on 3 port(s) that S1 will use
[OK]   S1 -> RM port 172.26.36.169:7300: reached RM, GFD, C1, C2, C3
[OK]   S1 -> S2 checkpoint port 172.26.75.34:8202: reached S2, LFD2
[OK]   LFD1 -> GFD port 172.26.36.169:7200: reached RM, GFD, C1, C2, C3
...
PASS: this computer can reach everything its processes need, with the same code and config. ...
```

When **all four** print `PASS`, press Ctrl-C on each of them. If a line says `[FAIL]`, the indented lines under it say what to fix:

| Doctor says | Fix |
| --- | --- |
| `none of the config's addresses ... belongs to this computer` | This computer's IP changed. Use the address it prints, fix the config on A, and copy it again |
| `connection refused` | Start the doctor on that computer. If it is already running, that computer's firewall rejects the port |
| `no answer within 4 s` | Wrong IP, a different network, or Wi-Fi that blocks devices. Check the IP; if every cross-computer line times out, switch to a phone hotspot |
| `cannot listen on ...` | A process from an earlier run holds the port. Run the printed `lsof` command and `kill` it |
| `MISMATCH` | Different commit or config. Compare the `build`/`config` values in each computer's first `[INFO]` line; fix the one that differs |

On macOS, if a dialog asks whether Python may accept incoming connections, click **Allow**. Keep each computer awake by running `caffeinate -dis` in a spare terminal.

## 5. Start order

Every process prints one banner line first: `build ..., config ... (m4.active.distributed.toml, active)`. All 11 windows should show the same values.

**Computer A**, window 1 (RM), then window 2 (GFD):

```bash
PYTHONPATH=src python -m ft_system.rm.main  --config configs/m4.active.distributed.toml
PYTHONPATH=src python -m ft_system.gfd.main --config configs/m4.active.distributed.toml
```

**Computers B, C, D**, the LFD first:

```bash
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m4.active.distributed.toml --lfd-id LFD1   # B
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m4.active.distributed.toml --lfd-id LFD2   # C
PYTHONPATH=src python -m ft_system.lfd.main --config configs/m4.active.distributed.toml --lfd-id LFD3   # D
```

Then the replicas, **one at a time**, waiting for `RM: N members` before starting the next:

```bash
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S1   # B
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S2   # C
PYTHONPATH=src python -m ft_system.server.main --config configs/m4.active.distributed.toml --server-id S3   # D
```

**Computer A**, windows 3–5, after `RM: 3 members: S1, S2, S3`:

```bash
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C1 --interval 1
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C2 --interval 1
PYTHONPATH=src python -m ft_system.client.main --config configs/m4.active.distributed.toml --client-id C3 --interval 1
```

For the passive demo, use `configs/m4.passive.distributed.toml` in every command.

## 6. Demo sequence and checklist

### Active replication

1. **RM**: `RM: 0 members`.
2. **GFD**: `GFD: 0 members` and `GFD: notified RM: 0 members`. **RM** prints `RM: GFD registered` and `RM: 0 members` again.
3. **LFD1–3**: GFD prints `GFD: registered LFD1`, `LFD2`, `LFD3`. Membership stays 0 until the replicas start.
4. **S1**: it prints `S1: i_am_ready = 0 (waiting for the RM)`. LFD1 prints `LFD1: add replica S1`, then GFD prints `GFD: 1 member: S1` and RM prints `RM: 1 member: S1`. RM prints `S1 is the first replica; asking it to set i_am_ready = 1`, and S1 prints `S1: i_am_ready = 1 (set by the RM)`.
5. **S2, then S3**: GFD and RM print 2, then 3, members. About two seconds after each joins, RM prints `asking S1 to transfer its state to S2`. S1 prints `secondary channel to S2 open` and `S1 sending checkpoint <my_state = ..., checkpoint_count = 1> to S2`. S2 prints `received checkpoint` and `i_am_ready = 1 (checkpoint installed)`.
6. **C1–3**: each client sends every request to S1, S2, and S3, delivers the first reply, and prints `Discarded duplicate reply` twice per request.
7. **Fault**: press Ctrl-C in S2's window (computer C). LFD2 prints `S2 has died` and `LFD2: delete replica S2`. GFD and RM print `2 members: S1, S3`. The clients keep running, now with one duplicate per request.
8. **Manual recovery**: in S2's window, press ↑ and Enter to relaunch the same command.
   - LFD2 prints `S2 has recovered` and `add replica S2`, and GFD and RM print 3 members again.
   - The clients print `reconnected to S2`.
   - For about two seconds S2 prints `Received <C1, S2, n, request>`, each followed by `S2: i_am_ready = 0; logged <C1, S2, n, request> without replying (high_watermark_request_num[C1] = n)`. This is rubric step 21.
   - S1 then sends a checkpoint. S2 prints `i_am_ready = 1 (checkpoint installed)` and `replaying K logged requests`, then serves normally. The clients return to two duplicates per request.
9. Press **Enter in the RM window** to show the status snapshot: membership, ready replicas, and which components are registered.

When finished, press Ctrl-C in all 11 windows.

### Passive replication

Stop all 11 processes, then start them again in the same order with `configs/m4.passive.distributed.toml`. The banner must now say `passive`; the RM prints a red `MISMATCH` if any process still uses the active config.

1. Startup: RM prints `RM: primary is S1`. S1 prints `S1: role = primary`. S2 and S3 print `role = backup (primary is S1)` and become ready on S1's first checkpoint.
2. Every client reply comes from S1 (`Received <C1, S1, ...>`); S2 and S3 never reply. S1 prints `S1 sending checkpoint <my_state = ..., checkpoint_count = N> to S2` (and S3) every second; the backups print each `received checkpoint`.
3. **Backup fault and recovery**: press Ctrl-C in S2's window. Membership drops to `S1, S3`, and S1 keeps replying and checkpointing to S3. Relaunch S2: it starts with `i_am_ready = 0` as a backup and becomes ready on S1's next checkpoint.
4. **Primary fault and re-election**: press Ctrl-C in S1's window.
   - RM prints `primary S1 failed; new primary is S3`. The new primary is the ready backup that joined earliest; S2 was just relaunched, so S3 wins. Do step 4 before step 3 if you want S2 to take over.
   - The new primary prints `role = primary`, and its `checkpoint_count` continues from the last number it received.
   - The clients print `primary changed S1 -> S3` and resend their in-progress request with the same `request_num`.
5. Relaunch S1. It prints `role = backup (primary is S3)` and becomes ready on S3's checkpoint.

### Talking points for the project manager

- **Roles.** The GFD reports membership; the RM decides who is ready and who is primary, and pushes that view to every replica and client.
- **Recovery without losing or repeating requests.** A replica becomes ready only after a checkpoint. The checkpoint carries `my_state`, `checkpoint_count`, and the last `request_num` for each client, so the new replica replays only the logged requests the checkpoint does not include.
- **Passive trade-off.** Requests the old primary applied after its last checkpoint are lost, at most one `checkpoint_freq` worth. The new primary prints which request numbers were lost.

## 7. When something goes wrong

**Press Enter in any process window** to print a `STATUS` block. Lines starting with `hint:` say what is probably wrong. Start with the **RM** window, because it sees the whole system:

| What you see | What the RM status says | Fix |
| --- | --- | --- |
| A replica stays at `i_am_ready = 0` | `hint: S2 is alive ... but never registered with the RM` | S2's computer cannot reach A:7300 (firewall or IP) |
| Same | `hint: S2 is running but is not a member` | LFD2 is not running, or is not getting heartbeat replies from S2 |
| Same | `S2 waiting for state from S1: sent 3 time(s)` | Look for `checkpoint to S2` errors in S1's window; computer C's port 8202 is blocked |
| `RM: N members` never changes | `GFD NOT connected` | Start the RM before the GFD |
| Red `RM ERROR: MISMATCH: ...` | `MISMATCH S2 runs build ...` | That computer is on another commit or config: stop the process, `git pull` or re-copy the config, relaunch it |
| Passive clients never print `primary changed` | Shows the primary and who is ready | Press Enter in a client window; `RM NOT connected` means that client cannot reach the RM |

Clients started with `--interactive` read commands from the window, so Enter does not print status there.

## 8. After the demo: collect logs

Every process also writes its window's output to `logs/console/<date>/<process>.log`; its second line shows the exact path. After a failed rehearsal:

1. On each computer, copy `logs/console/<date>/` to one computer, into folders such as `run1/A`, `run1/B`, `run1/C`, `run1/D`.
2. Merge them into one timeline:

   ```bash
   python scripts/merge_logs.py run1 --since 14:05 --events        # events only, without request/reply traffic
   python scripts/merge_logs.py run1 --grep "S2|RM:"                # one replica's story
   ```

The result is plain text that can be pasted into a chat or a bug report.

## 9. Common problems

- **`Address already in use`**: a process from an earlier run is still running. Find it with `lsof -nP -iTCP:<port> -sTCP:LISTEN` and `kill` it, or close its window.
- **A computer's IP changed** (sleep, Wi-Fi reconnect): fix that address in both configs on A, copy them to every computer, restart all processes, and run the doctor again.
- **The demo works on one computer but not on four**: run the doctor. Nearly every four-computer failure is a network, firewall, IP, commit, or config problem, and the doctor reports each one with a fix.
- **Windows PowerShell**: replace `PYTHONPATH=src python ...` with `$env:PYTHONPATH = "src"; python ...`.
