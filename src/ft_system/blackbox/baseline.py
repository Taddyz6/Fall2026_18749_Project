"""Normal signature: what each component's OS metrics look like when nothing is wrong.

    python -m ft_system.blackbox.baseline train --run-dir logs/ec/train
    python -m ft_system.blackbox.baseline show
    python -m ft_system.blackbox.baseline check --run-dir logs/ec/another-run

Trained from a recorded fault-free run. For every component and metric it keeps
the mean, the standard deviation and percentiles, and from those the bands that
the graphs draw and the detector tests against: 3 and 6 standard deviations
either side of the mean.

Two things a plain mean and deviation get wrong here, and what is done instead:

- A young process is not like a mature one (on Windows a server has 6 threads
  for its first minute and 4 after it). The signature has two phases, split by
  the age of the process: warm-up statistics pooled over all components of a
  role, and steady statistics per component.
- An idle server's CPU sits at exactly 0 and its memory does not move, so the
  measured deviation is 0 and any blip would count as infinitely abnormal. Each
  kind of metric has a floor under its deviation (``floor``).

A signature holds only for the computer and workload it was trained on; ``check``
says so when a run's description differs. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from ft_system.blackbox.collector import read_run

DEFAULT_PATH = Path("logs/ec/baseline.json")
WARMUP_SECONDS = 90.0   # covers the minute a Windows process takes to shed its start-up threads
MIN_SAMPLES = 30        # fewer than this is too little to call a component's own statistics
WARN_SIGMAS, ANOMALY_SIGMAS = 3.0, 6.0
WINDOW, WINDOW_HITS = 7, 3   # an excursion is sustained when 3 of 7 consecutive samples are anomalous

# How the figures and the dashboard name a metric.
METRIC_NAMES = {
    "cpu": "CPU (% of one core)",
    "cpu_user": "User CPU (% of one core)",
    "cpu_sys": "System CPU (% of one core)",
    "ctx_voluntary_s": "Context switches per second",
    "threads": "Threads",
    "io_write_count_s": "Write operations per second",
    "io_other_count_s": "Other I/O operations per second",
    "rss_mb": "Memory (MB)",
}
# What makes two runs comparable: where it ran, and what it was asked to do.
META_KEYS = ("host", "os", "python", "cores", "interval", "config", "mode", "client_interval")


def describe(values: Sequence[float]) -> dict[str, float]:
    ordered, count = sorted(values), len(values)

    def rank(fraction: float) -> float:
        return ordered[min(count - 1, max(0, math.ceil(fraction * count) - 1))]

    return {"n": count, "mean": round(statistics.fmean(ordered), 4),
            "std": round(statistics.stdev(ordered), 4) if count > 1 else 0.0,
            "median": rank(0.5), "p01": rank(0.01), "p99": rank(0.99),
            "min": ordered[0], "max": ordered[-1]}


def floor(metric: str) -> tuple[float, float]:
    """Smallest deviation a metric is allowed: an absolute value, and a fraction of its mean."""

    if metric.startswith("cpu") or metric == "agent_cpu":
        return 2.0, 0.0       # percentage points
    if metric in ("threads", "components"):
        return 0.5, 0.0
    if metric.endswith("_bytes_s"):
        return 1024.0, 0.10
    if metric.endswith("_s"):
        return 1.0, 0.10      # operations per second
    if metric.endswith("_mb"):
        return 0.0, 0.008     # six deviations are then about 5% of the mean
    return 0.0, 0.05


def spread(metric: str, stats: Mapping[str, float]) -> float:
    """The deviation the bands are built from: the measured one, or the metric's floor."""

    absolute, relative = floor(metric)
    return max(stats["std"], absolute, relative * abs(stats["mean"]))


def level_of(value: float, metric: str, stats: Mapping[str, float], mean: float | None = None) -> int:
    """-2, -1, 0, +1 or +2: beyond 6 deviations, beyond 3, or inside, with the sign of the side.

    ``mean`` replaces the trained mean when the caller tracks a running one.
    """

    distance = (value - (stats["mean"] if mean is None else mean)) / spread(metric, stats)
    size = 2 if abs(distance) >= ANOMALY_SIGMAS else 1 if abs(distance) >= WARN_SIGMAS else 0
    return size if distance > 0 else -size


