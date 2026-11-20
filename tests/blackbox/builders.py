"""Synthetic recorded runs for the signature, detection and report tests."""

import json

from ft_system.blackbox.collector import Store, when
from ft_system.blackbox.faults.ctl import EVENTS_FILE


def sample(label, age, role="server", **metrics):
    return {"type": "sample", "label": label, "role": role, "age": age, "recv": 1000.0 + age, "m": metrics}


def run(seconds=200, servers=("S1", "S2", "S3"), cpu=lambda second: 1.0 + second % 3, threads_after=4):
    """A fault-free run: CPU cycles through 1, 2, 3; a server has 6 threads for its first minute."""

    records = [
        {"event": "agent_started", "host": "pc", "os": "TestOS", "python": "3.12", "cores": 8, "interval": 1.0},
        {"event": "recording_started", "config": "m5.toml", "mode": "active", "client_interval": 0.2,
         "seconds": seconds},
    ]
    for second in range(5, seconds):
        for label in servers:
            records.append(sample(label, float(second), cpu=cpu(second), rss_mb=27.0,
                                  threads=6 if second < 60 else threads_after))
        records.append(sample("LFD1", float(second), role="lfd", cpu=0.0))
        records.append({"type": "sample", "label": "host:pc", "role": "host", "recv": 1000.0 + second,
                        "m": {"cpu": 5.0}})
    return records


def write_run(folder, records):
    """Store ``records`` the way a recording does: samples.jsonl and events.jsonl in ``folder``."""

    folder.mkdir()
    store = Store(folder)
    for record in records:
        if "event" not in record:
            store.add(record)
    store.close()
    with (folder / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        for record in records:
            if "event" in record:
                handle.write(json.dumps({"t": 999.0} | record) + "\n")
    return folder


def run_with_fault(fault=(100, 140), hot="S2", seconds=200, **kwargs):
    """A run in which ``hot`` burns CPU between the two seconds; every tick ends with a host sample."""

    records = run(seconds=seconds, **kwargs)
    for record in records:
        if record.get("label") == hot and fault[0] <= record.get("age", 0) < fault[1]:
            record["m"]["cpu"] = 75.0
    return records + [
        {"t": 1000.0 + fault[0], "event": "fault_injected", "server": hot, "fault": {"kind": "cpu"}},
        {"t": 1000.0 + fault[1], "event": "fault_cleared", "server": hot}]


def in_time_order(records):
    return sorted(records, key=when)
