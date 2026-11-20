# Extra credit: black-box failure diagnosis

Individual extra credit, Option #1 in the project overview. All of it lives in
`src/ft_system/blackbox/`, `scripts/ec_*.py` and `tests/blackbox/`. No Milestone 5
file is changed, and nothing in M5 imports this code.

| Part | State |
| --- | --- |
| Fault injector | Built, tested and calibrated on Windows |
| Metric collection, normal signature, detection, dashboard | Not built yet |

The commands below assume the repository root as the working directory and that
`ft_system` is importable (an editable install, or `PYTHONPATH=src`).

## 1. Fault injector

### The fault

A CPU-intensive task inside one server replica: one or more extra threads in the
server process that are busy for a chosen fraction (the *duty*) of every 100 ms.

Python runs one thread of a process at a time, so the busy thread and the
server's event loop take turns. The replica still answers heartbeats and
requests, only late. That is a fail-slow fault: the replica is alive by every
test the LFD, GFD and RM apply, and it is still the slowest member of the group.

### How it gets into an unmodified server

Python imports a module named `sitecustomize` at start-up if one is on
`PYTHONPATH`. The LFD passes its own environment to the server it launches. So an
LFD started with `faults/shim/` on `PYTHONPATH` and `FT_FAULT_DIR` set loads
[`sitecustomize.py`](../src/ft_system/blackbox/faults/shim/sitecustomize.py) into
every replica it launches or relaunches.

The shim acts only in a process whose command line has `--server-id`, which only
the server takes. In the RM, GFD, LFDs and clients it returns at once. In a
server it starts one background thread that:

1. writes `<server-id>.shim.json` to the fault directory, with the process's PID
   and a random token;
2. checks `<server-id>.json` four times a second, and starts or stops the busy
   threads when that file addresses a fault to its token;
3. rewrites `<server-id>.shim.json` to confirm what it is now running.

It prints nothing, so the server's log is unchanged.

### Commands

Start each LFD with the shim environment. Everything else starts as in the M5
instructions.

```powershell
# PowerShell
python -m ft_system.blackbox.faults.ctl env | Invoke-Expression
python -m ft_system.lfd.main --config configs/m5.active.local.toml --lfd-id LFD2
```

```bash
# macOS, Linux, Git Bash
eval "$(python -m ft_system.blackbox.faults.ctl env --shell bash)"
python -m ft_system.lfd.main --config configs/m5.active.local.toml --lfd-id LFD2
```

Then, from any terminal on the same computer:

```bash
python -m ft_system.blackbox.faults.ctl status
python -m ft_system.blackbox.faults.ctl inject S2 --duty 0.75
python -m ft_system.blackbox.faults.ctl clear S2      # no ID: clear every replica
```

The fault directory defaults to `logs/ec/faults` under the working directory.
`--fault-dir` or `FT_FAULT_DIR` overrides it; the LFD and `ctl` must agree.

### What the injector guarantees

- **Confirmed.** `inject` and `clear` succeed only after the replica reports the
  change. If it does not within 3 s, the fault is withdrawn and the command exits
  with an error.
- **Recorded.** Each confirmed change is appended to `events.jsonl` in the fault
  directory, with the time the replica applied it. That file is the ground truth
  for scoring the diagnosis later.
- **Tied to one process.** A fault is addressed to a process by its token. A
  relaunched replica has a new token and starts clean, as a restarted process
  with a runaway thread would.
- **No deletes.** Control files are replaced in one step and never deleted,
  because Windows refuses to delete a file another process is reading.
- **Standard library only.** The shim and `ctl` need nothing but the checkout.

### Calibration

[`scripts/ec_calibrate.py`](../scripts/ec_calibrate.py) starts the whole system on
one computer (three clients at 5 requests/s each), injects the fault into S2 at
each level, and reports S2's CPU use, client-observed reply times, and whether
the fault detectors noticed. It ends by killing S2 while faulty to check that
the relaunched process is clean. It needs `psutil`.

```bash
python scripts/ec_calibrate.py                 # 5 minutes per level, about 35 minutes
python scripts/ec_calibrate.py --seconds 20    # quick look
```

Reply times are read from the client logs. They are used to choose an intensity
and to show the fault's effect; the detector will not use them.

Measured on Nov 19 (Windows 11, Python 3.12, 20 logical cores, active
replication, one busy thread, five minutes per level; raw data in
`logs/ec/calibrate-phase1/`, which is not committed):

| Duty | S2 CPU, median | S2 reply time, median / 95th pct / max | LFD2 heartbeats answered by S2 | S2 in membership |
| --- | --- | --- | --- | --- |
| no fault | 0.0% | 1 / 2 / 8 ms | 298 of 298 | yes |
| 0.25 | 25.0% | 1 / 21 / 27 ms | 297 of 297 | yes |
| 0.5 | 48.4% | 7 / 45 / 53 ms | 296 of 296 | yes |
| **0.75** | **71.9%** | **29 / 70 / 77 ms** | **294 of 294** | **yes** |
| 0.9 | 82.8% | 41 / 84 / 92 ms | 288 of 288 | yes |
| 1.0 | 98.4% | 64 / 126 / 142 **seconds** | 0 of 115 | no |

- S1 and S3 stayed at 0.0% median CPU and 0 to 1 ms median replies at every level.
- Memory did not move: every replica stayed between 25.8 and 27.5 MB.
- Every request was answered at every level, and S2's CPU returned to between 0%
  and 1.5% after each fault was cleared.
- **0.75 is the demo setting.** S2 is far outside its normal CPU range and its
  replies take a median 29 ms instead of 1 ms, yet the LFD, GFD and RM see a
  healthy replica. 0.9 also works; 0.75 keeps more distance from 1.0.
- **1.0 is a different failure.** S2 could no longer keep up with 15 requests/s,
  so its backlog grew for as long as the fault lasted: requests sent in the
  first 30 s were answered a median 16 s late, and requests sent halfway through
  more than 100 s late. It missed a heartbeat within 3 s of the fault starting
  and was removed from the membership, but it was never repaired: the RM asked
  LFD2 to relaunch it 297 times and LFD2 answered "owned child is still running"
  each time, because the process was alive. The RM's view held two replicas
  until the fault was cleared, and S2 then caught up in under a second. In a 15 s trial at the same
  level no heartbeat was missed, so how soon this happens varies.
- **Relaunch check passed.** S2 was killed while running the fault at 0.9. It
  left the RM's view after 1.5 s, all three replicas were ready again after
  7.0 s, and the new process ran no fault at 0.0% CPU.

Rerun this on the computer used for the demo. The slowdown depends on how the
operating system schedules the two threads, and only Windows has been measured.

### Virtual environments on Windows

In a Windows virtual environment, `python.exe` is a small launcher that starts
the real interpreter as a child process. The PID the LFD writes to `S2.pid` is
then the launcher's, which uses no CPU. The shim runs in the real interpreter
and registers that PID, so `ctl` and the calibration script measure the right
process. Checked with a throwaway environment: the registered PID showed 48% CPU
under a 0.5 fault while the launcher showed 0%.

### Tests

```bash
python -m pytest tests/blackbox -q
```

The tests run the shim inside a stand-in process that takes `--server-id`, and
check confirmation, the CPU actually burned, the event record, the error paths,
and that a relaunched process ignores its predecessor's fault.
