# Extra credit: black-box failure diagnosis

Individual extra credit, Option #1 in the project overview. All of it lives in
`src/ft_system/blackbox/`, `scripts/ec_*.py` and `tests/blackbox/`. No Milestone 5
file is changed, and nothing in M5 imports this code.

| Part | State |
| --- | --- |
| 1. Fault injector | Built, tested and calibrated on Windows |
| 2. Metric collection (agent and collector) | Built and tested on Windows |
| 3. Normal signature | Built and tested on Windows |
| 4. Detection and diagnosis | Built and tested on Windows |
| 5. Live dashboard | Built and tested on Windows |
| 6. One-command launcher and unattended demo | Built and tested on Windows |
| 7. Automatic recovery, client reply times, CPU by thread | Built and tested on Windows |
| Evaluation over many trials | Not done yet |

## Quick start

From the repository root, with `psutil` installed:

```bash
python scripts/ec_run.py train        # once per computer: records 5 fault-free minutes and learns normal
python scripts/ec_run.py up           # starts everything; open http://127.0.0.1:8750/
python scripts/ec_run.py autopilot    # or: runs the whole demo unattended and checks each step
```

Section 6 describes these. The other sections explain each part and can be
run on their own; their commands assume the repository root as the working
directory and that `ft_system` is importable (an editable install, or
`PYTHONPATH=src`). The scripts under `scripts/` find the package themselves.

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

## 2. Metric collection

Two programs, with one JSON object per line sent between them over TCP:

- The **agent** ([`agent.py`](../src/ft_system/blackbox/agent.py)) runs on each
  computer that has components. Once a second it reports operating-system
  metrics for every component process there and for the computer itself. It
  needs `psutil`.
- The **collector** ([`collector.py`](../src/ft_system/blackbox/collector.py))
  runs on one computer. It stamps each record with its arrival time, appends
  samples to `samples.jsonl` and events to `events.jsonl` in the run directory,
  and keeps the last ten minutes in memory for the detector and the dashboard.
  It uses the standard library only.

```bash
python -m ft_system.blackbox.collector --run-dir logs/ec/demo
python -m ft_system.blackbox.agent --collector 127.0.0.1:8749
python -m ft_system.blackbox.collector --run-dir logs/ec/demo --summary   # what a run holds
```

For agents on other computers, start the collector with `--bind 0.0.0.0` and
give each agent the collector's address.

### How the agent stays black-box

It finds components by command line only: `-m ft_system.server.main --server-id S2`
is the server S2 ([`discover.py`](../src/ft_system/blackbox/discover.py)). It does
not read PID files, logs or the fault injector's files, and the components do
nothing to help it.

A component keeps its label across restarts. When the process behind a label
changes, the agent reports `process_exited` and `process_started` events and
raises the label's incarnation number.

Two details that matter on real systems:

- **One component can be two processes.** In a Windows virtual environment the
  launcher and the interpreter have the same command line, and the agent adds
  them together.
- **PIDs are reused.** The agent examines only PIDs it has not seen before, and
  rechecks all of them every 30 s, or every second while a component is missing.

### Metrics

Per component, every second:

| Metric | Meaning |
| --- | --- |
| `cpu`, `cpu_user`, `cpu_sys` | Percent of one core |
| `rss_mb` | Resident memory |
| `io_*_count_s`, `io_*_bytes_s` | I/O operations and bytes per second. On Windows the kinds are `read`, `write` and `other`. macOS has none |
| `threads` | Thread count (servers only) |
| `ctx_voluntary_s`, `ctx_involuntary_s` | Context switches per second (servers only) |

Thread count and context switches are collected for servers only, and read
every other second, with the last value repeated in between. On Windows they
cost a system-wide query of about 4 ms per process. Read every second for three
servers, that one query was two thirds of the agent's CPU.

Per computer: total and busiest-core CPU, available memory, swap, free disk
space, disk and network bytes per second, and the agent's own CPU and memory.

### Recording a run

[`scripts/ec_experiment.py`](../scripts/ec_experiment.py) starts the collector, the
whole system and the agent on one computer, performs the scheduled actions,
stops everything and prints a summary:

```bash
python scripts/ec_experiment.py --seconds 180 --do 60:inject:S2:0.75 --do 100:clear:S2 --do 120:kill:S2
python scripts/ec_experiment.py --seconds 180 --do 60:load:6 --do 120:load:3
```

