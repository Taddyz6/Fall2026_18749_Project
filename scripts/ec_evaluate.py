"""Run the evaluation experiments of the black-box diagnosis, and report on them.

    python scripts/ec_evaluate.py run detect quiet loop cost    # experiments, in the order given
    python scripts/ec_evaluate.py report                        # tables and figures from what was recorded

detect  24 faults: every replica at four intensities, twice, 35 s each          (31 minutes)
quiet   30 minutes with no fault: three kills and two doublings of the load      (31 minutes)
loop    10 faults with automatic recovery on                                      (15 minutes)
cost    the service measured bare, with the fault shim, and with the monitor      (11 minutes)

Each experiment records into its own folder under logs/ec/eval/. "report" replays
the recordings with both window settings and writes results.md and the figures to
logs/ec/eval/report/. Recording needs psutil; the report needs matplotlib.
"""

import argparse
import asyncio
import json
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ft_system.blackbox import baseline, impact  # noqa: E402
from ft_system.blackbox.collector import DEFAULT_PORT, read_run  # noqa: E402

SERVERS = ("S1", "S2", "S3")
DUTIES = (0.25, 0.5, 0.75, 1.0)
FIRST, PERIOD, FAULT_SECONDS = 20, 75, 35   # one fault every 75 s: 35 s of fault, 40 s to settle
LOOP_TRIALS, LOOP_PERIOD = 10, 85           # restarts must be at least 60 s apart
COST_SECONDS = 180
CALIBRATION = ROOT / "logs" / "ec" / "calibrate-phase1"


def say(text=""):
    print(text, flush=True)


def fresh(args, name):
    run_dir = args.out / name
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(f"{run_dir} already holds a recording; move it away first")
    return run_dir


def record(args, name, seconds, actions):
    """Record one run with ``ec_experiment.py``; ``actions`` are its --do values."""

    run_dir = fresh(args, name)
    command = [sys.executable, str(ROOT / "scripts" / "ec_experiment.py"), "--seconds", str(seconds),
               "--run-dir", str(run_dir), "--baseline", str(args.baseline)]
    for action in actions:
        command += ["--do", action]
    say(f"=== {name}: {seconds} s, {len(actions)} actions, into {run_dir}")
    if subprocess.run(command, cwd=ROOT).returncode:
        raise SystemExit(f"{name} failed")


def detect(args):
    """Every replica at every intensity, twice, in an order fixed by a seed."""

    trials = [(server, duty) for duty in DUTIES for server in SERVERS] * 2
    random.Random(18749).shuffle(trials)
    actions = []
    for index, (server, duty) in enumerate(trials):
        start = FIRST + index * PERIOD
        actions += [f"{start}:inject:{server}:{duty}", f"{start + FAULT_SECONDS}:clear:{server}"]
    record(args, "detect", FIRST + len(trials) * PERIOD + 10, actions)


def quiet(args):
    """Half an hour in which nothing is wrong, though plenty happens."""

    record(args, "quiet", 1800, ["300:kill:S1", "600:load:6", "720:load:3", "900:kill:S2",
                                 "1200:load:6", "1320:load:3", "1500:kill:S3"])


def loop(args):
    """Faults with automatic recovery on: nobody restarts anything by hand."""

    from ft_system.blackbox.fingerpoint import HEALTHY
    from ft_system.blackbox.session import Session

    async def run():
        session = Session(fresh(args, "loop"), args.baseline, auto_recover=True)
        say(f"=== loop: {LOOP_TRIALS} faults, one every {LOOP_PERIOD} s, into {session.run_dir}")
        try:
            await session.start()
            board = session.dashboard
            while not (len(board.frames) >= 25 and board.status["state"] == HEALTHY
                       and all(server["level"] for server in board.frames[-1]["servers"].values())):
                await asyncio.sleep(0.5)
            for index in range(LOOP_TRIALS):
                server = SERVERS[index % len(SERVERS)]
                answer = await asyncio.get_running_loop().run_in_executor(None, session.actions()["inject"], server)
                say(f"[{time.strftime('%H:%M:%S')}] fault {index + 1} of {LOOP_TRIALS}: {answer}")
                await asyncio.sleep(LOOP_PERIOD)
        finally:
            await session.stop()

    asyncio.run(run())


