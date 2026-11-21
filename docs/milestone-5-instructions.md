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

In each round, press `Control+C` in the target **server window**, or click its red close button and confirm **Terminate**. Keep the LFDs and clients running. While RM shows two members, **A** shows the TA a fresh reply in each of C1, C2 and C3. A calls the next round only when RM again shows **`ready: S1, S2, S3`**.

### Active demo

Start once all three clients have received replies. The TA can choose any replica for the first round. This example starts with S2 on C.

1. **C stops S2.** LFD2 detects the failure and GFD updates membership. A shows RM's `asking LFD2 to ensure replica S2 is running`. C shows the new S2 window that opens automatically and the new PID in LFD2's log.
2. **C stops S2 again in the new window from round one.** C shows the replacement logging requests, printing `installed checkpoint`, replaying the outstanding requests and resuming replies.
3. **D stops a different replica, S3.** D shows S3 restarting automatically and recovering its state. A confirms all three replicas are ready. Each client delivers one reply per request. The other replicas return the same value and the client logs `Discarded duplicate reply`.

### Passive demo

Shut down Active in the order **three clients → RM → LFDs on B, C and D → GFD**. Each LFD stops its server on exit. Repeat startup with `configs/m5.passive.distributed.toml` and `logs/m5-passive/`.

Start the designated primary's LFD first and wait for its replica to become ready before starting the others. Here B starts LFD1 first to make S1 primary. Before round one, A shows replies in all three client windows and B shows S1 sending checkpoints to S2 and S3.

1. **B stops primary S1.** A identifies the newly elected primary in RM and shows `primary changed` followed by replies in all three clients. The new primary keeps sending checkpoints. B shows `role = backup` and `installed checkpoint` in the new S1 window.
2. **B stops S1 again in its new window.** This time S1 is the recovered backup. The current primary keeps replying and sending checkpoints to the other backup. B shows S1 returning automatically as a backup again.
3. **A checks RM and names the other backup to stop.** If S2 is primary, D stops S3. If S3 is primary, C stops S2. That operator shows the replacement installing a checkpoint. A confirms all three replicas are ready, then the team shuts down in the order above.

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