It uses [`configs/ec.active.local.toml`](../configs/ec.active.local.toml), which is
the M5 local config plus three spare clients. Clients C1 to C3 run from the
start, as in the M5 demo. `load:6` starts C4 to C6, which doubles the request
rate, and `load:3` stops them again. A stopped client cannot be restarted in
the same run: it would number its requests from 1 again and the servers would
take them for duplicates.

### Measured

From a three-minute run with the fault at 60 s and the kill at 120 s (Nov 20,
Windows):

- **Coverage:** all 11 components and the host have one sample a second: 178 or
  179 samples in 178 s, largest gap 1.03 s. S2 has one 7.0 s gap, across its
  kill and relaunch.
- **Relaunch:** S2 was reported as exited 0.2 s after the kill, and as started
  with incarnation 2 a further 5.0 s later.
- **Cost:** the agent used between 1.0% and 1.4% of one core across five runs,
  and 37 MB of memory. Before thread count and context switches were moved to
  every other second it used about 2%.
- **What the fault looks like in S2** (medians, before and during the 0.75 fault):

| Metric | Normal | During the fault |
| --- | --- | --- |
| `cpu` | 0% | 75% |
| `ctx_voluntary_s` | 22 | 334 |
| `io_write_count_s` | 124 | 124 |
| `rss_mb` | 26.9 | 26.9 |

`io_write_count_s` follows the request load: the server makes two write
operations per log line and logs about four lines per request. It did not
move, so the extra CPU is not extra work from clients.

**Thread count is not a usable signal on Windows.** The fault does add exactly
one thread. But the count also changes when nothing is wrong: every server has
6 threads at start and 4 after its first minute, and several times an extra
thread appeared in all servers in the same second. It is recorded and shown,
and its normal band is wide enough that it never triggers a diagnosis.

## 3. Normal signature

The signature is what each component's metrics look like when nothing is wrong:
for every component and metric, the mean, standard deviation and percentiles
from a recorded fault-free run ([`baseline.py`](../src/ft_system/blackbox/baseline.py)).

```bash
python scripts/ec_experiment.py --seconds 300 --run-dir logs/ec/train      # record, fault-free
python -m ft_system.blackbox.baseline train --run-dir logs/ec/train         # writes logs/ec/baseline.json
python -m ft_system.blackbox.baseline show
python -m ft_system.blackbox.baseline check --run-dir logs/ec/another-run   # how much is outside
python -m ft_system.blackbox.report --run-dir logs/ec/another-run           # figure; needs matplotlib
```

### Bands

A value is judged by how many standard deviations it is from the mean: within 3
is normal, beyond 6 is anomalous. These are the thresholds of the
fingerpointing paper (Pertet, Gandhi and Narasimhan, 2007). The figures draw
them as two shaded bands and the detector tests against the same numbers.

Two adjustments were needed for this system:

- **Process age.** A young process does not look like a mature one, so the
  signature has two phases. A process younger than 90 s is judged against
  warm-up statistics pooled over all servers. An older one is judged against
  its own. Without this, every relaunched replica would look abnormal.
- **Floors.** An idle server's CPU sits at 0% and its memory is flat, so the
  measured deviation is close to zero and any blip would count as abnormal.
  Each kind of metric has a smallest allowed deviation: 2 percentage points for
  CPU, 0.5 for thread count, 10% of the mean for rates, 0.8% of the mean for
  memory.

Training refuses a run in which a fault was injected. A signature holds only
for the computer and workload it was trained on: `check` and the diagnosis warn
when the host, OS, Python version, core count, config, replication mode or
client rate differs.

### Measured

Trained from a five-minute fault-free run (Nov 20, Windows, three clients at 5
requests/s each). For S2 once it is past warm-up, 217 samples:

| Metric | Mean | Standard deviation | Normal band (6 deviations) |
| --- | --- | --- | --- |
| `cpu` | 0.45% | 1.02 | 0 to 12.5% |
| `ctx_voluntary_s` | 25.6 | 3.98 | 1.7 to 49.4 |
| `io_write_count_s` | 121.4 | 4.75 | 48.6 to 194.2 |
| `rss_mb` | 26.81 | 0.03 | 25.5 to 28.1 |
| `threads` | 4.03 | 0.18 | 1.0 to 7.0 |

