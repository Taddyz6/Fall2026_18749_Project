import asyncio
import json
from datetime import datetime

import pytest

from ft_system.blackbox import dashboard as dashboard_module
from ft_system.blackbox.baseline import Signature, train
from ft_system.blackbox.dashboard import CHARTS, MATRIX, Dashboard, LogWatcher, Tail, describe, play
from ft_system.blackbox.detect import PAPER
from ft_system.blackbox.diagnose import Diagnoser
from ft_system.blackbox.faults.ctl import EVENTS_FILE
from tests.blackbox.builders import in_time_order, run, run_with_fault, sample


@pytest.fixture
def signature():
    return Signature(train(run(), warmup=90.0))


@pytest.fixture
def board(signature):
    return Dashboard(signature, Diagnoser(signature, PAPER))


async def replayed(board, records, **kwargs):
    await play(in_time_order(records), board.diagnoser, board, speed=0, **kwargs)
    return board


@pytest.mark.parametrize("record, expected", [
    ({"event": "fault_injected", "server": "S2", "t": 5.0}, ("action", "fault injected into S2", "S2")),
    ({"event": "fault_cleared", "server": "S2", "t": 5.0}, ("action", "fault cleared in S2", "S2")),
    ({"event": "replica_killed", "server": "S1", "t": 5.0}, ("action", "S1 killed", "S1")),
    ({"event": "load_changed", "clients": 6, "t": 5.0}, ("action", "client count set to 6", None)),
    ({"event": "process_exited", "label": "S2", "role": "server", "t": 5.0}, ("process", "S2 exited", "S2")),
    ({"event": "process_started", "label": "S2", "role": "server", "incarnation": 2, "t": 5.0},
     ("process", "S2 relaunched", "S2")),
    ({"event": "diagnosis", "status": "fault", "headline": "S2: CPU-intensive task", "culprit": "S2", "t": 5.0},
     ("fault", "S2: CPU-intensive task", "S2")),
])
def test_describe_turns_an_event_into_a_line_for_the_page(record, expected):
    shown = describe(record)

    assert (shown["group"], shown["text"], shown["who"], shown["t"]) == (*expected, 5.0)


@pytest.mark.parametrize("record", [
    {"event": "process_started", "label": "S2", "role": "server", "incarnation": 1, "t": 5.0},   # the first start
    {"event": "process_started", "label": "C1", "role": "client", "incarnation": 2, "t": 5.0},   # not a replica
    {"event": "process_exited", "label": "LFD2", "role": "lfd", "t": 5.0},
    {"event": "agent_started", "t": 5.0},
    {"event": "recording_started", "t": 5.0},
])
def test_describe_leaves_out_events_the_page_does_not_show(record):
    assert describe(record) is None


def test_tail_returns_whole_new_lines_and_waits_for_an_unfinished_one(tmp_path):
    path = tmp_path / "log.txt"
    tail = Tail(path)
    assert tail.lines() == []                       # the file does not exist yet

    path.write_bytes(b"one\ntwo\nthr")
    assert tail.lines() == ["one", "two"]
    with path.open("ab") as handle:
        handle.write(b"ee\nfour\n")
    assert tail.lines() == ["three", "four"]
    assert tail.lines() == []


def test_log_watcher_reads_the_rm_view_and_counts_recent_heartbeat_timeouts(tmp_path):
    watcher = LogWatcher(tmp_path)
    now = datetime(2026, 11, 20, 0, 31, 30).timestamp()
    assert watcher.poll(now) is None                 # no RM log: nothing to show

    (tmp_path / "RM.log").write_text(
        "[2026-11-20 00:30:00.000] RM: view 5: members: S1, S2, S3; ready: S1, S2\n"
        "[2026-11-20 00:30:02.000] RM: asking S1 to transfer its state to S3\n"
        "[2026-11-20 00:30:02.100] RM: view 6: members: S1, S2, S3; ready: S1, S2, S3\n", encoding="utf-8")
    (tmp_path / "LFD2.log").write_text(
        "[2026-11-20 00:29:00.000] [10] LFD2 heartbeat to S2 failed: timeout\n"       # older than a minute
        "[2026-11-20 00:31:10.000] [90] LFD2 heartbeat to S2 failed: timeout\n"
        "[2026-11-20 00:31:11.000] [91] LFD2 sending heartbeat to S2\n", encoding="utf-8")
    assert watcher.poll(now) == {"members": ["S1", "S2", "S3"], "ready": ["S1", "S2", "S3"], "timeouts": 1}

    with (tmp_path / "RM.log").open("a", encoding="utf-8") as handle:
        handle.write("[2026-11-20 00:31:40.000] RM: view 7: members: S1, S3; ready: S1, S3\n")
    later = watcher.poll(now + 60)
    assert later == {"members": ["S1", "S3"], "ready": ["S1", "S3"], "timeouts": 0}


