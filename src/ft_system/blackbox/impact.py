"""What a fault does to the service, read from the system's own logs.

Used to choose a fault intensity and to show a fault's effect beside the
diagnosis. The detector never reads any of this: it sees operating-system
metrics only.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

_STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\] (.*)")
_MESSAGE = re.compile(r"(Sending|Received) <(\w+), (\w+), (\d+), (?:request|reply)>$")


def timed_lines(log: str):
    """``(seconds since the epoch, text)`` for each timestamped line of a component log."""

    for line in log.splitlines():
        match = _STAMP.match(line)
        if match:
            yield datetime.fromisoformat(match.group(1)).timestamp(), match.group(2)


def count_lines(log: str, pattern: str, start: float = -math.inf, end: float = math.inf) -> int:
    """Lines matching ``pattern`` that were logged in ``[start, end)``."""

    expression = re.compile(pattern)
    return sum(1 for stamp, text in timed_lines(log) if start <= stamp < end and expression.search(text))


def reply_times(client_log: str, start: float = -math.inf,
                end: float = math.inf) -> tuple[dict[str, list[float]], dict[str, int]]:
    """Per server: milliseconds to answer each request sent in ``[start, end)``, and how many
    of those requests have no reply in the log.

    The log has millisecond timestamps, so a fast reply reads as 0 or 1 ms.
    """

    sent: dict[tuple[str, str, str], float] = {}
    times: dict[str, list[float]] = {}
    for stamp, text in timed_lines(client_log):
        match = _MESSAGE.match(text)
        if not match:
            continue
        action, client, server, number = match.groups()
        if action == "Sending":
            if start <= stamp < end:
                sent[client, server, number] = stamp
                times.setdefault(server, [])
        elif (client, server, number) in sent:
            times[server].append(float(round((stamp - sent.pop((client, server, number))) * 1000)))
    unanswered: dict[str, int] = {server: 0 for server in times}
    for _, server, _ in sent:
        unanswered[server] += 1
    return times, unanswered


def summarize(values: list[float]) -> dict[str, float] | None:
    """Median, 95th percentile and maximum; ``None`` for no data."""

    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"n": len(ordered), "median": median,
            "p95": ordered[math.ceil(0.95 * len(ordered)) - 1], "max": ordered[-1]}