S1 and S3 are close to these: their CPU means are 0.52% and 0.45%, and their
context switches 26.6 and 25.4 a second. For the warm-up phase the thread band
is 0 to 11.0, which covers the 6 threads of a young process.

Checked against runs that were not used for training:

| Run | Values judged | Beyond 6 deviations | Sustained (3 of 7 samples) |
| --- | --- | --- | --- |
| Fault-free, five minutes | 11,661 | 0 | 0 |
| S2 killed and relaunched | 5,746 | 3, all in the relaunched S2's first seconds | 0 |
| Fault in S2 | | S2: 40 of its CPU samples and 40 of its context-switch samples | S2 only |

## 4. Detection and diagnosis

Three stages, following the fingerpointing paper:

1. **Is a replica's metric outside its own normal range?**
   ([`detect.py`](../src/ft_system/blackbox/detect.py)) An anomaly is logged when
   3 of the last 7 samples are beyond 6 deviations. The metric is *problematic*
   when an anomaly was logged in more than half of the last 15 samples. The
   mean is a running one, so it follows slow drift, but it stops following
   while samples are anomalous, so a fault that persists is never learned as
   normal.
2. **Which replica is the odd one out?**
   ([`fingerpoint.py`](../src/ft_system/blackbox/fingerpoint.py)) A replica is
   blamed for a metric only if it is out of range on it *and* is 6 deviations
   away from a peer that is not. If several replicas are odd, the one that is
   odd on the most metrics is named, and on a tie the one named before.
3. **What kind of problem?** ([`classify.py`](../src/ft_system/blackbox/classify.py))
   CPU up with the write rate unchanged is a CPU-intensive task inside the
   process. CPU and the write rate up together, on one replica only, is extra
   request work. Memory above its range without CPU is memory growth.

When the replicas have moved together, nobody is blamed. The diagnosis says
"a change in workload or on the computer", because from OS metrics alone those
two cannot be told apart.

The windows and thresholds are the paper's. Three things were added because
the recorded runs needed them:

- **Compare values, not only flags.** When the client load doubled, write bytes
  per second landed right at the threshold on all three servers, with about
  30 of 48 samples beyond it on each. With the faster windows one replica was
  flagged while the others' running means had adapted, and it was blamed. The
  three servers' values were within a tenth of a deviation of each other, so
  the rule became distance from a peer.
- **An excursion ends when the values are normal again.** With only the
  15-sample memory, a diagnosis was withdrawn about 18 s after the fault ended
  instead of about 10 s.
- **Fading evidence is not a new problem.** As a fault ends its metrics return
  to normal one at a time. A new cause is reported for the same replica only
  when a metric it was not odd on before becomes odd.

### Running it

```bash
python -m ft_system.blackbox.diagnose --run-dir logs/ec/some-run              # replay a recording
python -m ft_system.blackbox.diagnose --run-dir logs/ec/some-run --profile fast
python -m ft_system.blackbox.collector --run-dir logs/ec/demo --baseline logs/ec/baseline.json   # live
python scripts/ec_experiment.py --seconds 180 --do 60:inject:S2:0.75 --baseline logs/ec/baseline.json
```

Replaying a recorded run gives the same verdicts as diagnosing it live, which
makes tuning repeatable. `--profile fast` uses windows of 5 and 7 samples
instead of 7 and 15. Live, each change of verdict is printed by the collector
and stored as a `diagnosis` event:

```text
DIAG: S2: CPU-intensive task inside the process. CPU rose but the write rate, which follows
request work, did not: CPU 73.4% (normal 0 to 12.2%); write rate 124/s (normal 49.1 to 195/s);
S1 and S3 normal
```

`report` marks the diagnoses on its figures beside the things that were done
to the system.

### Measured

Recorded runs replayed against the signature above (Nov 20, Windows):

| Run | Paper windows (7 and 15) | Fast windows (5 and 7) |
| --- | --- | --- |
| 0.75 fault in S2 for 40 s | S2 named after 9.9 s as a CPU-intensive task; withdrawn 9.9 s after the fault was cleared | Named after 5.9 s; withdrawn 8.9 s after |
| Clients doubled for 60 s | "Change in workload" after 10.0 s; no replica blamed | After 6.0 s; no replica blamed |
| Fault-free, five minutes | Nothing diagnosed | Nothing diagnosed |
| S2 killed and relaunched | Nothing diagnosed | Nothing diagnosed |

