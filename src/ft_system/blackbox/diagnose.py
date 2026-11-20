"""The diagnosis as it happens: feed it records, it reports each change of verdict.

    python -m ft_system.blackbox.diagnose --run-dir logs/ec/some-run    # replay a recording

Live, the collector feeds one of these with the records it receives
(``collector --baseline ...``). Replaying a recorded run gives the same
verdicts, which makes tuning repeatable without starting the system.

Three stages: ``detect`` (is a replica's metric outside its own normal range),
``fingerpoint`` (which replica is the odd one out) and ``classify`` (what kind
of problem). A verdict is reported when it changes, and withdrawn only after
several healthy seconds in a row. Standard library only.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path

from ft_system.blackbox.baseline import DEFAULT_PATH, Signature, describe_run
from ft_system.blackbox.classify import CAUSES, WORK, classify
from ft_system.blackbox.collector import read_run, when
from ft_system.blackbox.detect import PAPER, PROFILES, MetricTrack, Profile, ReplicaDetector
from ft_system.blackbox.fingerpoint import FAULT, HEALTHY, WORKLOAD, Verdict, fingerpoint

STALE_SECONDS = 3.0   # a replica silent for this long is not compared with the others
CLEAR_TICKS = 5       # healthy verdicts in a row before a diagnosis is withdrawn
LINGER_SECONDS = 15.0  # a diagnosis this soon after a fault ended still belongs to that fault


WORDS = {"cpu": "CPU", "cpu_user": "user CPU", "cpu_sys": "system CPU", "rss_mb": "memory",
         "threads": "threads", "ctx_voluntary_s": "context switches", "ctx_involuntary_s": "forced context switches",
         "io_write_count_s": "write rate", "io_read_count_s": "read rate", "io_other_count_s": "other I/O rate",
         "io_write_bytes_s": "bytes written", "io_read_bytes_s": "bytes read", "io_other_bytes_s": "other I/O bytes"}


def words(metric: str) -> str:
    return WORDS.get(metric, metric)


def number(value: float) -> str:
    """Three significant digits, without exponents: 73.4, 124, 12,400."""

    return format(float(f"{value:.3g}"), ",g")


def reading(metric: str, track: MetricTrack) -> str:
    unit = "%" if metric.startswith("cpu") else "/s" if metric.endswith("_s") else " MB" if metric.endswith("_mb") else ""
    return (f"{words(metric)} {number(track.value)}{unit} "
            f"(normal {number(track.band[0])} to {number(track.band[1])}{unit})")


def and_join(names: Sequence[str]) -> str:
    return " and ".join(filter(None, (", ".join(names[:-1]), names[-1])))


class Diagnoser:
    def __init__(self, signature: Signature, profile: Profile = PAPER, role: str = "server") -> None:
        self.signature, self.profile, self.role = signature, profile, role
        self.replicas: dict[str, ReplicaDetector] = {}
        self.verdict = Verdict()                                  # the latest, reported or not
        self.reported: tuple[str, str | None, str | None] = (HEALTHY, None, None)
        self._last_culprit: str | None = None
        self._evidence: dict[str, int] = {}   # every metric the current culprit has been odd on
        self._quiet = 0

    def observe(self, record: Mapping) -> list[dict]:
        """Take one record; return the diagnosis events it leads to (usually none)."""

        if record.get("type") != "sample":
            return []
        if record.get("role") == self.role:
            label = record["label"]
            if label not in self.replicas:
                self.replicas[label] = ReplicaDetector(label, self.signature, self.profile)
            self.replicas[label].observe(record)
            return []
        # An agent sends its host sample last, so every replica it watches is up to date.
        return self._evaluate(when(record)) if record.get("role") == "host" else []

    def _evaluate(self, now: float) -> list[dict]:
        watched = {label: detector.tracks for label, detector in self.replicas.items()
                   if now - detector.last_seen <= STALE_SECONDS}
        self.verdict = verdict = fingerpoint(watched, self._last_culprit)
        if verdict.status == HEALTHY:
            self._quiet += 1
            if self.reported[0] == HEALTHY or self._quiet < CLEAR_TICKS:
                return []
            self.reported = (HEALTHY, None, None)
            return [self._event(now, HEALTHY, "Normal", "every replica is back inside its normal range")]
        self._quiet = 0
        cause = None
        if verdict.status == FAULT:
            if self.reported[:2] != (FAULT, verdict.culprit):
                self._evidence = {}
            elif verdict.blamed.keys() <= self._evidence.keys():
                # The same culprit with no new evidence. As a fault ends its metrics return
                # to normal one by one; that is not a different problem.
                return []
            self._evidence |= verdict.blamed
            cause = classify(self._evidence)
        if (verdict.status, verdict.culprit, cause) == self.reported:
            return []
        self.reported = (verdict.status, verdict.culprit, cause)
        if verdict.status == WORKLOAD:
            return [self._event(
                now, WORKLOAD, "Workload change: no replica is blamed",
                f"all {len(watched)} replicas moved together on "
                f"{and_join(sorted(words(metric) for metric in verdict.common))}: "
                "a change in workload or on the computer; no replica is blamed",
                common=verdict.common)]
        self._last_culprit = culprit = verdict.culprit
        name, reason, action = CAUSES[cause]
        tracks = watched[culprit]
        primary = next((metric for metric in ("cpu", "rss_mb") if metric in verdict.blamed),
                       sorted(verdict.blamed)[0])
        details = [reading(primary, tracks[primary])]
        if cause == "cpu_task" and WORK in tracks:
            details.append(reading(WORK, tracks[WORK]))
        others = sorted(label for label in verdict.suspects if label != culprit)
        peers = sorted(label for label in watched if label != culprit and label not in others)
        if others:
            details.append(f"also odd: {and_join(others)}")
        if peers:
            details.append(f"{and_join(peers)} normal")
        evidence = [{"metric": metric, "direction": direction, "value": tracks[metric].value,
                     "low": round(tracks[metric].band[0], 2), "high": round(tracks[metric].band[1], 2)}
                    for metric, direction in sorted(verdict.blamed.items())]
        return [self._event(now, FAULT, f"{culprit}: {name}", f"{reason}: {'; '.join(details)}",
                            culprit=culprit, cause=cause, evidence=evidence, action=action,
                            common=verdict.common)]

    @staticmethod
    def _event(now: float, status: str, headline: str, detail: str, **fields) -> dict:
        """``summary`` is the one-line form for logs; a fault's reads "headline. detail"."""

        summary = f"{headline}. {detail}" if status == FAULT else detail
        return {"type": "event", "event": "diagnosis", "t": round(now, 3), "status": status,
                "headline": headline, "detail": detail, "summary": summary, **fields}


