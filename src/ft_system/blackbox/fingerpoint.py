"""Stage 2 of the diagnosis: which replica is the odd one out?

The replicas of one service do the same work, so they should look alike. A
replica is blamed for a metric only when it is out of its range on that metric
and far from a peer that is not: 6 deviations apart, the same distance that
makes a sample anomalous. When the replicas have moved together, the cause is something
they share (more client requests, a busy computer), and no replica is blamed,
even if only some of them crossed their thresholds.

Choosing among several odd replicas follows the fingerpointing paper: the one
that is odd on the most metrics, and on a tie the one blamed before.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ft_system.blackbox.baseline import ANOMALY_SIGMAS
from ft_system.blackbox.detect import MetricTrack

HEALTHY, FAULT, WORKLOAD = "healthy", "fault", "workload"


@dataclass
class Verdict:
    status: str = HEALTHY
    culprit: str | None = None
    blamed: dict[str, int] = field(default_factory=dict)   # the culprit's odd metrics -> direction
    common: dict[str, int] = field(default_factory=dict)   # metrics on which all replicas moved together
    suspects: dict[str, dict[str, int]] = field(default_factory=dict)  # every replica with odd metrics


def fingerpoint(replicas: Mapping[str, Mapping[str, MetricTrack]], previous: str | None = None) -> Verdict:
    """Compare the replicas that are being watched now; ``previous`` is the last culprit."""

    common: dict[str, int] = {}
    suspects: dict[str, dict[str, int]] = {}
    for metric in sorted({metric for tracks in replicas.values() for metric in tracks}):
        having = {label: tracks[metric] for label, tracks in replicas.items() if metric in tracks}
        for direction in (1, -1):
            unmoved = [track for track in having.values() if track.ongoing != direction]
            for label, track in having.items():
                if track.ongoing != direction:
                    continue
                # Odd only if some peer has stayed behind. Peers that moved the same way but
                # stopped just short of their thresholds are close by, and do not count.
                apart = any(direction * (track.typical - peer.typical) >= ANOMALY_SIGMAS * track.spread
                            for peer in unmoved)
                if apart or len(having) == 1:
                    suspects.setdefault(label, {})[metric] = direction
                else:
                    common[metric] = direction
    if suspects:
        culprit = max(sorted(suspects), key=lambda label: (len(suspects[label]), label == previous))
        return Verdict(FAULT, culprit, suspects[culprit], common, suspects)
    if common:
        return Verdict(WORKLOAD, common=common)
    return Verdict()