def cost(args):
    """The same workload three ways: bare system, with the dormant fault shim, with the monitor running."""

    from ft_system.blackbox.cluster import LocalCluster, kill_tree, launch

    results = {}
    for name, label, shim, monitored in (("bare", "system alone", False, False),
                                         ("shim", "with dormant\nfault shim", True, False),
                                         ("monitored", "with shim, agent\nand collector", True, True)):
        run_dir = fresh(args, f"cost-{name}")
        run_dir.mkdir(parents=True, exist_ok=True)
        say(f"=== cost, {name}: {COST_SECONDS} s into {run_dir}")
        watchers = []
        try:
            if monitored:
                watchers.append(launch("ft_system.blackbox.collector", "--run-dir", str(run_dir), "--baseline",
                                       str(args.baseline), log=run_dir / "collector.log"))
                time.sleep(2)
            with LocalCluster(run_dir, "configs/ec.active.local.toml", 0.2, 3, shim=shim) as cluster:
                if monitored:
                    watchers.append(launch("ft_system.blackbox.agent", "--collector", f"127.0.0.1:{DEFAULT_PORT}",
                                           "--volume", str(run_dir), log=run_dir / "agent.log"))
                time.sleep(25)
                servers = cluster.server_processes()
                for process in servers.values():
                    process.cpu_percent(None)
                start, usage = time.time(), []
                for _ in range(COST_SECONDS):
                    time.sleep(1)
                    usage.append(statistics.fmean(process.cpu_percent(None) for process in servers.values()))
                end = time.time()
                time.sleep(3)  # late replies reach the logs
                replies = []
                for client_id in cluster.running_clients():
                    times, _ = impact.reply_times(cluster.log_text(client_id), start, end)
                    replies += [took for values in times.values() for took in values]
        finally:
            for process in watchers:
                if process.poll() is None:
                    kill_tree(process.pid)
        summary = impact.summarize(replies)
        results[name] = {"label": label, "replies": summary["n"], "median_ms": summary["median"],
                         "p95_ms": summary["p95"], "mean_ms": round(statistics.fmean(replies), 3),
                         "server_cpu": round(statistics.fmean(usage), 3)}
        say(f"    {results[name]}")
    (args.out / "cost.json").write_text(json.dumps(results, indent=1), encoding="utf-8")


EXPERIMENTS = {"detect": detect, "quiet": quiet, "loop": loop, "cost": cost}


# --- report ---

def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return "\n".join(lines + ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows])


def seconds(value):
    return "–" if value is None else f"{value:.1f} s"


