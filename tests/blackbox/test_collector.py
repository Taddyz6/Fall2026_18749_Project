import asyncio
import io
import json
import time

from ft_system.blackbox import collector
from ft_system.blackbox.baseline import Signature, train
from ft_system.blackbox.collector import SAMPLES_FILE, Store
from ft_system.blackbox.faults.ctl import EVENTS_FILE
from ft_system.common.logging import EventLogger
from tests.blackbox.builders import in_time_order, run, run_with_fault
from tests.helpers import eventually


def sample(seq, label="S2", cpu=1.0, recv=None, **extra):
    record = {"type": "sample", "agent": "pc/1", "seq": seq, "t": 100.0 + seq, "host": "pc",
              "label": label, "role": "server", "pid": 42, "incarnation": 1, "m": {"cpu": cpu}, **extra}
    if recv is not None:
        record["recv"] = recv
    return record


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_store_keeps_samples_and_events_in_their_files(tmp_path):
    store, heard = Store(tmp_path), []
    store.subscribe(heard.append)
    started = {"type": "event", "agent": "pc/1", "seq": 2, "event": "process_started", "label": "S2"}
    injected = {"t": 5.0, "event": "fault_injected", "server": "S2"}  # as the fault controller writes it

    assert store.add(sample(1, cpu=3.0, recv=10.0))
    assert store.add(started)
    assert store.add(injected)
    store.close()

    assert lines(tmp_path / SAMPLES_FILE) == [sample(1, cpu=3.0, recv=10.0)]
    assert lines(tmp_path / EVENTS_FILE) == [started, injected | {"type": "event"}]
    assert store.series("S2", "cpu") == [(10.0, 3.0)]
    assert store.events == [started, injected | {"type": "event"}]
    assert len(heard) == 3


def test_store_drops_a_batch_it_already_has_and_rejects_malformed_records():
    store = Store()

    assert store.add(sample(1)) and store.add(sample(2))
    assert not store.add(sample(1)) and not store.add(sample(2))   # resent after a failed send
    assert store.add(sample(1) | {"agent": "pc/2"})                # another agent, or this one restarted
    for bad in (None, [], "text", {}, {"type": "sample"}, {"type": "sample", "label": "S2"},
                {"type": "sample", "label": "S2", "m": 3}, {"event": 7}):
        assert not store.add(bad)

    assert (store.duplicates, store.rejected) == (2, 8)
    assert len(store.recent["S2"]) == 3


def test_store_keeps_only_the_newest_samples_in_memory():
    store = Store(window=3)
    for seq in range(1, 6):
        store.add(sample(seq, cpu=float(seq), recv=float(seq)))

    assert store.series("S2", "cpu") == [(3.0, 3.0), (4.0, 4.0), (5.0, 5.0)]
    assert store.series("S2", "threads") == [] and store.series("S9", "cpu") == []


def test_recorded_run_reads_back_in_time_order_and_replays(tmp_path):
    store = Store(tmp_path)
    store.add(sample(1, recv=10.0))
    store.add(sample(2, recv=12.0))
    store.close()
    with (tmp_path / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t": 11.0, "event": "fault_injected", "server": "S2"}) + "\n")
        handle.write("not json\n")

    records = collector.read_run(tmp_path)
    replayed = Store()
    collector.replay(tmp_path, replayed)

    assert [collector.when(record) for record in records] == [10.0, 11.0, 12.0]
    assert replayed.series("S2", "cpu") == [(10.0, 1.0), (12.0, 1.0)]
    assert [event["event"] for event in replayed.events] == ["fault_injected"]


def test_summary_counts_samples_incarnations_and_cpu(tmp_path, capsys):
    store = Store(tmp_path)
    store.add(sample(1, cpu=10.0, recv=10.0))
    store.add(sample(2, cpu=30.0, recv=11.0, incarnation=2))
    store.add({"type": "sample", "agent": "pc/1", "seq": 3, "label": "host:pc", "role": "host",
               "recv": 11.0, "m": {"cpu": 50.0, "agent_cpu": 1.5}})
    store.close()

    summary = collector.summarize(collector.read_run(tmp_path))
    assert collector.main(["--run-dir", str(tmp_path), "--summary"]) == 0
    printed = capsys.readouterr().out

    assert summary["S2"] == {"role": "server", "samples": 2, "first": 10.0, "last": 11.0,
                             "incarnations": [1, 2], "cpu_max": 30.0, "cpu_mean": 20.0}
    assert summary["host:pc"]["cpu_mean"] == 1.5      # for the host row: the agent's own cost
    assert printed.splitlines()[1].split()[:5] == ["S2", "server", "2", "1.0", "1,2"]


async def test_server_stamps_arrival_time_and_survives_bad_input(tmp_path):
    store = Store(tmp_path)
    server = await collector.start(store, "127.0.0.1", 0, EventLogger(io.StringIO()))
    port = server.sockets[0].getsockname()[1]
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = [json.dumps(sample(1)), "garbage", json.dumps(sample(2)), json.dumps(sample(2)), "[1, 2]"]
        writer.write(("\n".join(payload) + "\n").encode())
        await writer.drain()
        await eventually(lambda: len(store.recent.get("S2", ())) == 2 and store.rejected == 2, timeout=5)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        store.close()

    stored = lines(tmp_path / SAMPLES_FILE)
    assert [record["seq"] for record in stored] == [1, 2]
    assert all(abs(record["recv"] - time.time()) < 30 for record in stored)
    assert (store.duplicates, store.rejected) == (1, 2)


def test_collector_diagnoses_as_records_arrive_when_given_a_signature(tmp_path):
    Signature(train(run(), warmup=90.0)).save(tmp_path / "baseline.json")
    (tmp_path / "live").mkdir()
    store, output = Store(tmp_path / "live"), io.StringIO()
    collector.attach_diagnosis(store, tmp_path / "baseline.json", "paper", EventLogger(output))

    store.add({"event": "agent_started", "host": "another-pc", "os": "TestOS", "cores": 8})
    for record in in_time_order(run_with_fault()):
        if record.get("event") != "agent_started":
            store.add(record)
    store.close()

    stored = [event for event in lines(tmp_path / "live" / EVENTS_FILE) if event["event"] == "diagnosis"]
    assert [(event["status"], event.get("culprit")) for event in stored] == [("fault", "S2"), ("healthy", None)]
    assert [event["event"] for event in store.events if event["event"] == "diagnosis"] == ["diagnosis"] * 2
    printed = output.getvalue()
    assert "DIAG: S2: CPU-intensive task inside the process" in printed
    assert "DIAG: every replica is back inside its normal range" in printed
    assert "the signature may not apply here: host: trained on 'pc', this run has 'another-pc'" in printed
    assert "cores" not in printed.split("the signature may not apply here: ")[1].splitlines()[0]
