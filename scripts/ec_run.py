"""Run the black-box diagnosis demo on this computer with one command.

    python scripts/ec_run.py train                 # once per computer: record 5 fault-free minutes, learn normal
    python scripts/ec_run.py up                    # start everything; open the page it prints
    python scripts/ec_run.py autopilot             # start, run the whole demo unattended, check it, stop

While "up" is running, from another terminal (or with the buttons on the page):

    python scripts/ec_run.py inject S2             # start the CPU fault in S2 (add :0.5 for another intensity)
    python scripts/ec_run.py clear S2
    python scripts/ec_run.py load 6                # run 6 clients instead of 3; "load 3" goes back
    python scripts/ec_run.py rejuvenate            # kill the diagnosed replica so the RM relaunches it
    python scripts/ec_run.py auto on               # let the session do that by itself; "auto off" stops it
    python scripts/ec_run.py down                  # stop everything

"up" starts the system (RM, GFD, three LFDs with their servers, three clients),
the collection agent, and the collector with the diagnosis and the dashboard.
"down" also cleans up after a session that did not stop properly. Needs psutil.
"""

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psutil  # noqa: E402

from ft_system.blackbox import baseline  # noqa: E402
from ft_system.blackbox.cluster import ClusterError, sweep  # noqa: E402
from ft_system.blackbox.collector import DEFAULT_PORT as COLLECTOR_PORT  # noqa: E402
from ft_system.blackbox.dashboard import DEFAULT_PORT as DASHBOARD_PORT  # noqa: E402
from ft_system.blackbox.session import DEFAULT_CONFIG, Session, StepFailed, autopilot, send  # noqa: E402

REMOTE = ("inject", "clear", "load", "rejuvenate", "auto")


def say(text=""):
    print(text, flush=True)


def new_session(args, name):
    run_dir = args.run_dir or ROOT / "logs" / "ec" / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    if not args.baseline.exists():
        raise SystemExit(f"no signature at {args.baseline}: run 'python scripts/ec_run.py train' first")
    return Session(run_dir, args.baseline, args.config, args.profile, args.interval,
                   collector_port=args.port, dashboard_port=args.dashboard, auto_recover=args.auto_recover)


async def up(args):
    session = new_session(args, "session")
    try:
        say("starting the system; this takes about 15 s")
        await session.start()
        say(f"dashboard: http://127.0.0.1:{args.dashboard}/")
        say("stop with Ctrl+C here, or 'python scripts/ec_run.py down' from another terminal")
        await session.stopping.wait()
    finally:
        say("stopping")
        await session.stop()
    return 0


async def unattended(args):
    failures = 0
    for round_number in range(1, args.rounds + 1):
        session = new_session(args, "autopilot")
        say(f"round {round_number} of {args.rounds}: {session.run_dir}")
        try:
            await session.start()
            say(f"dashboard: http://127.0.0.1:{args.dashboard}/")
            timings = await autopilot(session, say)
            say("PASS: " + "; ".join(f"{name} in {seconds:.1f} s" for name, seconds in timings.items()))
        except StepFailed as exc:
            failures += 1
            say(f"FAIL: {exc}")
        finally:
            await session.stop()
    say(f"{args.rounds - failures} of {args.rounds} rounds passed")
    return 1 if failures else 0


def train(args):
    run_dir = args.run_dir or ROOT / "logs" / "ec" / f"train-{datetime.now():%Y%m%d-%H%M%S}"
    say(f"recording {args.seconds:g} fault-free seconds into {run_dir}")
    recorded = subprocess.run([sys.executable, str(ROOT / "scripts" / "ec_experiment.py"), "--seconds",
                               str(args.seconds), "--run-dir", str(run_dir), "--config", args.config,
                               "--interval", str(args.interval), "--port", str(args.port)], cwd=ROOT)
    if recorded.returncode:
        return recorded.returncode
    return baseline.main(["--baseline", str(args.baseline), "train", "--run-dir", str(run_dir)])


def down(args):
    try:
        say(send("down", port=args.dashboard)[1])
        return 0
    except ConnectionError:
        pass
    # No session answered: clean up whatever one left behind.
    others = [process for process in psutil.process_iter(["cmdline"])
              if process.pid != os.getpid() and any(Path(part).name == "ec_run.py" for part in process.info["cmdline"] or ())]
    for process in others:
        process.kill()
    killed = [process.pid for process in others] + sweep()
    say(f"no session was listening; killed {len(killed)} leftover processes" if killed
        else "nothing is running")
    return 0


def remote(args):
    argument = " ".join(args.argument)
    if args.command == "inject" and args.duty is not None:
        argument += f":{args.duty}"
    try:
        done, answer = send(args.command, argument, port=args.dashboard)
    except ConnectionError as exc:
        raise SystemExit(f"{exc}; start one with 'python scripts/ec_run.py up'") from None
    say(answer if done else f"not done: {answer}")
    return 0 if done else 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, default=ROOT / baseline.DEFAULT_PATH, help="Signature file")
    parser.add_argument("--dashboard", type=int, default=DASHBOARD_PORT, metavar="PORT", help="Dashboard port")
    commands = parser.add_subparsers(dest="command", required=True)

    def session_options(sub):
        sub.add_argument("--config", default=DEFAULT_CONFIG)
        sub.add_argument("--interval", type=float, default=0.2, help="Seconds between each client's requests")
        sub.add_argument("--port", type=int, default=COLLECTOR_PORT, help="Collector port")
        sub.add_argument("--run-dir", type=Path, help="Default: a new folder under logs/ec/")

    learn = commands.add_parser("train", help="Record a fault-free run and learn the normal signature from it")
    learn.add_argument("--seconds", type=float, default=300)
    session_options(learn)
    for name, text in (("up", "Start everything and keep it running"),
                       ("autopilot", "Start, run the scripted demo with checks, and stop")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--profile", choices=("paper", "fast"), default="paper", help="Detection windows")
        sub.add_argument("--auto-recover", action="store_true",
                         help="Restart a diagnosed replica without being asked")
        session_options(sub)
        if name == "autopilot":
            sub.add_argument("--rounds", type=int, default=1, help="How many times to run it")
    for name, text, takes in (("inject", "Start the CPU fault in a replica, for example S2", "+"),
                              ("clear", "Stop the fault in a replica", "+"),
                              ("load", "Run this many clients", "+"),
                              ("rejuvenate", "Kill a replica (default: the one diagnosed) so the RM relaunches it", "*"),
                              ("auto", "Turn automatic recovery on or off", "+")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("argument", nargs=takes)
        if name == "inject":
            sub.add_argument("--duty", type=float, help="Busy fraction of the fault (default 0.75)")
    commands.add_parser("down", help="Stop the running session, or clean up after one")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command == "train":
            return train(args)
        if args.command == "down":
            return down(args)
        if args.command in REMOTE:
            return remote(args)
        return asyncio.run(up(args) if args.command == "up" else unattended(args))
    except KeyboardInterrupt:
        return 0
    except (ClusterError, OSError) as exc:
        raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    raise SystemExit(main())
