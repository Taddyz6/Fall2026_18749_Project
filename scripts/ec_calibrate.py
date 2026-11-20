"""Measure what the injected CPU fault does at several intensities.

    python scripts/ec_calibrate.py                 # 5 minutes per level, about 35 minutes
    python scripts/ec_calibrate.py --seconds 20    # quick look

It starts the whole system on this computer, then for each level injects the
fault into one replica and reports that replica's CPU use, how long replies
from it take, and whether the fault detectors noticed anything. It ends by
killing the faulty replica to check that the relaunched one starts clean.

A level suits the diagnosis demo when the replica is plainly abnormal and
plainly slower, yet its LFD never misses a heartbeat: a fail-slow fault that the
fault-tolerance layer cannot see. Needs psutil.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psutil  # noqa: E402

from ft_system.blackbox import impact  # noqa: E402
from ft_system.blackbox.cluster import LocalCluster, component_processes  # noqa: E402
from ft_system.blackbox.faults import ctl  # noqa: E402

RAMP_SECONDS = 2       # after an inject, before measuring
SETTLE_SECONDS = 5     # after a clear; also lets late replies reach the client logs
AFTER_SECONDS = 10     # how long to watch for a return to normal
SLOWDOWN_FACTOR = 10   # "plainly slower": median reply time at least this many times normal
CPU_MARGIN = 10.0      # "plainly abnormal": this many points above anything seen fault-free


def say(text=""):
    print(text, flush=True)


def median(values):
    summary = impact.summarize(values)
    return summary["median"] if summary else float("nan")


def sample(cluster, seconds):
    """CPU percent and resident memory (MB) of every replica, once a second."""

    handles, cpu, rss = {}, {s: [] for s in cluster.servers}, {s: [] for s in cluster.servers}
    start, tick = time.time(), time.monotonic()
    for _ in range(seconds):
        tick += 1
        time.sleep(max(0.0, tick - time.monotonic()))
        for server_id in cluster.servers:
            pid = cluster.replica_pid(server_id)
            if pid is None:
                handles.pop(server_id, None)
                continue
            try:
                if server_id not in handles or handles[server_id].pid != pid:
                    handles[server_id] = psutil.Process(pid)
                    handles[server_id].cpu_percent(None)  # first call only sets the reference point
                    continue
                cpu[server_id].append(handles[server_id].cpu_percent(None))
                rss[server_id].append(handles[server_id].memory_info().rss / 2**20)
            except psutil.Error:
                handles.pop(server_id, None)
    return {"start": start, "end": time.time(), "cpu": cpu, "rss": rss}


def service_view(cluster, target, start, end):
    """Reply times per server and what the fault detectors logged, for one time window."""

    times, unanswered = {s: [] for s in cluster.servers}, {s: 0 for s in cluster.servers}
    client_errors = 0
    for client_id in cluster.config.clients:
        log = cluster.log_text(client_id)
        per_server, missing = impact.reply_times(log, start, end)
        for server_id in per_server:
            times[server_id] += per_server[server_id]
            unanswered[server_id] += missing[server_id]
        client_errors += impact.count_lines(log, r"ERROR", start, end)
    return {
        "reply_ms": {s: impact.summarize(times[s]) for s in cluster.servers},
        "unanswered": unanswered,
        "client_errors": client_errors,
        "heartbeat_failures": impact.count_lines(
            cluster.log_text(cluster.lfd_of[target]), rf"heartbeat to {target} failed", start, end),
        "membership_changes": impact.count_lines(cluster.log_text("GFD"), r"^GFD: \d+ members?", start, end),
        "view_changes": impact.count_lines(cluster.log_text("RM"), r"^RM: view \d+", start, end),
    }


def phase(cluster, target, label, seconds, fault=None):
    """One measured window, with ``fault`` (inject arguments) running in ``target`` if given."""

    if fault:
        ctl.inject(cluster.run_dir, target, **fault)
        time.sleep(RAMP_SECONDS)
    say(f"[{datetime.now():%H:%M:%S}] {label}: measuring {seconds} s")
    result = {"label": label, "fault": fault, **sample(cluster, seconds)}
    if fault:
        ctl.clear(cluster.run_dir, target)
    time.sleep(SETTLE_SECONDS)
    if fault:
        result["cpu_after_clear"] = median(sample(cluster, AFTER_SECONDS)["cpu"][target])
    result |= service_view(cluster, target, result["start"], result["end"])
    report(cluster, target, result)
    return result


def report(cluster, target, result):
    for server_id in cluster.servers:
        cpu, reply = impact.summarize(result["cpu"][server_id]), result["reply_ms"][server_id]
        cpu_text = (f"cpu% median {cpu['median']:5.1f}  p95 {cpu['p95']:5.1f}  max {cpu['max']:5.1f}"
                    if cpu else "cpu% no samples")
        reply_text = (f"replies {reply['n']:5d}  ms median {reply['median']:6.1f}  p95 {reply['p95']:5.0f}  "
                      f"max {reply['max']:5.0f}" if reply else "replies     0")
        mark = "*" if server_id == target and result["fault"] else " "
        say(f"   {mark}{server_id}  {cpu_text}   rss {median(result['rss'][server_id]):5.1f} MB   "
            f"{reply_text}   unanswered {result['unanswered'][server_id]}")
    after = f"   {target} cpu% after clear {result['cpu_after_clear']:.1f}" if result["fault"] else ""
    say(f"    {cluster.lfd_of[target]} heartbeat failures {result['heartbeat_failures']}   "
        f"GFD membership changes {result['membership_changes']}   "
        f"RM view changes {result['view_changes']}   client errors {result['client_errors']}{after}")


def verdict(baseline, result, target):
    """Why a level does not suit the demo; an empty list means it does."""

    normal_cpu = max(max(values) for values in baseline["cpu"].values() if values)
    normal_ms = max(1.0, baseline["reply_ms"][target]["median"])
    problems = []
    cpu = median(result["cpu"][target])
    if not cpu >= normal_cpu + CPU_MARGIN:
        problems.append(f"cpu {cpu:.0f}% is within {CPU_MARGIN:.0f} points of fault-free peaks ({normal_cpu:.0f}%)")
    reply = result["reply_ms"][target]
    if reply is None or reply["median"] < SLOWDOWN_FACTOR * normal_ms:
        problems.append(f"median reply {reply['median'] if reply else 0:.0f} ms is under "
                        f"{SLOWDOWN_FACTOR}x normal ({normal_ms:.0f} ms)")
    if result["heartbeat_failures"] or result["membership_changes"] or result["view_changes"]:
        problems.append(f"the fault-tolerance layer reacted ({result['heartbeat_failures']} heartbeat "
                        f"failures, {result['membership_changes']} membership changes)")
    if not result["cpu_after_clear"] < normal_cpu + CPU_MARGIN:
        problems.append(f"cpu was {result['cpu_after_clear']:.0f}% after the fault was cleared")
    return problems


def relaunch_check(cluster, target, duty, threads):
    """A replica killed while faulty must come back clean: the fault belongs to the process."""

    ctl.inject(cluster.run_dir, target, duty=duty, threads=threads)
    killed_at, old = time.time(), cluster.kill_replica(target)
    while target in cluster.view()[0]:
        time.sleep(0.05)
    removed = time.time() - killed_at
    cluster.wait_ready(cluster.servers)
    recovered = time.time() - killed_at
    time.sleep(SETTLE_SECONDS)
    state = ctl.replicas(cluster.run_dir)[target]
    cpu = median(sample(cluster, AFTER_SECONDS)["cpu"][target])
    clean = state["pid"] != old and state["applied"] is None and cpu < CPU_MARGIN
    say(f"relaunch: killed {target} pid {old} while it ran the fault at duty {duty:g}; out of the RM "
        f"view after {removed:.1f} s; all replicas ready after {recovered:.1f} s; new pid "
        f"{state['pid']}, {ctl.describe(state['applied'])}, cpu {cpu:.1f}%  ->  {'PASS' if clean else 'FAIL'}")
    return {"duty": duty, "old_pid": old, "new_pid": state["pid"], "removed_s": removed,
            "recovered_s": recovered, "applied": state["applied"], "cpu": cpu, "clean": clean}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=int, default=300, help="Measured seconds per level (default 300)")
    parser.add_argument("--levels", type=float, nargs="+", default=[0.25, 0.5, 0.75, 0.9, 1.0],
                        help="Fractions of each period the injected task is busy")
    parser.add_argument("--threads", type=int, default=1, help="Busy threads in the injected task")
    parser.add_argument("--target", default="S2", help="Replica that receives the fault")
    parser.add_argument("--interval", type=float, default=0.2, help="Seconds between each client's requests")
    parser.add_argument("--config", default="configs/m5.active.local.toml")
    parser.add_argument("--run-dir", type=Path, help="Default: logs/ec/calibrate-<time>")
    args = parser.parse_args()
    run_dir = args.run_dir or ROOT / "logs" / "ec" / f"calibrate-{datetime.now():%Y%m%d-%H%M%S}"

    say(f"run directory: {run_dir}")
    with LocalCluster(run_dir, args.config, args.interval) as cluster:
        time.sleep(SETTLE_SECONDS)
        baseline = phase(cluster, args.target, "no fault", args.seconds)
        levels = []
        for duty in args.levels:
            fault = {"duty": duty, "threads": args.threads}
            levels.append(phase(cluster, args.target, f"duty {duty:g}", args.seconds, fault))
        say()
        usable = []
        for level in levels:
            problems = verdict(baseline, level, args.target)
            say(f"{level['label']:10s} {'usable' if not problems else 'not usable: ' + '; '.join(problems)}")
            if not problems:
                usable.append(level["fault"]["duty"])
        relaunch = relaunch_check(cluster, args.target, usable[-1] if usable else args.levels[-1], args.threads)

    summary = {"config": str(args.config), "target": args.target, "seconds": args.seconds,
               "client_interval": args.interval, "baseline": baseline, "levels": levels,
               "usable": usable, "relaunch": relaunch}
    (run_dir / "calibration.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    say(f"results: {run_dir / 'calibration.json'}")
    say(f"processes left running: {[process.pid for process in component_processes()] or 'none'}")
    return 0 if usable and relaunch["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