def describe_run(records: Iterable[dict]) -> dict:
    """Where a run was recorded and what workload it carried, from its own events."""

    meta: dict = {}
    for record in records:
        if record.get("event") == "agent_started":
            meta.update({key: record[key] for key in ("host", "os", "python", "cores", "interval")
                         if key in record})
        elif record.get("event") == "recording_started":
            meta.update({key: record[key] for key in ("config", "mode", "client_interval", "seconds")
                         if key in record})
    return meta


def train(records: Sequence[dict], warmup: float = WARMUP_SECONDS) -> dict:
    """Build a signature from the records of one fault-free run."""

    faults = [record for record in records if record.get("event") == "fault_injected"]
    if faults:
        raise ValueError(f"not a fault-free run: a fault was injected into {faults[0].get('server')}")
    steady: dict[str, dict[str, list[float]]] = {}
    role_of: dict[str, str] = {}
    roles: dict[str, dict[str, dict[str, list[float]]]] = {}
    count = 0
    for record in records:
        if record.get("type") != "sample":
            continue
        count += 1
        age = record.get("age")
        phase = "warmup" if age is not None and age < warmup else "steady"
        pooled = roles.setdefault(record["role"], {"warmup": {}, "steady": {}})[phase]
        own = steady.setdefault(record["label"], {}) if phase == "steady" else None
        role_of[record["label"]] = record["role"]
        for metric, value in record["m"].items():
            pooled.setdefault(metric, []).append(value)
            if own is not None:
                own.setdefault(metric, []).append(value)
    if not count:
        raise ValueError("the run has no samples")

    def summarize(table: dict[str, list[float]]) -> dict[str, dict[str, float]]:
        return {metric: describe(values) for metric, values in sorted(table.items())}

    return {
        "meta": describe_run(records) | {"created": round(time.time(), 3), "samples": count},
        "warmup_seconds": warmup,
        "components": {label: {"role": role_of[label], "steady": summarize(table)}
                       for label, table in sorted(steady.items())},
        "roles": {role: {phase: summarize(table) for phase, table in phases.items()}
                  for role, phases in sorted(roles.items())},
    }


class Signature:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.meta, self.warmup = data["meta"], data["warmup_seconds"]

    @classmethod
    def load(cls, path: str | Path) -> Signature:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.data, indent=1), encoding="utf-8")

    def stats(self, label: str, role: str, metric: str, age: float | None = None) -> dict | None:
        """What is normal for this metric of this component at this age; ``None`` if unknown."""

        pooled = self.data["roles"].get(role, {})
        if age is not None and age < self.warmup:
            choices = (pooled.get("warmup", {}), pooled.get("steady", {}))
        else:
            own = self.data["components"].get(label, {}).get("steady", {})
            choices = (own, pooled.get("steady", {}))
        for table in choices:
            entry = table.get(metric)
            if entry is not None and entry["n"] >= MIN_SAMPLES:
                return entry
        return None

    def band(self, label: str, role: str, metric: str, age: float | None = None,
             sigmas: float = ANOMALY_SIGMAS) -> tuple[float, float] | None:
        entry = self.stats(label, role, metric, age)
        if entry is None:
            return None
        width = sigmas * spread(metric, entry)
        return max(0.0, entry["mean"] - width), entry["mean"] + width  # no metric here goes below 0

    def level(self, sample: Mapping, metric: str) -> int | None:
        """How far outside normal one metric of one sample is; ``None`` if it cannot be judged."""

        entry = self.stats(sample["label"], sample["role"], metric, sample.get("age"))
        if entry is None or metric not in sample["m"]:
            return None
        return level_of(sample["m"][metric], metric, entry)

    def mismatches(self, run: Mapping) -> list[str]:
        """Ways a run differs from the one this signature was trained on."""

        return [f"{key}: trained on {self.meta[key]!r}, this run has {run[key]!r}"
                for key in META_KEYS if key in self.meta and key in run and self.meta[key] != run[key]]