def replay(records: Sequence[Mapping], signature: Signature, profile: Profile = PAPER) -> list[dict]:
    """Every diagnosis a recorded run leads to."""

    diagnoser = Diagnoser(signature, profile)
    return [event for record in records for event in diagnoser.observe(record)]


def fault_intervals(records: Sequence[Mapping]) -> list[dict]:
    """Ground truth: when each injected fault started and ended (cleared, killed, or end of run)."""

    faults, running = [], {}
    end = max((when(record) for record in records), default=0.0)
    for record in records:
        kind, server = record.get("event"), record.get("server")
        if kind == "fault_injected":
            running[server] = {"server": server, "start": when(record), "end": end, "fault": record.get("fault")}
            faults.append(running[server])
        elif kind in ("fault_cleared", "replica_killed") and server in running:
            running.pop(server)["end"] = when(record)
    return faults


def score(records: Sequence[Mapping], diagnoses: Sequence[Mapping]) -> dict:
    """Compare the diagnoses with the injected faults.

    A fault is detected by the first diagnosis that blames its replica while it
    runs (or within a few seconds after). Any other blame is a false alarm.
    """

    faults = fault_intervals(records)
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
    return {"faults": faults, "detected": [fault for fault in faults if "delay" in fault],
            "missed": [fault for fault in faults if "delay" not in fault], "false_alarms": false_alarms}


DONE = {"fault_injected": "fault injected into {server}", "fault_cleared": "fault cleared in {server}",
        "replica_killed": "{server} killed", "load_changed": "client load changed to {clients} clients"}


def format_timeline(records: Sequence[Mapping], diagnoses: Sequence[Mapping]) -> str:
    """What was done to the system and what was diagnosed, in time order."""

    start = next((when(record) for record in records if record.get("event") == "recording_started"),
                 when(records[0]) if records else 0.0)
    lines = [(when(record), "done", DONE[record["event"]].format(**record))
             for record in records if record.get("event") in DONE]
    lines += [(diagnosis["t"], "DIAG", diagnosis["summary"]) for diagnosis in diagnoses]
    return "\n".join(f"+{moment - start:6.1f} s  {kind}  {text}" for moment, kind, text in sorted(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay a recorded run through the diagnosis")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_PATH,
                        help=f"Signature file (default {DEFAULT_PATH.as_posix()})")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="paper",
                        help="Detection windows: the paper's 7 and 15 samples, or the faster 5 and 7")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    signature, records = Signature.load(args.baseline), read_run(args.run_dir)
    for difference in signature.mismatches(describe_run(records)):
        print(f"warning: {difference}")
    diagnoses = replay(records, signature, PROFILES[args.profile])
    print(format_timeline(records, diagnoses) or "nothing was done and nothing was diagnosed")
    result = score(records, diagnoses)
    for fault in result["detected"]:
        print(f"detected: the fault in {fault['server']} after {fault['delay']:.1f} s, as {fault['cause']}")
    for fault in result["missed"]:
        print(f"missed: the fault in {fault['server']}")
    print(f"false alarms: {len(result['false_alarms'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
