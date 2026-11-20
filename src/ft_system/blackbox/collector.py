"""Collector: receive records from the agents, stamp when they arrived, keep them.

    python -m ft_system.blackbox.collector --run-dir logs/ec/demo
    python -m ft_system.blackbox.collector --run-dir logs/ec/demo --baseline logs/ec/baseline.json
    python -m ft_system.blackbox.collector --run-dir logs/ec/demo --baseline logs/ec/baseline.json --dashboard
    python -m ft_system.blackbox.collector --run-dir logs/ec/demo --summary

Samples go to ``samples.jsonl`` and events to ``events.jsonl`` in the run
directory; the last ten minutes also stay in memory for the detector and the
dashboard. Arrival time (``recv``) is the time axis, so the agents' clocks need
not agree. Given a signature (``--baseline``), it also diagnoses as the records
arrive and stores each diagnosis as an event; with ``--dashboard`` it also serves
the live page. Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from ft_system.blackbox.faults.ctl import EVENTS_FILE
from ft_system.common.logging import EventLogger

DEFAULT_PORT = 8749
SAMPLES_FILE = "samples.jsonl"


def when(record: dict) -> float:
    """A record's place on the time axis: when it arrived, else when it says it happened."""

    return record.get("recv", record.get("t", 0.0))


def usable(record: object) -> bool:
    """An event names what happened; a sample names its component and carries metrics."""

    if not isinstance(record, dict):
        return False
    if "event" in record:
        return isinstance(record["event"], str)
    return (record.get("type") == "sample" and isinstance(record.get("label"), str)
            and isinstance(record.get("m"), dict))


class Store:
    """Every record received: on disk if given a run directory, the recent ones in memory."""

    def __init__(self, run_dir: Path | None = None, window: int = 600) -> None:
        self.window = window
        self.recent: dict[str, deque[dict]] = {}  # label -> newest samples, oldest first
        self.events: list[dict] = []
        self.rejected = self.duplicates = 0
        self._last_seq: dict[str, int] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        self._events_path = run_dir / EVENTS_FILE if run_dir else None
        self._samples = ((run_dir / SAMPLES_FILE).open("a", encoding="utf-8", buffering=1)
                         if run_dir else None)

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        """Call ``callback`` with each record as it is accepted."""

        self._subscribers.append(callback)

    def add(self, record: object) -> bool:
        """Keep one record. ``False`` if it is malformed or was already received."""

        if not usable(record):
            self.rejected += 1
            return False
        agent, seq = record.get("agent"), record.get("seq")
        if isinstance(agent, str) and isinstance(seq, int):
            # An agent resends a whole batch when a send fails part-way.
            if seq <= self._last_seq.get(agent, 0):
                self.duplicates += 1
                return False
            self._last_seq[agent] = seq
        if "event" in record:
            record.setdefault("type", "event")
            self.events.append(record)
            if self._events_path is not None:
                # Opened per event: the fault controller appends to the same file.
                with self._events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
        else:
            self.recent.setdefault(record["label"], deque(maxlen=self.window)).append(record)
            if self._samples is not None:
                self._samples.write(json.dumps(record, separators=(",", ":")) + "\n")
        for callback in self._subscribers:
            callback(record)
        return True

    def series(self, label: str, metric: str) -> list[tuple[float, float]]:
        """Recent ``(time, value)`` points of one metric of one component."""

        return [(when(sample), sample["m"][metric])
                for sample in self.recent.get(label, ()) if metric in sample["m"]]

    def close(self) -> None:
        if self._samples is not None:
            self._samples.close()


def read_run(run_dir: Path) -> list[dict]:
    """Every record of a recorded run, in time order."""

    records = []
    for name in (SAMPLES_FILE, EVENTS_FILE):
        path = Path(run_dir) / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return sorted(records, key=when)


def replay(run_dir: Path, store: Store) -> None:
    """Feed a recorded run through ``store`` as if it were arriving now."""

    for record in read_run(run_dir):
        store.add(record)


def summarize(records: Iterable[dict]) -> dict[str, dict]:
    """Per component: sample count, time span, incarnations seen, mean and peak CPU."""

    summary: dict[str, dict] = {}
    for record in records:
        if record.get("type") != "sample":
            continue
        row = summary.setdefault(record["label"], {
            "role": record.get("role"), "samples": 0, "first": when(record), "incarnations": [],
            "cpu_sum": 0.0, "cpu_max": 0.0})
        row["samples"] += 1
        row["last"] = when(record)
        incarnation = record.get("incarnation")
        if incarnation is not None and incarnation not in row["incarnations"]:
            row["incarnations"].append(incarnation)
        cpu = record["m"].get("agent_cpu" if record.get("role") == "host" else "cpu", 0.0)
        row["cpu_sum"] += cpu
        row["cpu_max"] = max(row["cpu_max"], cpu)
    for row in summary.values():
        row["cpu_mean"] = row.pop("cpu_sum") / row["samples"]
    return summary


