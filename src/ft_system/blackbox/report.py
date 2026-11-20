"""Figures of a recorded run against the normal signature.

    python -m ft_system.blackbox.report --run-dir logs/ec/some-run
    python -m ft_system.blackbox.report --run-dir logs/ec/some-run --metrics cpu rss_mb --out figures/

One figure per run: a row per metric and a column per replica, so the replicas
can be compared side by side on the same scale. Behind each line is the band the
signature calls normal for a process of that age; samples outside it are
marked; a numbered rule marks each thing that was done to the system and each
diagnosis that replaying the run produces.

Needs matplotlib. Nothing else in the monitor does.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # files only; no window

from matplotlib import pyplot  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from ft_system.blackbox.baseline import (  # noqa: E402
    ANOMALY_SIGMAS, DEFAULT_PATH, METRIC_NAMES as NAMES, WARN_SIGMAS, Signature,
)
from ft_system.blackbox.classify import CAUSES  # noqa: E402
from ft_system.blackbox.collector import read_run, when  # noqa: E402
from ft_system.blackbox.detect import PAPER, PROFILES, Profile  # noqa: E402
from ft_system.blackbox.diagnose import replay  # noqa: E402
from ft_system.blackbox.fingerpoint import FAULT, WORKLOAD  # noqa: E402

DEFAULT_METRICS = ("cpu", "ctx_voluntary_s", "threads", "io_write_count_s", "rss_mb")
EVENTS = {"fault_injected": "fault injected into {who}", "fault_cleared": "fault cleared in {who}",
          "replica_killed": "{who} killed", "process_started": "{who} relaunched",
          "load_changed": "client count set to {clients}"}
PER_LINE = 4  # caption entries per line

SURFACE, INK, INK_SECONDARY, INK_MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SERIES, CRITICAL = "#e1e0d9", "#c3c2b7", "#2a78d6", "#d03b3b"
GAP_SECONDS = 2.5  # a longer silence than this is an outage, not a line to draw


def collect(records: Sequence[dict], signature: Signature, metrics: Sequence[str], role: str,
            profile: Profile = PAPER):
    """Per replica and metric: times, values, the normal band at each sample, and which are outside.

    Also the events to mark, as ``(seconds, replica or None for all, text)``.
    """

    samples = [record for record in records if record.get("type") == "sample" and record.get("role") == role]
    if not samples:
        raise ValueError(f"the run has no samples of role {role!r}")
    start = next((when(record) for record in records if record.get("event") == "recording_started"),
                 when(samples[0]))
    series: dict[str, dict[str, dict[str, list]]] = {}
    for sample in samples:
        for metric in metrics:
            if metric not in sample["m"]:
                continue
            point = series.setdefault(sample["label"], {}).setdefault(
                metric, {"t": [], "value": [], "inner": [], "outer": [], "outside": []})
            moment = when(sample) - start
            if point["t"] and moment - point["t"][-1] > GAP_SECONDS:
                for column in point.values():  # break the line and the band across the outage
                    column.append(math.nan)
            point["t"].append(moment)
            point["value"].append(sample["m"][metric])
            arguments = (sample["label"], role, metric, sample.get("age"))
            point["inner"].append(signature.band(*arguments, sigmas=WARN_SIGMAS) or (math.nan, math.nan))
            point["outer"].append(signature.band(*arguments, sigmas=ANOMALY_SIGMAS) or (math.nan, math.nan))
            point["outside"].append(abs(signature.level(sample, metric) or 0) == 2)
    events = []
    for record in records:
        kind, who = record.get("event"), record.get("server") or record.get("label")
        if kind == "load_changed":
            events.append((when(record) - start, None, EVENTS[kind].format(**record)))
        elif kind in EVENTS and who in series and (kind != "process_started" or record.get("incarnation", 1) > 1):
            events.append((when(record) - start, who, EVENTS[kind].format(who=who)))
    blamed = None
    for diagnosis in replay(records, signature, profile):
        if diagnosis["status"] == FAULT:
            blamed = diagnosis["culprit"]
            text = f"{blamed} diagnosed: {CAUSES[diagnosis['cause']][0]}"
        elif diagnosis["status"] == WORKLOAD:
            blamed, text = None, "diagnosed: workload change, no replica blamed"
        else:
            text = "diagnosis withdrawn"
        events.append((diagnosis["t"] - start, blamed, text))
    return series, sorted(events, key=lambda event: event[0])


def plot_run(run_dir: str | Path, signature: Signature, out: str | Path | None = None,
             metrics: Sequence[str] = DEFAULT_METRICS, role: str = "server", title: str | None = None,
             profile: Profile = PAPER) -> Path:
    """Write the figure for one recorded run and return its path."""

    run_dir = Path(run_dir)
    series, events = collect(read_run(run_dir), signature, metrics, role, profile)
    labels = sorted(series)
    metrics = [metric for metric in metrics if any(metric in series[label] for label in labels)]
    duration = max(moment for label in labels for point in series[label].values()
                   for moment in point["t"] if not math.isnan(moment))
    pyplot.rcParams.update({"font.family": "sans-serif", "font.size": 8,
                            "font.sans-serif": ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]})
    caption_lines = max(1, -(-len(events) // PER_LINE))
    height = 1.7 * len(metrics) + 1.15 + 0.16 * caption_lines  # inches; the header is placed in inches
    figure, grid = pyplot.subplots(len(metrics), len(labels), sharex=True, sharey="row", squeeze=False,
                                   figsize=(3.6 * len(labels) + 0.6, height), facecolor=SURFACE)
    for row, metric in enumerate(metrics):
        for column, label in enumerate(labels):
            axes, point = grid[row][column], series[label].get(metric)
            axes.set_facecolor(SURFACE)
            axes.grid(axis="y", color=GRID, linewidth=0.6)
            axes.set_axisbelow(True)
            axes.tick_params(colors=INK_MUTED, length=0, labelsize=7.5)
            for side, spine in axes.spines.items():
                spine.set_visible(side == "bottom")
                spine.set_color(AXIS)
            if row == 0:
                axes.set_title(label, color=INK, fontsize=10, fontweight="semibold", loc="left", pad=6)
            if column == 0:
                axes.set_ylabel(NAMES.get(metric, metric), color=INK_SECONDARY, fontsize=8)
            if row == len(metrics) - 1:
                axes.set_xlabel("Seconds since recording started", color=INK_SECONDARY, fontsize=8)
            if point is None:
                continue
            for name, alpha in (("outer", 0.10), ("inner", 0.14)):
                axes.fill_between(point["t"], [low for low, _ in _pairs(point[name])],
                                  [high for _, high in _pairs(point[name])],
                                  color=INK_MUTED, alpha=alpha, linewidth=0)
            axes.plot(point["t"], point["value"], color=SERIES, linewidth=1.5,
                      solid_joinstyle="round", solid_capstyle="round")
            marked = [(t, value) for t, value, outside in zip(point["t"], point["value"], point["outside"])
                      if outside is True]
            if marked:
                axes.scatter(*zip(*marked), s=18, color=CRITICAL, edgecolors=SURFACE, linewidths=1.0, zorder=3)
            previous, raised = -math.inf, False
            for number, (moment, who, _) in enumerate(events, start=1):
                if who not in (None, label):
                    continue
                axes.axvline(moment, color=INK_SECONDARY, linewidth=0.7, zorder=1)
                if row == 0:
                    # Numbers of rules that are close together sit on two levels, so they stay apart.
                    raised = not raised and moment - previous < 0.04 * duration
                    previous = moment
                    axes.annotate(str(number), (moment, 1.0), xycoords=("data", "axes fraction"),
                                  xytext=(0, 11 if raised else 2), textcoords="offset points", ha="center",
                                  va="bottom", color=INK_SECONDARY, fontsize=7.5, annotation_clip=False)
        _scale(grid[row], [series[label][metric] for label in labels if metric in series[label]])

    key = [Line2D([], [], color=SERIES, linewidth=1.5, label="measured"),
           Patch(color=INK_MUTED, alpha=0.24, linewidth=0, label=f"within {WARN_SIGMAS:g} deviations of normal"),
           Patch(color=INK_MUTED, alpha=0.10, linewidth=0, label=f"within {ANOMALY_SIGMAS:g} deviations"),
           Line2D([], [], color=CRITICAL, marker="o", markersize=4.5, markeredgecolor=SURFACE, linewidth=0,
                  label=f"outside {ANOMALY_SIGMAS:g} deviations")]
    figure.legend(handles=key, loc="upper right", ncols=4, frameon=False, fontsize=8, labelcolor=INK_SECONDARY,
                  bbox_to_anchor=(0.995, 1 - 0.04 / height), handlelength=1.6, columnspacing=1.4)
    figure.suptitle(title or f"Run {run_dir.name}: each replica against the normal signature",
                    x=0.012, y=1 - 0.10 / height, ha="left", va="top", color=INK, fontsize=11,
                    fontweight="semibold")
    entries = [f"{number}  {text} at {moment:.0f} s" for number, (moment, _, text) in enumerate(events, 1)]
    caption = "\n".join("     ".join(entries[first:first + PER_LINE]) for first in range(0, len(entries), PER_LINE))
    figure.text(0.012, 1 - 0.40 / height, caption or "Nothing was done to the system and nothing was diagnosed.",
                ha="left", va="top", color=INK_SECONDARY, fontsize=8, linespacing=1.5)
    figure.tight_layout(rect=(0, 0, 1, 1 - (0.34 + 0.16 * caption_lines) / height))
    out = Path(out) if out else run_dir / f"{run_dir.name}-metrics.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, dpi=150, facecolor=SURFACE)
    pyplot.close(figure)
    return out


def _pairs(band: Sequence) -> list[tuple[float, float]]:
    """Band entries are (low, high) pairs, except the single NaN that marks an outage."""

    return [pair if isinstance(pair, tuple) else (math.nan, math.nan) for pair in band]


def _scale(row, points: Sequence[dict]) -> None:
    """One y-range for a whole row: wide enough for every replica's data and band."""

    values = [value for point in points for value in point["value"] if not math.isnan(value)]
    bounds = [edge for point in points for pair in _pairs(point["outer"]) for edge in pair if not math.isnan(edge)]
    low, high = min(values + bounds), max(values + bounds)
    margin = 0.08 * (high - low or 1.0)
    row[0].set_ylim(max(0.0, low - margin), high + margin)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draw a recorded run against the normal signature")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_PATH,
                        help=f"Signature file (default {DEFAULT_PATH.as_posix()})")
    parser.add_argument("--metrics", nargs="+", default=list(DEFAULT_METRICS))
    parser.add_argument("--out", type=Path, help="Image file (default: <run-dir>/<run-name>-metrics.png)")
    parser.add_argument("--title")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="paper",
                        help="Detection windows used for the diagnoses that are marked")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        path = plot_run(args.run_dir, Signature.load(args.baseline), args.out, args.metrics, title=args.title,
                        profile=PROFILES[args.profile])
    except ValueError as exc:
        parser.error(str(exc))
    print(f"figure written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