The fault times match what the windows predict: the third anomalous sample logs
the first anomaly, and the eighth logged sample (the fourth, with the fast
windows) makes the metric problematic.

In the doubled-load run every server's write rate went from 124 to 244
operations a second while median CPU stayed at 0%.

### Limits

- Only active replication on one Windows computer has been measured.
- One fault kind, at one intensity, in one replica. The plan's evaluation
  covers more trials and intensities.
- If every replica has the same fault at once, it reads as a change in
  workload.
- A fault that grows slowly enough is followed by the running mean and never
  flagged.

## 5. Live dashboard

One web page, served by the collector, that shows the diagnosis as it happens
([`dashboard.py`](../src/ft_system/blackbox/dashboard.py) and
[`dashboard.html`](../src/ft_system/blackbox/dashboard.html)). The page is a
single self-contained file, so it works with no internet connection.

```bash
# live, beside a running system
python -m ft_system.blackbox.collector --run-dir logs/ec/demo --baseline logs/ec/baseline.json --dashboard
python scripts/ec_experiment.py --seconds 110 --do 35:inject:S2:0.75 --do 80:clear:S2 \
    --baseline logs/ec/baseline.json --dashboard

# play a recording back, 5 times faster than life
python -m ft_system.blackbox.dashboard --replay logs/ec/fault-ec --speed 5
```

Then open <http://127.0.0.1:8750/>. Add `?theme=dark` or `?theme=light` to force
a theme; otherwise the page follows the system setting.

### What is on the page

- **Status banner.** Normal, the current diagnosis with its evidence and a
  suggested action, or a workload change with nobody blamed.
- **Four charts:** CPU, context switches, write operations and memory, one line
  per replica over the shaded normal range. A fault shows as one line leaving
  the other two. Pointing at a chart shows every replica's value at that
  second; the arrow keys do the same from the keyboard.
- **Anomaly matrix.** One row per metric of each replica, one column per second,
  coloured by how far the value is from normal. A fault shows as one replica's
  rows lighting up while the others stay plain.
- **Now.** The latest value of every metric for each replica beside its normal
  range, marked when it is outside.
- **What the fault-tolerance layer reports.** The RM's current membership and
  the LFDs' heartbeat timeouts in the last minute, read from their logs when
  those are in the run directory. This is there for contrast and the diagnosis
  never reads it. During the fault it says three members, all ready, no
  timeouts, while the banner names S2.
- **Events.** Everything done to the system and every change of diagnosis,
  numbered to match the rules drawn on the charts.

When the page runs beside a live system on the same computer (section 6) it
also shows:

- **Client reply time.** The median time each replica takes to answer, over the
  last 5 s, read from the client logs. This is what the fault does to the
  service, and the diagnosis does not use it. During the 0.75 fault S2's
  replies take tens of milliseconds while S1's and S3's take about one.
- **CPU by thread.** While a replica is diagnosed, the busiest threads of its
  process in percent of one core. For the injected fault one thread shows about
  72% and the rest 0%: a task inside the process, not request handling.

The server sends one frame a second over server-sent events and keeps the last
15 minutes for a page that opens late. It is bound to this computer only
unless the collector is started with `--bind 0.0.0.0`.

### Measured

In a live run on Nov 20 (Windows) with a 0.75 fault injected into S2: the
banner changed to "S2: CPU-intensive task inside the process" 10 s after the
injection and back to "Normal" 10 s after the fault was cleared. Throughout,
the fault-tolerance panel showed three members, three ready and no heartbeat
timeouts.

## 6. Running the demo

[`scripts/ec_run.py`](../scripts/ec_run.py) runs everything on one computer
([`session.py`](../src/ft_system/blackbox/session.py) holds the logic).

```bash
python scripts/ec_run.py train                 # record a fault-free run and train the signature
python scripts/ec_run.py up                    # start everything and keep it running
python scripts/ec_run.py autopilot             # start, run the scripted demo with checks, stop
python scripts/ec_run.py autopilot --rounds 3  # the same, three times
```

