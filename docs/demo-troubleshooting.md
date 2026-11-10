# Four-laptop demo: preflight and troubleshooting

Three tools, one for each stage:

| When | Tool | What it answers |
| --- | --- | --- |
| Before starting anything | `python -m ft_system.doctor` on all four laptops | Can every process reach every port it needs? Do all laptops run the same code and config? |
| During the demo | Startup banner, RM `MISMATCH` errors, **Enter** in any window | What does this process think is happening right now, and why is something stuck? |
| After a failed run | `logs/console/<date>/` on each laptop + `scripts/merge_logs.py` | What happened, in order, across all 11 processes? |

## 1. Before the demo (about 10 minutes)

1. On all four laptops: `git switch m4 && git pull`.
2. On laptop A, set the four IP addresses in `configs/m4.active.distributed.toml` and `configs/m4.passive.distributed.toml`, then copy **both files unchanged** to B, C, and D. Don't edit them separately on each laptop.
3. Start the doctor on **all four laptops at the same time**, from the project root:

   ```bash
   PYTHONPATH=src python -m ft_system.doctor --config configs/m4.active.distributed.toml
   ```

   Each laptop works out which machine it is from the config, listens on its real ports, and keeps connecting to every port its processes will use. Failures resolve on their own as the other laptops start their doctors. Wait until **all four laptops print `PASS`**, then press Ctrl-C on each one and start the demo.
4. On macOS, if a dialog asks whether Python may accept incoming connections, click **Allow**. The doctor triggers this dialog on purpose so it doesn't appear in the middle of the demo. Use the same `python` for the doctor and for the demo.
5. Keep the laptops awake and plugged in. On macOS, run `caffeinate -dis` in a spare terminal.

### Reading the doctor's output

| Doctor says | Meaning | Fix |
| --- | --- | --- |
| `none of the config's addresses ... belongs to this computer` | This laptop's IP changed or was typed wrong | Use the address it prints, fix the config on A, and copy it again to every laptop |
| `connection refused` | Nothing is listening on that laptop's port | Start the doctor there. If it's already running, that laptop's firewall is rejecting the port |
| `no answer within 4 s` | Wrong IP, different network, the Wi-Fi blocks laptop-to-laptop traffic, or a firewall silently drops the port | Check `ipconfig getifaddr en0` on that laptop. Try a phone hotspot if every cross-laptop check times out |
| `cannot listen on ...` | A process from an earlier run still holds the port | Run the printed `lsof` (macOS) or `netstat` (Windows) command and stop that process |
| `something other than the doctor answered` | Same as above, but on the other laptop | Stop the leftover process there |
| `MISMATCH` / `reached ..., but its config is X, this computer's is Y` | Different commit or config file | Each laptop's first `[INFO]` line shows `build` and `config`; the laptop that disagrees with the others is the stale one |
| `advertised_host is 127.0.0.1` / `listens on 127.0.0.1` | A single-laptop config was used for four laptops | Use the `*.distributed.toml` file, with `bind_host = "0.0.0.0"` |

The doctor only checks the network, code, and config. It doesn't start any demo processes.

## 2. During the demo

**The startup banner.** Every process prints `build <commit>, config <fingerprint> (<file>, <mode>)` as its first line. If any component registers with different values, the RM prints a red `MISMATCH` error. This catches a process relaunched with the passive config during the active demo, or a laptop that missed `git pull`.

**Press Enter in any window** to print a `STATUS` block for that process. Lines starting with `hint:` are its best guess at what's wrong. Press Enter in the **RM** window first, because it sees the whole system:

```text
----- STATUS RM -----
  mode active, view 8: members S1, S2, S3; ready S1, S3
  GFD connected; replicas registered: S1, S2, S3; clients registered: C1, C2, C3
  S2 waiting for state from S1: sent 3 time(s); next attempt in 1.4 s
  hint: look for 'checkpoint to S2' errors in S1's console; S2's checkpoint port may be blocked, ...
```

| What you see | Press Enter in | Likely cause and fix |
| --- | --- | --- |
| GFD never prints `registered LFD2` | GFD (`NOT registered: LFD2`), LFD2 (`GFD NOT connected`) | LFD2's laptop cannot reach A's GFD port. Re-run the doctor on both laptops |
| `GFD: N members` changes but `RM: N members` does not | GFD (`RM NOT connected`) | Start the RM before the GFD, and check the RM's port |
| A replica stays at `i_am_ready = 0` | RM | Read the hint. **Not registered with the RM**: that laptop cannot reach A's RM port. **Not a member**: its LFD is not getting heartbeat replies (is the LFD running with the same config?). **Waiting for state from S1, sent N times**: S1's console shows `checkpoint to S2 unavailable/failed`, so S2's checkpoint port is blocked |
| S2 repeats `checkpoint covers C1 only through request_num ...` | S2, then the clients | Normal once or twice: the donor had not yet applied a request S2 had already logged. If it keeps repeating, check each client's status for `not connected to S1` |
| Client prints `S2 unavailable` | That client (`not connected to S2`) | S2 is not running, or A cannot reach S2's client port |
| Active: duplicates stay at 1 per request after S2 is relaunched | Client (`RM NOT connected` or `not connected to S2`) | The client never received the view that re-added S2 |
| Passive: clients keep failing after the primary dies and never print `primary changed` | RM (`primary none` or an unready primary) and a client | The backups never received a checkpoint before the primary died; check the old primary's console for checkpoint errors |
| Passive: the new primary is S3, not S2 | (nothing to fix) | Expected: the earliest-joined ready backup wins, and a just-relaunched S2 is newer than S3 |
| `RM ERROR: MISMATCH: S2 runs build ...` | RM | Stop that process, `git pull` or re-copy the config on its laptop, then relaunch it |
| `replica state divergence` in a client | Each replica (compare `my_state`) | A real bug. Keep the logs (section 3) and report the time |
| A client that was closed and restarted never gets replies (it restarts at `request_num` 1, which the replicas have already seen) | That client (`next request_num 1`) | Restart the whole system. Keep each client running for the entire demo |
| A laptop's heartbeats suddenly time out | That laptop's LFD | The laptop slept or the Wi-Fi reconnected; its IP may have changed. Run the doctor again |

Interactive clients (`--interactive`) read commands from the terminal, so Enter doesn't print status there.

## 3. After a failed run

Every process also appends its console to `logs/console/<date>/<process>.log`; the banner shows the exact path. Set `FT_LOG_DIR` to use another folder, or `FT_LOG_DIR=off` to turn the copy off.

1. Copy each laptop's `logs/console/<date>/` folder (AirDrop, Slack, USB) into one folder per laptop, for example `run1/A`, `run1/B`, `run1/C`, `run1/D`. Process names don't collide across laptops.
2. Merge them into one timeline:

   ```bash
   python scripts/merge_logs.py run1 --since 14:05 --events          # what happened, without request/reply traffic
   python scripts/merge_logs.py run1 --since 14:05 --until 14:07      # everything in a two-minute window
   python scripts/merge_logs.py run1 --grep "S2|RM:"                  # one replica's story
   ```

   Routine heartbeats are hidden unless you add `--heartbeats`; failed heartbeats are always shown. Lines are ordered by each laptop's clock, which is usually accurate to a few milliseconds. Lines from different processes in the same millisecond keep file order, not real order.

The merged output is plain text, so it can be pasted straight into a bug report or a chat for help.