def report(args):
    from ft_system.blackbox import evaluate
    from ft_system.blackbox import report as figures
    from ft_system.blackbox.baseline import Signature, spread
    from ft_system.blackbox.detect import FAST, PAPER
    from ft_system.blackbox.diagnose import replay

    signature, out = Signature.load(args.baseline), args.out / "report"
    out.mkdir(parents=True, exist_ok=True)
    settings = (("paper windows (7 and 15)", PAPER), ("fast windows (5 and 7)", FAST))
    text = ["# Evaluation results", "",
            f"Signature: `{args.baseline.name}`, trained on {signature.meta.get('os')}, Python "
            f"{signature.meta.get('python')}, {signature.meta.get('cores')} cores, config "
            f"`{signature.meta.get('config')}`, one request every {signature.meta.get('client_interval')} s per client.",
            ""]

    text += ["## What is normal", "", "Steady-state signature of one replica (S2), from the training run:", ""]
    rows = []
    for metric, entry in signature.data["components"].get("S2", {}).get("steady", {}).items():
        if metric in figures.DEFAULT_METRICS:
            width = 6 * spread(metric, entry)
            rows.append((f"`{metric}`", entry["n"], f"{entry['mean']:.2f}", f"{entry['std']:.2f}",
                         f"{max(0.0, entry['mean'] - width):.1f} to {entry['mean'] + width:.1f}"))
    text += [table(("Metric", "Samples", "Mean", "Standard deviation", "Normal band (6 deviations)"), rows), ""]

    if (args.out / "detect").exists():
        records = read_run(args.out / "detect")
        scored = {name: evaluate.fault_trials(records, replay(records, signature, profile))
                  for name, profile in settings}
        figures.plot_detection({name: result["faults"] for name, result in scored.items()}, out / "detection.png")
        text += ["## Does it detect the fault and blame the right replica?", "",
                 f"{len(scored[settings[0][0]]['faults'])} faults of {FAULT_SECONDS} s: every replica at each of "
                 "four intensities, twice. Replayed with both window settings.", ""]
        rows = []
        for name, result in scored.items():
            score = evaluate.accuracy(result)
            rows.append((name, f"{score['detected']} of {score['faults']}", score["false_alarms"],
                         *(f"{score[key]:.2f}" if score[key] is not None else "–"
                           for key in ("precision", "recall", "f_score"))))
        text += [table(("Setting", "Faults blamed on the right replica", "Wrong blames", "Precision", "Recall",
                        "F-score"), rows), ""]
        for name, result in scored.items():
            rows = [(f"{row['duty']:.0%}", f"{row['detected']} of {row['faults']}", seconds(row["delay_median"]),
                     seconds(row["delay_max"]), f"{row['cpu_task']} of {row['faults']}", f"{row['covered']:.0%}")
                    for row in evaluate.by_intensity(result["faults"])]
            text += [f"With the {name}:", "",
                     table(("Intensity", "Detected", "Median time to diagnosis", "Longest",
                            "Diagnosed as a CPU-intensive task", "Share of the fault's duration blamed"), rows), ""]
        text += ["![Time to diagnosis](detection.png)", ""]

    if (args.out / "quiet").exists():
        records = read_run(args.out / "quiet")
        text += ["## Does it raise false alarms?", "",
                 "A run with no injected fault, three replicas killed and relaunched, and the client load doubled "
                 "twice.", ""]
        rows = []
        for name, profile in settings:
            summary = evaluate.quiet_summary(records, replay(records, signature, profile))
            rows.append((name, f"{summary['minutes']:.0f}", summary["false_alarms"],
                         f"{summary['false_alarms_per_hour']:.1f}", summary["kills"],
                         ", ".join(seconds(value) for value in summary["load_reported_after"])))
        text += [table(("Setting", "Minutes", "Replicas blamed", "Per hour", "Kill and relaunch cycles",
                        "Load doublings reported as workload after"), rows), ""]
        figures.plot_run(args.out / "quiet", signature, out / "quiet.png",
                         metrics=("cpu", "ctx_voluntary_s", "io_write_count_s"),
                         title="Thirty minutes with no fault: three relaunches and two load changes")
        text += ["![The fault-free run](quiet.png)", ""]

    if (CALIBRATION / "calibration.json").exists():
        data = json.loads((CALIBRATION / "calibration.json").read_text(encoding="utf-8"))
        target = data["target"]
        lfd = next(CALIBRATION.glob(f"LFD{target[1:]}.log")).read_text(encoding="utf-8", errors="replace")
        levels = []
        for phase in [data["baseline"], *data["levels"]]:
            sent, answered = evaluate.heartbeats(lfd, target, phase["start"], phase["end"])
            reply = phase["reply_ms"][target]
            levels.append({"duty": (phase["fault"] or {}).get("duty"), "median_ms": reply["median"],
                           "p95_ms": reply["p95"], "sent": sent, "answered": answered,
                           "cpu": statistics.median(phase["cpu"][target])})
        figures.plot_impact(levels, out / "impact.png")
        text += ["## How does the fault hurt the service, and can heartbeats see it?", "",
                 f"Five minutes at each intensity, fault in {target}.", "",
                 table(("Intensity", f"{target} CPU (median)", "Reply time, median", "Reply time, 95th percentile",
                        "Heartbeats answered"),
                       [(figures._percent(level["duty"]), f"{level['cpu']:.1f}%", f"{level['median_ms']:,.0f} ms",
                         f"{level['p95_ms']:,.0f} ms", f"{level['answered']} of {level['sent']}") for level in levels]),
                 "", "![Fault impact](impact.png)", ""]

    if (args.out / "loop").exists():
        records = read_run(args.out / "loop")
        diagnoses = [record for record in records if record.get("event") == "diagnosis"]
        rm_log = (args.out / "loop" / "RM.log").read_text(encoding="utf-8", errors="replace")
        trials = evaluate.recovery_trials(records, diagnoses, rm_log, SERVERS)
        figures.plot_loop(trials, out / "loop.png")
        slow = []
        for trial in trials:
            if trial["restarted"] is None:
                continue
            for client in sorted((args.out / "loop").glob("C*.log")):
                times, _ = impact.reply_times(client.read_text(encoding="utf-8", errors="replace"),
                                              trial["start"], trial["start"] + trial["restarted"])
                slow += times.get(trial["server"], [])
        done = [trial for trial in trials if trial["ready"] is not None]
        text += ["## How fast is the whole loop?", "",
                 f"{len(trials)} faults at 75% with automatic recovery on; {len(done)} were repaired without anyone "
                 "touching the system.", "",
                 table(("Step", "Median", "Longest"),
                       [(label, seconds(evaluate.median_of(trials, key)),
                         seconds(max((trial[key] for trial in trials if trial[key] is not None), default=None)))
                        for label, key in (("Injection to diagnosis", "diagnosed"),
                                           ("Injection to restart", "restarted"),
                                           ("Injection to all replicas ready", "ready"),
                                           ("Injection to the diagnosis withdrawn", "normal"))]), ""]
        if slow:
            summary = impact.summarize(slow)
            text += [f"While a fault ran, the faulty replica answered clients in a median {summary['median']:.0f} ms "
                     f"(95th percentile {summary['p95']:.0f} ms).", ""]
        text += ["![The full loop](loop.png)", ""]

    if (args.out / "cost.json").exists():
        conditions = json.loads((args.out / "cost.json").read_text(encoding="utf-8"))
        figures.plot_cost(conditions, out / "cost.png")
        text += ["## What does monitoring cost?", "", f"{COST_SECONDS} s in each condition, no fault.", "",
                 table(("Condition", "Replies", "Reply time: mean", "median", "95th percentile",
                        "Mean CPU of one server"),
                       [(entry["label"].replace("\n", " "), f"{entry['replies']:,}", f"{entry['mean_ms']:.2f} ms",
                         f"{entry['median_ms']:g} ms", f"{entry['p95_ms']:g} ms", f"{entry['server_cpu']:.2f}%")
                        for entry in conditions.values()]), ""]
        costs = []
        for name in ("detect", "quiet", "loop"):
            if (args.out / name).exists():
                host = [record["m"]["agent_cpu"] for record in read_run(args.out / name)
                        if record.get("role") == "host" and "agent_cpu" in record.get("m", {})]
                costs.append((name, statistics.fmean(host[2:])))
        if costs:
            text += ["The agent's own CPU, as it reports it: " + ", ".join(
                f"{value:.2f}% of one core in the {name} run" for name, value in costs) + ".", ""]
        text += ["![Monitoring cost](cost.png)", ""]

    (out / "results.md").write_text("\n".join(text), encoding="utf-8")
    say(f"report written to {out / 'results.md'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, default=ROOT / baseline.DEFAULT_PATH, help="Signature file")
    parser.add_argument("--out", type=Path, default=ROOT / "logs" / "ec" / "eval", help="Folder for the recordings")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Record experiments")
    run.add_argument("experiments", nargs="+", choices=list(EXPERIMENTS))
    commands.add_parser("report", help="Score the recordings and draw the figures")
    args = parser.parse_args()
    if not args.baseline.exists():
        raise SystemExit(f"no signature at {args.baseline}: run 'python scripts/ec_run.py train' first")
    if args.command == "report":
        report(args)
        return 0
    for name in args.experiments:
        EXPERIMENTS[name](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
