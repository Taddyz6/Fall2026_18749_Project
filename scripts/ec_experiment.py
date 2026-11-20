"""Record one run of the whole system with the monitor attached.

    python scripts/ec_experiment.py --seconds 120
    python scripts/ec_experiment.py --seconds 120 --do 30:inject:S2:0.75 --do 70:clear:S2 --do 90:kill:S2
    python scripts/ec_experiment.py --seconds 180 --do 60:load:6 --do 120:load:3 --baseline logs/ec/baseline.json

Starts the collector, the system (RM, GFD, LFDs with their servers, clients) and
the agent on this computer, performs each scheduled action that many seconds
after recording starts, then stops everything and prints what was recorded.
The run directory then holds samples.jsonl, events.jsonl and every log.

Actions: SECONDS:inject:SERVER[:DUTY], SECONDS:clear:SERVER, SECONDS:kill:SERVER, and
SECONDS:load:CLIENTS, which changes how many clients are running (3 at the start; the
config has 6). With --baseline the collector also diagnoses while the run is recorded.
Needs psutil.
"""

import argparse
import socket
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ft_system.blackbox.cluster import ClusterError, LocalCluster, kill_tree, launch  # noqa: E402
from ft_system.blackbox.collector import DEFAULT_PORT, format_summary, read_run, summarize  # noqa: E402
from ft_system.blackbox.faults import ctl  # noqa: E402


def say(text=""):
    print(text, flush=True)


def parse_action(text):
    parts = text.split(":")
    try:
        at, kind, target = float(parts[0]), parts[1], parts[2]
        duty = float(parts[3]) if len(parts) > 3 else 0.75
        if kind == "load":
            target = int(target)
    except (IndexError, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r} is not SECONDS:ACTION:TARGET[:DUTY]") from None
    if kind not in ("inject", "clear", "kill", "load") or len(parts) > 4:
        raise argparse.ArgumentTypeError(f"{text!r}: the action must be inject, clear, kill or load")
    return at, kind, target, duty


def wait_listening(port, process, log, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"the collector exited; see {log}:\n{log.read_text(errors='replace')[-400:]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"the collector did not start listening on port {port}")


def perform(cluster, kind, server, duty):
    if kind == "load":
        cluster.set_clients(server)
        ctl.record(cluster.run_dir, {"t": time.time(), "event": "load_changed", "clients": server})
        return f"{server} clients running"
    if kind == "inject":
        state = ctl.inject(cluster.run_dir, server, duty=duty)
        return ctl.describe(state["applied"])
    if kind == "clear":
        ctl.clear(cluster.run_dir, server)
        return "fault cleared"
    pid = cluster.kill_replica(server)
    ctl.record(cluster.run_dir, {"t": time.time(), "event": "replica_killed", "server": server, "pid": pid})
    return f"killed pid {pid}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=120, help="How long to record (default 120)")
    parser.add_argument("--do", type=parse_action, action="append", default=[], metavar="SECONDS:ACTION:SERVER[:DUTY]",
                        help="Scheduled action; repeatable")
    parser.add_argument("--interval", type=float, default=0.2, help="Seconds between each client's requests")
    parser.add_argument("--config", default="configs/ec.active.local.toml")
    parser.add_argument("--clients", type=int, default=3, help="Clients running at the start (default 3)")
    parser.add_argument("--baseline", type=Path, help="Signature file: diagnose while recording")
    parser.add_argument("--profile", choices=("paper", "fast"), default="paper")
    parser.add_argument("--dashboard", type=int, nargs="?", const=8750, metavar="PORT",
                        help="Serve the live page on this port (default 8750); needs --baseline")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Collector port")
    parser.add_argument("--run-dir", type=Path, help="Default: logs/ec/run-<time>")
    args = parser.parse_args()
    run_dir = (args.run_dir or ROOT / "logs" / "ec" / f"run-{datetime.now():%Y%m%d-%H%M%S}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    say(f"run directory: {run_dir}")

    if args.dashboard and not args.baseline:
        parser.error("--dashboard needs --baseline")
    diagnosis = ["--baseline", str(args.baseline.resolve()), "--profile", args.profile] if args.baseline else []
    if args.dashboard:
        diagnosis += ["--dashboard", str(args.dashboard)]
        say(f"dashboard: http://127.0.0.1:{args.dashboard}/")
    collector = launch("ft_system.blackbox.collector", "--run-dir", str(run_dir), "--port", str(args.port),
                       *diagnosis, log=run_dir / "collector.log")
    agent = None
    try:
        wait_listening(args.port, collector, run_dir / "collector.log")
        with LocalCluster(run_dir, args.config, args.interval, args.clients) as cluster:
            agent = launch("ft_system.blackbox.agent", "--collector", f"127.0.0.1:{args.port}",
                           "--volume", str(run_dir), log=run_dir / "agent.log")
            started = time.time()
            replication = cluster.config.replication
            ctl.record(run_dir, {"t": started, "event": "recording_started", "seconds": args.seconds,
                                 "config": str(args.config), "config_hash": cluster.config_hash,
                                 "mode": replication.mode if replication else None,
                                 "client_interval": args.interval, "clients": args.clients,
                                 "actions": [list(action) for action in args.do]})
            say(f"[{datetime.now():%H:%M:%S}] recording {args.seconds:g} s")
            for at, kind, server, duty in sorted(args.do):
                time.sleep(max(0.0, started + at - time.time()))
                try:
                    outcome = perform(cluster, kind, server, duty)
                except (ctl.FaultError, ClusterError) as exc:
                    # One action that cannot be done must not cost a long recording.
                    outcome = f"NOT DONE: {exc}"
                    ctl.record(run_dir, {"t": time.time(), "event": "action_failed", "action": kind,
                                         "target": server, "error": str(exc)})
                say(f"[{datetime.now():%H:%M:%S}] +{time.time() - started:5.1f} s  {kind} {server}: {outcome}")
            time.sleep(max(0.0, started + args.seconds - time.time()))
            kill_tree(agent.pid)  # before the system stops, so its teardown is not recorded
            ctl.record(run_dir, {"t": time.time(), "event": "recording_stopped"})
    finally:
        for process in (agent, collector):
            if process is not None and process.poll() is None:
                kill_tree(process.pid)

    records = read_run(run_dir)
    say()
    say(format_summary(summarize(records)))
    kinds = Counter(record["event"] for record in records if "event" in record)
    say("events: " + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())))
    for record in records:
        if record.get("event") == "diagnosis":
            say(f"+{record['t'] - started:6.1f} s  DIAG  {record['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