async def test_frames_carry_values_levels_and_the_diagnosis(board):
    await replayed(board, run_with_fault())
    by_time = {frame["t"]: frame for frame in board.frames}
    before, during, after = by_time[1090.0], by_time[1120.0], by_time[1160.0]

    assert len(board.frames) == 195 and sorted(during["servers"]) == ["S1", "S2", "S3"]
    assert during["servers"]["S2"]["v"] == {"cpu": 75.0, "rss_mb": 27.0, "threads": 4}
    assert (during["servers"]["S2"]["level"]["cpu"], during["servers"]["S1"]["level"]["cpu"]) == (2, 0)
    assert (before["status"]["state"], during["status"]["state"], after["status"]["state"]) == (
        "healthy", "fault", "healthy")
    assert during["status"]["headline"] == "S2: CPU-intensive task inside the process"
    assert during["status"]["culprit"] == "S2" and "kill it" in during["status"]["action"]
    assert (during["status"]["since"], after["status"]["since"]) == (1109.0, 1147.0)
    assert during["host"] == {"cpu": 5.0, "agent_cpu": None} and during["ft"] is None
    assert [(event["t"], event["group"]) for event in board.events] == [
        (1100.0, "action"), (1109.0, "fault"), (1140.0, "action"), (1147.0, "healthy")]


async def test_replay_can_stop_part_way_and_skips_recorded_diagnoses(board):
    stale = {"event": "diagnosis", "status": "fault", "headline": "old", "detail": "", "culprit": "S9", "t": 1050.0}

    await replayed(board, run_with_fault() + [stale], until=120.0)

    assert board.frames[-1]["t"] == 1125.0            # the first sample is at 1005
    assert [event["text"] for event in board.events] == [
        "fault injected into S2", "S2: CPU-intensive task inside the process"]


async def test_a_silent_or_newborn_replica_shows_as_a_gap(board):
    records = [record for record in run() if not (record.get("label") == "S3" and record.get("age", 0) >= 100)]
    records += [sample("S3", 3.0, cpu=1.0, rss_mb=27.0, threads=6) | {"recv": 1149.5, "incarnation": 2}]

    await replayed(board, records)
    by_time = {frame["t"]: frame for frame in board.frames}

    assert sorted(by_time[1101.0]["servers"]) == ["S1", "S2", "S3"]      # heard from 2 s ago: still shown
    assert sorted(by_time[1110.0]["servers"]) == ["S1", "S2"]
    newborn = by_time[1150.0]["servers"]["S3"]
    assert (newborn["incarnation"], newborn["age"], newborn["level"]) == (2, 3.0, {})   # shown, not yet judged


