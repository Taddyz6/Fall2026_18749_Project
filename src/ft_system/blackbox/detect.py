"""Stage 1 of the diagnosis: is a replica's metric outside its own normal range?

This is the local anomaly detection of the fingerpointing paper (Pertet, Gandhi
and Narasimhan, 2007). Each sample is judged by its distance from the normal
mean, in deviations, and an excursion counts only when it lasts:

    level        one sample: 2 beyond 6 deviations, 1 beyond 3, 0 inside; signed
    logged       at least 3 of the last 7 samples are at level 2
    problematic  an anomaly was logged in more than half of the last 15 samples
    ongoing      problematic, and the recent values are still outside the normal range

The windows (7 and 15), the thresholds and the weight of the running mean are
the paper's. The running mean follows slow drift, and it stops following while
a sample is at level 2, so a fault that persists is never learned as normal.
The deviation is the trained one and does not adapt.
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from ft_system.blackbox.baseline import ANOMALY_SIGMAS, Signature, level_of, spread
from ft_system.blackbox.collector import when


@dataclass(frozen=True)
class Profile:
    anomaly_window: int = 7             # the paper's anomalyWin
    anomaly_hits: int = 3
    fingerpoint_window: int = 15        # the paper's fingerpointWin
    fingerpoint_fraction: float = 0.5
    adapt: float = 0.05                 # weight of a new sample in the running mean
    grace_seconds: float = 10.0         # a process this young is not judged at all


PAPER = Profile()
FAST = Profile(anomaly_window=5, fingerpoint_window=7)
PROFILES = {"paper": PAPER, "fast": FAST}


def sign_of(counts: Mapping[int, int], needed: float) -> int:
    """+1 or -1 if that side has reached ``needed`` (the larger side if both have), else 0."""

    up, down = counts.get(1, 0), counts.get(-1, 0)
    if max(up, down) < needed:
        return 0
    return 1 if up >= down else -1


class MetricTrack:
    """One metric of one replica: its running mean and its recent levels."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.levels: deque[int] = deque(maxlen=profile.anomaly_window)
        self.values: deque[float] = deque(maxlen=profile.anomaly_window)
        self.logged: deque[int] = deque(maxlen=profile.fingerpoint_window)  # -1, 0 or +1 per sample
        self.spread = 0.0    # one deviation of this metric
        self.mean: float | None = None
        self.phase: str | None = None
        self.value = 0.0
        self.band = (0.0, 0.0)

    def observe(self, value: float, metric: str, stats: Mapping[str, float], phase: str) -> int:
        if phase != self.phase:
            # Warm-up and steady state have different statistics: start again from the trained mean.
            self.mean, self.phase = stats["mean"], phase
        level = level_of(value, metric, stats, self.mean)
        if abs(level) < 2:
            self.mean += self.profile.adapt * (value - self.mean)
        self.spread = spread(metric, stats)
        width = ANOMALY_SIGMAS * self.spread
        self.value, self.band = value, (max(0.0, self.mean - width), self.mean + width)
        self.values.append(value)
        self.levels.append(level)
        self.logged.append(sign_of({1: sum(level == 2 for level in self.levels),
                                    -1: sum(level == -2 for level in self.levels)}, self.profile.anomaly_hits))
        return level

    @property
    def typical(self) -> float:
        """The middle of the recent values: what the metric is doing now, without the blips."""

        return statistics.median(self.values)

    @property
    def ongoing(self) -> int:
        """``problematic`` remembers 15 samples; this drops to 0 as soon as the excursion is over."""

        side = self.problematic
        return side if side * (self.typical - self.mean) >= ANOMALY_SIGMAS * self.spread else 0

    @property
    def problematic(self) -> int:
        needed = self.profile.fingerpoint_fraction * self.profile.fingerpoint_window
        counts = {1: sum(side == 1 for side in self.logged), -1: sum(side == -1 for side in self.logged)}
        # "More than half": strictly above the fraction.
        return sign_of(counts, needed + 1e-9)


class ReplicaDetector:
    """All metrics of one replica. A new incarnation starts with a clean slate."""

    def __init__(self, label: str, signature: Signature, profile: Profile = PAPER) -> None:
        self.label, self.signature, self.profile = label, signature, profile
        self.tracks: dict[str, MetricTrack] = {}
        self.incarnation: int | None = None
        self.last_seen = 0.0

    def observe(self, sample: Mapping) -> None:
        if sample.get("incarnation") != self.incarnation:
            self.tracks, self.incarnation = {}, sample.get("incarnation")
        self.last_seen = when(sample)
        age = sample.get("age")
        if age is not None and age < self.profile.grace_seconds:
            return
        phase = "warmup" if age is not None and age < self.signature.warmup else "steady"
        for metric, value in sample["m"].items():
            stats = self.signature.stats(self.label, sample["role"], metric, age)
            if stats is not None:
                self.tracks.setdefault(metric, MetricTrack(self.profile)).observe(value, metric, stats, phase)