`up` starts the RM, the GFD, the three LFDs with their servers and three
clients, then the agent, and runs the collector, the diagnosis and the
dashboard in its own process. Train first: `up` refuses to start without a
signature. `--profile fast` after `up` or `autopilot` selects the faster
detection windows.

### Doing things to the running system

Use the buttons on the page, or the same actions from another terminal:

```bash
python scripts/ec_run.py inject S2             # start the CPU fault in S2 at 0.75
python scripts/ec_run.py inject S2 --duty 0.5
python scripts/ec_run.py clear S2
python scripts/ec_run.py load 6                # run 6 clients instead of 3
python scripts/ec_run.py load 3
python scripts/ec_run.py rejuvenate            # kill the diagnosed replica; the RM relaunches it
python scripts/ec_run.py rejuvenate S1         # or a named one
python scripts/ec_run.py auto on               # let the session restart a diagnosed replica itself
python scripts/ec_run.py auto off
python scripts/ec_run.py down                  # stop everything
```

Each action is recorded in `events.jsonl`, whichever way it was started.
`up --auto-recover` starts with automatic recovery on.

- **Restarting a replica is refused unless every replica is ready.** Recovery
  must never take the group below two working replicas.
- **The load can be doubled three times per session.** Doubling starts spare
  clients, and a spare cannot be reused (section 2). After that, restart the
  session.
- **`down` always cleans up.** If no session answers, it kills every process of
  the system and the monitor that it finds on this computer, including ones you
  started by hand.

### Automatic recovery

Restarting the diagnosed replica reuses the system's own crash handling: the
LFD reports the dead process and the RM relaunches it. That turns a fail-slow
fault, which M5 cannot repair, into a crash fault, which it can.

With automatic recovery on, the session restarts the culprit by itself
([`recover.py`](../src/ft_system/blackbox/recover.py)), but only when all of
these hold:

- the diagnosis is one a restart cures (a CPU-intensive task or memory growth);
- it has stood for 5 s;
- every replica is ready;
- no replica was restarted in the last 60 s, by anyone.

While it is holding back, the banner says why.

### The unattended demo

`autopilot` runs the demo script and stops with an error if a step does not go
as expected:

1. Wait until every replica is reporting and inside its normal range.
2. Double the client load. Expect a workload change with no replica blamed.
   Return to the normal load and expect "Normal" again.
3. Inject the fault into S2. Expect S2 to be diagnosed with a CPU-intensive
   task, while the RM still reports three ready replicas and the LFDs report
   no heartbeat timeout.
4. Restart S2. Expect it to be relaunched as a new process without the fault,
   ready, and inside its normal range.
5. Wait 15 s and expect it to still be normal.
6. Turn automatic recovery on and inject the fault again. Expect the session to
   restart S2 without being asked, once the minute since step 4 has passed.

### Measured

Three rounds in a row on Nov 20 (Windows), all passed. Each took about 70 s, or 86 s with starting and stopping:

| Step | Round 1 | Round 2 | Round 3 |
| --- | --- | --- | --- |
| Load change reported, nobody blamed | 10.0 s | 9.9 s | 9.9 s |
| Fault in S2 diagnosed | 9.9 s | 9.8 s | 9.8 s |
| S2 relaunched, ready and normal after being killed | 8.0 s | 7.8 s | 7.0 s |

With step 6 added, one more round passed: the second fault was diagnosed after
10 s, the policy held back because S2 had been restarted 33 s earlier, and S2
was restarted automatically and normal again 45 s after the injection.

After each round no process of the system or the monitor was left running.
After a session's launcher was killed without cleanup, `down` removed the 12
processes it left behind.

## Tests

```bash
python -m pytest tests/blackbox -q
```

159 tests. They cover the fault injector (in a stand-in process that takes
`--server-id`), discovery, the agent (against stand-in processes with component
command lines), the collector, the signature, each detection and blame rule on
synthetic series, the diagnosis on synthetic recorded runs, the figures, and
the dashboard's frames and HTTP routes, the recovery rules, and the session's
actions and client load changes with stand-in processes. The agent and session tests are skipped
where `psutil` is not installed, and the figure tests where `matplotlib` is
not. The page itself was checked by eye in a headless browser, in both themes,
with the pointer over a chart and a matrix cell, and by pressing a button.
