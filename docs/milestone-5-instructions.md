# M5 automatic recovery on four Macs

[中文版](milestone-5-instructions_zh.md) · [System design](m5-design.md) · [Verification](m5-verification.md)

## Files and setup

Connect all four Macs to the same LAN and use the `m5` branch with Python 3.11+. Run the commands from each Mac's repository root.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

On each Mac, run `networksetup -listallhardwareports` and find the `Device` for the demo Wi-Fi or Ethernet adapter. Use that device in place of `en0` to get its IPv4 address.

```bash
ipconfig getifaddr en0
```

B, C and D send their IPs to A. A sets `advertised_host` in `configs/m5.active.distributed.toml` and `configs/m5.passive.distributed.toml` using the table. **A pushes to `m5`, then B, C and D pull.**

| Mac | Processes | Sections that use this Mac's IP | Inbound TCP ports |
| --- | --- | --- | --- |
| A (sacred) | RM, GFD, C1–C3 | `[rm]`, `[gfd]` | 7300, 7200 |
| B | LFD1, S1 | `[server.S1]` | 5201, 6201, 8201 |
| C | LFD2, S2 | `[server.S2]` | 5202, 6202, 8202 |
| D | LFD3, S3 | `[server.S3]` | 5203, 6203, 8203 |

## Start the active demo

**A** starts RM first, then GFD, in separate terminals. Both should show `0 members`.

```bash
.venv/bin/python -m ft_system.rm.main --config configs/m5.active.distributed.toml
.venv/bin/python -m ft_system.gfd.main --config configs/m5.active.distributed.toml
```

**B, C and D** can each check connectivity to A. Replace the example IP with A's actual IP. Port 7300 is RM and 7200 is GFD. Both commands should report `succeeded` or `open`.

```bash
nc -vz 192.168.1.42 7300
nc -vz 192.168.1.42 7200
```

On B, C and D, start only the LFD. When RM requests a server, the LFD opens its local server window with live logs. If macOS asks for permission to control Terminal, choose **Allow**.

**B starts LFD1 first.**

```bash
# B
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD1 --replica-log logs/m5-active/S1.log
```

When **A** sees `S1 is ready` in the RM window, tell **C** to start LFD2.

```bash
# C
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD2 --replica-log logs/m5-active/S2.log
```

When **A** sees `S2 is ready`, tell **D** to start LFD3.

```bash
# D
.venv/bin/python -m ft_system.lfd.main \
  --config configs/m5.active.distributed.toml \
  --lfd-id LFD3 --replica-log logs/m5-active/S3.log
```

When **A** sees `S3 is ready` and confirms `ready: S1, S2, S3`, start C1, C2 and C3 in three separate terminals on A.

```bash
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C1 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C2 --interval 0.2
.venv/bin/python -m ft_system.client.main --config configs/m5.active.distributed.toml --client-id C3 --interval 0.2
```

## Three fault rounds

Run three rounds in Active, then three in Passive. Wait until all three clients receive replies before injecting a fault. Press `Control+C` in the target **server window**, or click its red close button and confirm **Terminate**. **Keep the LFDs and clients running.**

| Round | Active | Passive (S1 is the initial primary here) |
| --- | --- | --- |
| 1 | C stops S2. The TA can choose any first replica. | B stops primary S1. All three clients log `primary changed`, the new primary continues checkpointing, and S1 returns with `role = backup`. |
| 2 | C stops S2 again in its **new window**. | B stops the recovered S1 backup again. The current primary keeps replying and checkpointing. |
| 3 | D stops S3, a different replica. | Stop the other backup. If S2 is primary, D stops S3. If S3 is primary, C stops S2. |

**Show the TA these three events each round.**

1. The LFD detects a missed heartbeat and GFD updates membership. While A's RM shows two members, C1, C2 and C3 each receive new replies.
2. For S2, RM logs `asking LFD2 to ensure replica S2 is running`. A fresh S2 window opens automatically on C and LFD2 logs its new PID.
3. The replacement logs `installed checkpoint`. A waits for RM to show **`ready: S1, S2, S3`** again before starting the next round.

A recovering active replica logs requests, installs a checkpoint and replays outstanding requests before replying. Each client delivers one reply per request. Replies from different replicas should agree.

**When switching to Passive or ending the demo**, stop **the three clients → RM → the LFDs on B, C and D → GFD**. Each LFD stops its own server on exit.
To start Passive, use `configs/m5.passive.distributed.toml` and `logs/m5-passive/`. Start the designated primary's LFD first and wait for it to become ready before starting the other two. Show the primary sending checkpoints to both backups before round one.

### Passive consistency and latency

The default checkpoint period is one second. The primary sends a successful reply only after every current backup (at least one) installs and acknowledges a checkpoint containing the request. Waiting adds latency and preserves acknowledged updates across a primary crash.

All three clients keep looping and retry the same request number on failover. Show successful replies during each two-member interval and check that each client's `request_num N` yields `client_value = N` before and after the switch. [Measured results](m5-passive-commit-verification.md)

## Troubleshooting and local tests

| Symptom | What to check |
| --- | --- |
| `nc` reports `Connection refused` or `timed out` | For a refusal, check the corresponding RM or GFD on A. For a timeout, check A's IP, the LAN and the firewall. |
| LFD starts but no server window opens | Check that RM has received GFD membership and the LFD registration. In System Settings → Privacy & Security → Automation, allow the launching app to control Terminal. |
| Three members but fewer than three ready | Check the replacement's `installed checkpoint`, the sender's logs and checkpoint ports 8201–8203. |
| Passive heartbeats continue but replies wait | Check that the current backups installed the checkpoint and sent their ACKs. |

For a headless run, set `[recovery].mac_terminal = false`. On C, for example, `tail -F logs/m5-active/S2.log` shows S2's log and `kill -KILL "$(cat logs/m5-active/S2.pid)"` stops S2.

Run pytest and the smoke demo locally. Smoke logs go to `logs/m5-smoke/`.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/m5_smoke.py --demo-timing
```

On a Mac with a desktop session, run the real Terminal window test. To confirm the red close button by hand, use `M5_TEST_TERMINAL=confirm-close`.

```bash
M5_TEST_TERMINAL=1 .venv/bin/python -m pytest -q tests/integration/test_m5_mac_terminal.py
```