def check(signature: Signature, records: Iterable[dict], role: str = "server") -> dict[tuple[str, str], dict]:
    """Per component and metric: how many samples fall outside the bands, and whether any
    excursion is sustained (3 anomalous samples within 7), which is what would be reported."""

    rows: dict[tuple[str, str], dict] = {}
    recent: dict[tuple[str, str], list[bool]] = {}
    for record in records:
        if record.get("type") != "sample" or record.get("role") != role:
            continue
        for metric in record["m"]:
            level = signature.level(record, metric)
            if level is None:
                continue
            key = (record["label"], metric)
            row = rows.setdefault(key, {"samples": 0, "warn": 0, "anomalous": 0, "sustained": 0})
            window = recent.setdefault(key, [])
            window.append(abs(level) == 2)
            del window[:-WINDOW]
            row["samples"] += 1
            row["warn"] += abs(level) >= 1
            row["anomalous"] += abs(level) == 2
            row["sustained"] += sum(window) >= WINDOW_HITS
    return rows


def format_signature(signature: Signature, role: str = "server") -> str:
    lines = [f"trained on: {json.dumps({key: signature.meta.get(key) for key in META_KEYS})}",
             f"samples: {signature.meta['samples']}; a process is in warm-up for its first "
             f"{signature.warmup:g} s", "",
             "component  metric                      n      mean      std   median      p01      p99"
             "   normal band (6 deviations)"]

    def rows(name: str, table: Mapping[str, Mapping[str, float]]) -> None:
        for metric, entry in table.items():
            width = ANOMALY_SIGMAS * spread(metric, entry)
            lines.append(f"{name:10s} {metric:22s} {entry['n']:6d} {entry['mean']:9.2f} {entry['std']:8.2f} "
                         f"{entry['median']:8.2f} {entry['p01']:8.2f} {entry['p99']:8.2f}   "
                         f"{max(0.0, entry['mean'] - width):9.2f} to {entry['mean'] + width:.2f}")

    for label, component in signature.data["components"].items():
        if component["role"] == role:
            rows(label, component["steady"])
    pooled = signature.data["roles"].get(role, {})
    rows(f"{role}s", pooled.get("steady", {}))
    rows("warm-up", pooled.get("warmup", {}))
    return "\n".join(lines)


def format_check(rows: Mapping[tuple[str, str], Mapping[str, int]]) -> str:
    lines = ["component  metric                 samples  beyond 3 dev  beyond 6 dev  sustained"]
    for (label, metric), row in sorted(rows.items()):
        lines.append(f"{label:10s} {metric:22s} {row['samples']:7d} {row['warn']:13d} {row['anomalous']:13d} "
                     f"{row['sustained']:10d}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train, show or test the normal signature")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_PATH,
                        help=f"Signature file (default {DEFAULT_PATH.as_posix()})")
    commands = parser.add_subparsers(dest="command", required=True)
    learn = commands.add_parser("train", help="Build the signature from a fault-free recorded run")
    learn.add_argument("--run-dir", type=Path, required=True)
    learn.add_argument("--warmup", type=float, default=WARMUP_SECONDS,
                       help="Seconds a process counts as young (default 90)")
    commands.add_parser("show", help="Print the signature as a table")
    test = commands.add_parser("check", help="Count how much of a recorded run is outside the signature")
    test.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "train":
        try:
            signature = Signature(train(read_run(args.run_dir), args.warmup))
        except ValueError as exc:
            parser.error(str(exc))
        signature.save(args.baseline)
        print(f"signature written to {args.baseline}")
        print(format_signature(signature))
        return 0
    signature = Signature.load(args.baseline)
    if args.command == "show":
        print(format_signature(signature))
        return 0
    records = read_run(args.run_dir)
    for difference in signature.mismatches(describe_run(records)):
        print(f"warning: {difference}")
    rows = check(signature, records)
    print(format_check(rows))
    sustained = sorted({label for (label, _), row in rows.items() if row["sustained"]})
    print("verdict: " + (f"outside the signature: {', '.join(sustained)}" if sustained
                         else "every server stays inside the signature"))
    return 1 if sustained else 0


if __name__ == "__main__":
    raise SystemExit(main())