def test_things_done_to_the_system_are_read_from_the_events_file(tmp_path, signature):
    board = Dashboard(signature, Diagnoser(signature, PAPER), run_dir=tmp_path)
    host = {"type": "sample", "label": "host:pc", "role": "host", "recv": 1200.0, "m": {"cpu": 5.0, "agent_cpu": 1.0}}
    with (tmp_path / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t": 1199.5, "event": "fault_injected", "server": "S2"}) + "\n")
        handle.write(json.dumps({"t": 1199.6, "event": "process_started", "label": "S2", "role": "server",
                                 "incarnation": 2}) + "\n")     # arrives through the collector, not from here
        handle.write("not json\n")

    board.observe(host)
    board.observe(host | {"recv": 1201.0})

    assert [event["text"] for event in board.events] == ["fault injected into S2"]
    assert board.frames[-1]["host"] == {"cpu": 5.0, "agent_cpu": 1.0}


def test_snapshot_describes_the_charts_and_their_normal_ranges(board):
    snapshot = board.snapshot()

    assert [chart["key"] for chart in snapshot["charts"]] == list(CHARTS)
    assert [row["key"] for row in snapshot["matrix"]] == list(MATRIX)
    cpu, missing = snapshot["charts"][0], snapshot["charts"][1]
    assert cpu["name"] == "CPU (% of one core)"
    assert cpu["band"] == {"low3": 0.0, "high3": pytest.approx(8.0, abs=0.1), "low6": 0.0,
                           "high6": pytest.approx(14.0, abs=0.1)}
    assert missing["band"] is None                                 # the synthetic run has no context switches
    assert snapshot["trained"] == {"host": "pc", "config": "m5.toml", "client_interval": 0.2}
    assert (snapshot["actions"], snapshot["controls"], snapshot["frames"], snapshot["events"]) == ([], {}, [], [])


async def ask(port, text, body=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(text.encode() + b"\r\n\r\n" + body)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), payload


async def test_page_snapshot_and_actions_are_served_over_http(signature):
    pressed = []

    def refuse(argument):
        raise RuntimeError("S2 is not running")

    controls = {"buttons": [{"label": "Inject fault", "action": "inject", "target": True}], "targets": ["S1", "S2"],
                "default": "S2"}
    board = Dashboard(signature, Diagnoser(signature, PAPER), controls=controls,
                      actions={"inject": lambda argument: pressed.append(argument) or "started", "kill": refuse})
    server = await board.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        status, page = await ask(port, "GET / HTTP/1.1\r\nHost: x")
        assert status == 200 and b"<title>Black-box diagnosis</title>" in page
        assert b"http://" not in page and b"https://" not in page          # nothing is fetched from elsewhere

        status, payload = await ask(port, "GET /snapshot HTTP/1.1")
        assert status == 200 and json.loads(payload)["actions"] == ["inject", "kill"]
        assert json.loads(payload)["controls"] == controls

        assert (await ask(port, "GET /elsewhere HTTP/1.1"))[0] == 404
        assert (await ask(port, "POST /action/inject HTTP/1.1\r\nContent-Length: 2", b"S2"))[0] == 403
        press = "POST /action/{} HTTP/1.1\r\nX-Dashboard: 1\r\nContent-Length: 2"
        assert await ask(port, press.format("inject"), b"S2") == (200, b"started")
        assert await ask(port, press.format("kill"), b"S2") == (409, b"S2 is not running")
        assert (await ask(port, press.format("missing"), b"S2"))[0] == 404
        assert pressed == ["S2"]
    finally:
        server.close()
        await server.wait_closed()


async def test_stream_sends_each_new_frame_and_event(board):
    server = await board.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /stream HTTP/1.1\r\n\r\n")
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert b"200 OK" in head and b"text/event-stream" in head
        await asyncio.sleep(0.05)                      # let the server register this listener

        board.observe({"t": 1200.0, "event": "replica_killed", "server": "S1"})
        board.observe({"type": "sample", "label": "host:pc", "role": "host", "recv": 1201.0, "m": {"cpu": 5.0}})
        first = json.loads((await asyncio.wait_for(reader.readuntil(b"\n\n"), 5)).removeprefix(b"data: "))
        second = json.loads((await asyncio.wait_for(reader.readuntil(b"\n\n"), 5)).removeprefix(b"data: "))
        writer.close()
    finally:
        server.close()
        await server.wait_closed()

    assert (first["type"], first["event"]["text"]) == ("event", "S1 killed")
    assert (second["type"], second["frame"]["t"], second["frame"]["status"]["state"]) == ("frame", 1201.0, "healthy")


def test_command_line_needs_a_recording():
    with pytest.raises(SystemExit):
        dashboard_module.main([])
