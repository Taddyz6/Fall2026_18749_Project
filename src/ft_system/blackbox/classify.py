"""Stage 3 of the diagnosis: what kind of problem does the culprit's evidence point to?

A small table of signatures over the metrics on which the culprit is the odd
one out. The server writes its log as it handles requests, so its write rate
follows the work clients give it; that is what separates a task burning CPU
inside the process from a replica that is simply being asked to do more.
"""

from __future__ import annotations

from collections.abc import Mapping

CPU = ("cpu", "cpu_user", "cpu_sys")
WORK = "io_write_count_s"

CAUSES = {
    "cpu_task": ("CPU-intensive task inside the process",
                 "CPU rose but the write rate, which follows request work, did not",
                 "restart the replica: kill it and the RM relaunches it"),
    "extra_work": ("more request work than its peers",
                   "CPU and the write rate rose together on this replica only",
                   "check how clients spread their requests"),
    "memory_growth": ("memory growth",
                      "memory is above its normal range while CPU is not",
                      "restart the replica before it runs out of memory"),
    "unclassified": ("unrecognised deviation", "no known pattern matches", "inspect the replica"),
}


def classify(blamed: Mapping[str, int]) -> str:
    """Name of the cause in ``CAUSES`` for a culprit that is odd on ``blamed`` (metric -> direction)."""

    cpu_up = any(blamed.get(metric) == 1 for metric in CPU)
    if cpu_up:
        return "extra_work" if blamed.get(WORK) == 1 else "cpu_task"
    if blamed.get("rss_mb") == 1:
        return "memory_growth"
    return "unclassified"
