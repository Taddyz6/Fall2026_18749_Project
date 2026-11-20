import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("psutil")

from ft_system.blackbox import agent as agent_module  # noqa: E402
from ft_system.blackbox.agent import Agent, Sender, rates_between  # noqa: E402
from tests.helpers import free_ports  # noqa: E402

IDLE = "import time\ntime.sleep(60)"
BUSY = "import time\nend = time.time() + 60\nwhile time.time() < end: pass"


@pytest.fixture
def stand_in():
    """Start a process whose command line reads like one of the system's components."""

    started = []

    def start(role, option, label, code=IDLE):
        process = subprocess.Popen([sys.executable, "-c", code, f"ft_system.{role}.main", option, label])
        started.append(process)
        return process

    yield start
    for process in started:
        process.kill()
        process.wait()


def observe(agent, label, ticks=4, pause=0.5):
    """Tick the agent a few times; return the events and samples that mention ``label``."""

    events, samples = [], []
    for _ in range(ticks):
        time.sleep(pause)
        for record in agent.tick():
            if record.get("label") == label:
                (events if record["type"] == "event" else samples).append(record)
    return events, samples


def test_rates_are_per_second_and_cpu_is_percent_of_one_core():
    before = {"cpu_user": 1.0, "cpu_sys": 0.5, "io_write_count": 100, "ctx_voluntary": 40}
    after = {"cpu_user": 2.5, "cpu_sys": 0.6, "io_write_count": 350, "ctx_voluntary": 100}

    assert rates_between(before, after, 2.0) == {
        "cpu_user": 75.0, "cpu_sys": 5.0, "io_write_count_s": 125.0, "ctx_voluntary_s": 30.0}


def test_a_counter_that_went_backwards_is_left_out_of_the_sample():
    before = {"cpu_user": 1.0, "ctx_voluntary": 4000}
    after = {"cpu_user": 1.5, "ctx_voluntary": 900}  # a thread exited and took its count with it

    assert rates_between(before, after, 1.0) == {"cpu_user": 50.0}


def test_readings_that_cannot_be_compared_give_no_rates():
    assert rates_between({"cpu_user": 1.0}, {"cpu_user": 2.0}, 0.0) is None
    assert rates_between({"cpu_user": 1.0}, {"cpu_user": 2.0, "io_read_count": 1}, 1.0) is None


def test_agent_reports_a_component_once_a_tick_with_full_metrics_for_a_server(stand_in):
    process = stand_in("server", "--server-id", "T1")
    stand_in("client", "--client-id", "T1c")
    time.sleep(agent_module.MIN_AGE + 0.2)
    agent = Agent(host="test-host")

    events, samples = observe(agent, "T1")
    _, client_samples = observe(agent, "T1c", ticks=2)

    assert [(event["event"], event["incarnation"], event["role"]) for event in events] == [
        ("process_started", 1, "server")]
    assert len(samples) == 3                      # the first tick only sets the reference point
    assert {sample["incarnation"] for sample in samples} == {1}
    assert all(sample["role"] == "server" and sample["host"] == "test-host" for sample in samples)
    metrics = samples[-1]["m"]
    assert metrics["cpu"] < 30 and metrics["rss_mb"] > 1 and metrics["threads"] >= 1
    assert "ctx_voluntary_s" in metrics
    assert samples[-1]["age"] > 1.5
    # Under a virtual-environment launcher the reported root is the launcher, as started here.
    assert samples[-1]["pid"] == process.pid
    assert [sample["seq"] for sample in samples] == sorted({sample["seq"] for sample in samples})
    assert "threads" not in client_samples[-1]["m"] and "cpu" in client_samples[-1]["m"]


def test_thread_count_and_context_switches_are_read_every_other_tick_and_held(stand_in, monkeypatch):
    process = stand_in("server", "--server-id", "T4")
    time.sleep(agent_module.MIN_AGE + 0.2)
    readings = []
    original = agent_module.read_detail

    def counted(members):
        if process.pid in [member.pid for member in members]:
            readings.append(time.monotonic())
        return original(members)

    monkeypatch.setattr(agent_module, "read_detail", counted)

    _, samples = observe(Agent(), "T4", ticks=7, pause=0.3)

    # Once when the process is first seen, then on every second tick.
    assert len(readings) == 1 + 7 // agent_module.DETAIL_EVERY
    assert len(samples) == 6 and all("threads" in sample["m"] for sample in samples)
    rates = [sample["m"]["ctx_voluntary_s"] for sample in samples if "ctx_voluntary_s" in sample["m"]]
    assert len(rates) >= 5 and any(first == second for first, second in zip(rates, rates[1:]))


def test_agent_sees_a_busy_process_as_busy(stand_in):
    stand_in("server", "--server-id", "T2", code=BUSY)
    time.sleep(agent_module.MIN_AGE + 0.2)

    _, samples = observe(Agent(), "T2")

    assert samples[-1]["m"]["cpu"] > 50


def test_a_relaunched_component_keeps_its_label_with_a_new_incarnation(stand_in):
    first = stand_in("server", "--server-id", "T3")
    time.sleep(agent_module.MIN_AGE + 0.2)
    agent = Agent()
    observe(agent, "T3", ticks=2)

    first.kill()
    first.wait()
    events, samples = observe(agent, "T3", ticks=2)
    assert [event["event"] for event in events] == ["process_exited"] and samples == []

    second = stand_in("server", "--server-id", "T3")
    events, samples = observe(agent, "T3", ticks=4)

    assert [(event["event"], event["incarnation"]) for event in events] == [("process_started", 2)]
    assert samples and {sample["incarnation"] for sample in samples} == {2}
    assert samples[-1]["pid"] == second.pid != first.pid
    assert samples[0]["age"] < 3


def test_agent_reports_the_host_and_its_own_cost():
    agent = Agent(host="test-host", interval=0.3)
    first = agent.tick()
    time.sleep(0.3)

    second = agent.tick()
    host = [record for record in second if record.get("role") == "host"]

    assert (first[0]["event"], first[0]["interval"], first[0]["cores"] >= 1) == ("agent_started", 0.3, True)
    assert {"os", "python", "psutil"} <= set(first[0])
    assert not any(record.get("event") == "agent_started" for record in second)
    assert len(host) == 1 and host[0]["label"] == "host:test-host"
    assert {"cpu", "cpu_max_core", "mem_available_mb", "disk_free_mb", "agent_cpu", "agent_rss_mb"} <= set(host[0]["m"])


def test_sender_holds_lines_while_the_collector_is_down_then_delivers_them_in_order():
    port = free_ports(1)[0]
    sender = Sender(("127.0.0.1", port))
    sender.RETRY_SECONDS = 0.0

    assert not sender.send(["one"])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen()
        assert sender.send(["two", "three"])
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(5)
            received = b""
            while received.count(b"\n") < 3:
                received += connection.recv(4096)
    sender.close()

    assert received == b"one\ntwo\nthree\n"


def test_sender_does_not_retry_the_connection_on_every_tick():
    sender = Sender(("127.0.0.1", free_ports(1)[0]), timeout=0.2)

    assert not sender.send(["one"])
    before = time.monotonic()
    assert not sender.send(["two"])

    assert time.monotonic() - before < 0.1        # no second connection attempt yet
    assert len(sender._pending) == 2