def format_summary(summary: dict[str, dict]) -> str:
    lines = ["component            role    samples  seconds  incarnations  cpu% mean   max"]
    for label in sorted(summary, key=lambda label: (summary[label]["role"] == "host", label)):
        row = summary[label]
        note = "  (agent's own cpu)" if row["role"] == "host" else ""
        lines.append(f"{label:20s} {row['role'] or '?':7s} {row['samples']:7d}  {row['last'] - row['first']:7.1f}  "
                     f"{','.join(map(str, row['incarnations'])) or '-':12s}  {row['cpu_mean']:9.2f} "
                     f"{row['cpu_max']:5.1f}{note}")
    return "\n".join(lines)


def attach_diagnosis(store: Store, baseline: Path, profile: str, logger: EventLogger):
    """Diagnose every record the store accepts; store and print each change of verdict.

    Returns the signature and the diagnoser, for a dashboard to show.
    """

    # Imported here: these modules read recorded runs through this one.
    from ft_system.blackbox.baseline import Signature, describe_run
    from ft_system.blackbox.detect import PROFILES
    from ft_system.blackbox.diagnose import Diagnoser

    signature = Signature.load(baseline)
    diagnoser = Diagnoser(signature, PROFILES[profile])

    def on_record(record: dict) -> None:
        if record.get("event") == "agent_started":
            for difference in signature.mismatches(describe_run([record])):
                logger.error("collector", f"the signature may not apply here: {difference}")
        for diagnosis in diagnoser.observe(record):
            logger.event(f"DIAG: {diagnosis['summary']}")
            store.add(diagnosis)

    store.subscribe(on_record)
    logger.event(f"collector: diagnosing against {baseline} ({profile} windows)")
    return signature, diagnoser


async def start(store: Store, bind: str, port: int, logger: EventLogger) -> asyncio.Server:
    """Listen for agents. Each sends JSON lines; each line is stamped and stored."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = ":".join(map(str, writer.get_extra_info("peername")[:2]))
        logger.event(f"collector: agent connected from {peer}")
        try:
            async for line in reader:
                try:
                    record = json.loads(line)
                except ValueError:
                    store.rejected += 1
                    continue
                if isinstance(record, dict):
                    record["recv"] = round(time.time(), 3)
                store.add(record)
        except (ConnectionError, ValueError):  # ValueError: a line longer than the stream limit
            pass
        finally:
            logger.event(f"collector: agent at {peer} disconnected")
            writer.close()

    return await asyncio.start_server(handle, bind, port)


async def serve(store: Store, bind: str, port: int, logger: EventLogger,
                dashboard=None, dashboard_port: int = 0) -> None:
    server = await start(store, bind, port, logger)
    logger.event(f"collector: listening on {bind}:{port}")
    if dashboard is not None:
        await dashboard.start(bind, dashboard_port)
        logger.event(f"collector: dashboard at http://{'127.0.0.1' if bind == '0.0.0.0' else bind}:{dashboard_port}/")
    async with server:
        await server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Receive and store the agents' records")
    parser.add_argument("--run-dir", type=Path, required=True, help="Folder for samples.jsonl and events.jsonl")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Address to listen on; 0.0.0.0 to accept agents on other computers")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--baseline", type=Path, help="Signature file; with it, diagnose as records arrive")
    parser.add_argument("--profile", choices=("paper", "fast"), default="paper",
                        help="Detection windows: the paper's 7 and 15 samples, or the faster 5 and 7")
    parser.add_argument("--dashboard", type=int, nargs="?", const=8750, metavar="PORT",
                        help="Serve the live page on this port (default 8750); needs --baseline")
    parser.add_argument("--summary", action="store_true", help="Print what the run directory holds, then exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.summary:
        print(format_summary(summarize(read_run(args.run_dir))))
        return 0
    args.run_dir.mkdir(parents=True, exist_ok=True)
    if args.dashboard and args.baseline is None:
        parser.error("--dashboard needs --baseline: the page shows the normal range and the diagnosis")
    store, logger, dashboard = Store(args.run_dir), EventLogger(), None
    if args.baseline is not None:
        signature, diagnoser = attach_diagnosis(store, args.baseline, args.profile, logger)
        if args.dashboard:
            from ft_system.blackbox.dashboard import Dashboard

            dashboard = Dashboard(signature, diagnoser, args.run_dir)
            store.subscribe(dashboard.observe)
    try:
        asyncio.run(serve(store, args.bind, args.port, logger, dashboard, args.dashboard or 0))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        parser.error(f"cannot listen on {args.bind}:{args.port}: {exc}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
