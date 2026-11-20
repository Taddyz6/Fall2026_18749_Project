"""Score recorded evaluation runs: what was detected, how fast, and what was blamed wrongly.

Everything here works on the records of a run (``collector.read_run``) and on the
diagnoses a replay of it gives (``diagnose.replay``), so the same recording can be
scored with different detection windows. Standard library only.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping, Sequence

from ft_system.blackbox import impact
from ft_system.blackbox.collector import when
from ft_system.blackbox.diagnose import LINGER_SECONDS, fault_intervals
from ft_system.blackbox.fingerpoint import FAULT, HEALTHY, WORKLOAD

_VIEW = re.compile(r"RM: view \d+: members: (.*?); ready: (.*)")


def blamed_spans(diagnoses: Sequence[Mapping], end: float) -> list[tuple[float, float, str]]:
    """``(from, to, replica)`` for every stretch in which a replica stood diagnosed."""

    spans = []
    for index, diagnosis in enumerate(diagnoses):
        if diagnosis["status"] == FAULT:
            until = diagnoses[index + 1]["t"] if index + 1 < len(diagnoses) else end
            spans.append((diagnosis["t"], until, diagnosis["culprit"]))
    return spans


def fault_trials(records: Sequence[Mapping], diagnoses: Sequence[Mapping]) -> dict:
    """One row per injected fault, and every diagnosis that blamed a replica without one.

    A fault is detected by the first diagnosis that blames its replica while it runs, or
    within a few seconds after. ``covered`` is the fraction of the fault's duration during
    which its replica stood diagnosed.
    """

    faults = fault_intervals(records)
    end = max((when(record) for record in records), default=0.0)
    spans = blamed_spans(diagnoses, end)
    false_alarms = []
    for diagnosis in diagnoses:
        if diagnosis["status"] != FAULT:
            continue
        match = next((fault for fault in faults if fault["server"] == diagnosis["culprit"]
                      and fault["start"] <= diagnosis["t"] <= fault["end"] + LINGER_SECONDS), None)
        if match is None:
            false_alarms.append(diagnosis)
        elif "delay" not in match:
            match["delay"], match["cause"] = diagnosis["t"] - match["start"], diagnosis["cause"]
    for fault in faults:
        fault["duty"] = (fault.get("fault") or {}).get("duty")
        blamed = sum(max(0.0, min(until, fault["end"]) - max(start, fault["start"]))
                     for start, until, replica in spans if replica == fault["server"])
        fault["covered"] = blamed / (fault["end"] - fault["start"]) if fault["end"] > fault["start"] else 0.0
    return {"faults": faults, "false_alarms": false_alarms}


def accuracy(result: Mapping) -> dict:
    """Precision, recall and F-score over faults, as the fingerpointing paper reports them.

    A true positive is a fault whose replica was blamed; a false positive is a blame with
    no fault behind it; a false negative is a fault nobody was blamed for.
    """

    detected = sum("delay" in fault for fault in result["faults"])
    missed = len(result["faults"]) - detected
    wrong = len(result["false_alarms"])
    precision = detected / (detected + wrong) if detected + wrong else None
    recall = detected / (detected + missed) if detected + missed else None
    score = (2 * precision * recall / (precision + recall)) if precision and recall else None
    return {"faults": len(result["faults"]), "detected": detected, "missed": missed, "false_alarms": wrong,
            "precision": precision, "recall": recall, "f_score": score}


def by_intensity(faults: Sequence[Mapping]) -> list[dict]:
    """Per fault intensity: how many were detected, how fast, and with which cause."""

    rows = []
    for duty in sorted({fault["duty"] for fault in faults}, key=lambda value: (value is None, value)):
        group = [fault for fault in faults if fault["duty"] == duty]
        delays = sorted(fault["delay"] for fault in group if "delay" in fault)
        rows.append({
            "duty": duty, "faults": len(group), "detected": len(delays),
            "delay_median": statistics.median(delays) if delays else None,
            "delay_max": delays[-1] if delays else None,
            "cpu_task": sum(fault.get("cause") == "cpu_task" for fault in group),
            "covered": statistics.fmean(fault["covered"] for fault in group)})
    return rows


def quiet_summary(records: Sequence[Mapping], diagnoses: Sequence[Mapping], base_clients: int = 3) -> dict:
    """A run with no injected fault: any blame is a false alarm; load changes should read as workload."""

    stamps = [when(record) for record in records if record.get("type") == "sample"]
    hours = (max(stamps) - min(stamps)) / 3600 if stamps else 0.0
    blames = [diagnosis for diagnosis in diagnoses if diagnosis["status"] == FAULT]
    raised = [when(record) for record in records
              if record.get("event") == "load_changed" and record.get("clients", 0) > base_clients]
    reported = []
    for moment in raised:
        seen = next((diagnosis["t"] - moment for diagnosis in diagnoses
                     if diagnosis["status"] == WORKLOAD and moment <= diagnosis["t"] <= moment + 60), None)
        reported.append(seen)
    return {"minutes": hours * 60, "false_alarms": len(blames),
            "false_alarms_per_hour": len(blames) / hours if hours else None,
            "kills": sum(record.get("event") == "replica_killed" for record in records),
            "load_raised": len(raised), "load_reported_after": reported,
            "healthy_reports": sum(diagnosis["status"] == HEALTHY for diagnosis in diagnoses)}


def ready_times(rm_log: str, servers: Sequence[str]) -> list[tuple[float, set[str]]]:
    """``(time, ready replicas)`` for every view the RM logged."""

    views = []
    for stamp, text in impact.timed_lines(rm_log):
        match = _VIEW.search(text)
        if match:
            views.append((stamp, {name.strip() for name in match.group(2).split(",")} & set(servers)))
    return views


def recovery_trials(records: Sequence[Mapping], diagnoses: Sequence[Mapping], rm_log: str,
                    servers: Sequence[str]) -> list[dict]:
    """The full loop for each injected fault: diagnosed, restarted, and all replicas ready again.

    Times are seconds after the injection; a step that did not happen is ``None``.
    """

    views = ready_times(rm_log, servers)
    kills = [record for record in records if record.get("event") == "replica_killed"]
    rows = []
    for fault in fault_intervals(records):
        server, start = fault["server"], fault["start"]
        diagnosed = next((diagnosis["t"] for diagnosis in diagnoses if diagnosis["status"] == FAULT
                          and diagnosis["culprit"] == server and diagnosis["t"] >= start), None)
        killed = next((when(kill) for kill in kills if kill.get("server") == server and when(kill) >= start), None)
        ready, lost = None, False
        for stamp, ready_set in views:
            if killed is None or stamp < killed:
                continue
            if server not in ready_set:
                lost = True
            elif lost and ready_set == set(servers):
                ready = stamp
                break
        normal = next((diagnosis["t"] for diagnosis in diagnoses if diagnosis["status"] == HEALTHY
                       and killed is not None and diagnosis["t"] >= killed), None)
        rows.append({"server": server, "duty": (fault.get("fault") or {}).get("duty"), "start": start,
                     **{name: (moment - start if moment is not None else None) for name, moment in (
                         ("diagnosed", diagnosed), ("restarted", killed), ("ready", ready), ("normal", normal))}})
    return rows


def heartbeats(lfd_log: str, server: str, start: float, end: float) -> tuple[int, int]:
    """How many heartbeats an LFD sent to its server in a time window, and how many were answered."""

    return (impact.count_lines(lfd_log, rf"sending heartbeat to {server}$", start, end),
            impact.count_lines(lfd_log, rf"receives heartbeat from {server}$", start, end))


def median_of(rows: Sequence[Mapping], name: str) -> float | None:
    values = [row[name] for row in rows if row.get(name) is not None]
    return statistics.median(values) if values else None
