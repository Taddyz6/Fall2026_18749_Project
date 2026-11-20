import json
import os
import subprocess
import sys
import time

import pytest

from ft_system.blackbox.faults import ctl
from ft_system.blackbox.faults.shim import sitecustomize as shim

# A stand-in for a server: it takes --server-id like the real one, so the shim
# loads into it, and it reports its own CPU time whenever it is sent a line.
STAND_IN = "import sys, time\nfor line in sys.stdin:\n    print(time.process_time(), flush=True)\n"


class StandIn:
    def __init__(self, fault_dir, server_id="S9", environment=None):
        self.fault_dir, self.server_id, self.launched = fault_dir, server_id, time.time()
        self.process = subprocess.Popen(
            [sys.executable, "-c", STAND_IN, "--server-id", server_id],
            env=ctl.shim_environment(fault_dir) if environment is None else environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def cpu_seconds(self) -> float:
        self.process.stdin.write("\n")
        self.process.stdin.flush()
        return float(self.process.stdout.readline())

    def state(self) -> dict | None:
        return ctl.replicas(self.fault_dir).get(self.server_id)

    def registered(self, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state is not None and state["started"] >= self.launched:
                return state
            time.sleep(0.02)
        raise AssertionError("the shim did not register")

    def stop(self) -> None:
        self.process.kill()
        self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            pipe.close()


@pytest.fixture
def launch(tmp_path):
    started = []

    def start(**kwargs) -> StandIn:
        replica = StandIn(tmp_path, **kwargs)
        started.append(replica)
        return replica

    yield start
    for replica in started:
        if replica.process.poll() is None:
            replica.stop()


def events(fault_dir) -> list[dict]:
    path = fault_dir / ctl.EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_server_id_is_read_from_either_option_form():
    assert shim.server_id_from(["python", "-m", "ft_system.server.main", "--server-id", "S2"]) == "S2"
    assert shim.server_id_from(["python", "-m", "ft_system.server.main", "--server-id=S3"]) == "S3"
    assert shim.server_id_from(["python", "-m", "ft_system.lfd.main", "--lfd-id", "LFD2"]) is None
    assert shim.server_id_from(["python", "--server-id"]) is None


@pytest.mark.parametrize("server_id, usable", [
    ("S2", True), ("replica-2_b", True), ("", False), (None, False),
    ("../S2", False), ("S2.json", False), ("a b", False)])
def test_only_plain_server_ids_become_file_names(server_id, usable):
    assert shim.safe_id(server_id) is usable


def test_normalize_bounds_parameters_and_reads_other_kinds_as_no_fault():
    assert shim.normalize({"kind": "cpu"}) == {"kind": "cpu", "duty": 1.0, "threads": 1, "period": 0.1}
    assert shim.normalize({"kind": "cpu", "duty": 7, "threads": 99, "period": 0}) == {
        "kind": "cpu", "duty": 1.0, "threads": shim.MAX_THREADS, "period": 0.01}
    assert shim.normalize({"kind": "none", "duty": 0.5}) is None
    assert shim.normalize({}) is None


def test_cpu_hog_burns_about_its_duty_cycle_then_stops():
    hog = shim.CpuHog()
    assert hog.apply({"kind": "cpu", "duty": 0.5, "period": 0.05}) == {
        "kind": "cpu", "duty": 0.5, "threads": 1, "period": 0.05}
    before = time.process_time()
    time.sleep(1.0)
    busy = time.process_time() - before
    assert hog.apply({"kind": "none"}) is None
    before = time.process_time()
    time.sleep(0.5)
    idle = time.process_time() - before

    assert 0.25 < busy < 0.8
    assert idle < 0.1


def test_shim_stays_out_of_processes_that_are_not_servers(tmp_path):
    report = "import threading; print(sorted(thread.name for thread in threading.enumerate()))"
    environment = ctl.shim_environment(tmp_path)
    not_a_server = subprocess.run([sys.executable, "-c", report, "--lfd-id", "LFD2"],
                                  env=environment, capture_output=True, text=True, check=True)
    without_fault_dir = subprocess.run(
        [sys.executable, "-c", report, "--server-id", "S9"], capture_output=True, text=True,
        check=True, env={key: value for key, value in environment.items() if key != "FT_FAULT_DIR"})

    assert "fault-shim" not in not_a_server.stdout
    assert "fault-shim" not in without_fault_dir.stdout
    assert not_a_server.stderr == without_fault_dir.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_inject_and_clear_are_confirmed_by_the_replica_and_recorded(tmp_path, launch):
    replica = launch()
    registration = replica.registered()
    assert registration["applied"] is None and ctl.pid_alive(registration["pid"])

    state = ctl.inject(tmp_path, "S9", duty=0.5)
    before = replica.cpu_seconds()
    time.sleep(1.0)
    busy = replica.cpu_seconds() - before
    assert state["applied"] == {"kind": "cpu", "duty": 0.5, "threads": 1, "period": 0.1}
    assert state["token"] == registration["token"]
    assert 0.25 < busy < 0.8

    state = ctl.clear(tmp_path, "S9")
    before = replica.cpu_seconds()
    time.sleep(0.5)
    idle = replica.cpu_seconds() - before
    assert state["applied"] is None
    assert idle < 0.1
    # Clearing replaces the spec; it is never deleted while the replica may be reading it.
    assert shim.read_json(shim.spec_path(str(tmp_path), "S9"))["kind"] == "none"

    injected, cleared = events(tmp_path)
    assert (injected["event"], injected["server"], injected["fault"]["duty"]) == ("fault_injected", "S9", 0.5)
    assert (cleared["event"], cleared["previous"]["kind"], cleared["fault"]) == ("fault_cleared", "cpu", None)
    assert injected["requested_at"] <= injected["t"] <= cleared["t"]


def test_relaunched_replica_ignores_the_fault_given_to_its_predecessor(tmp_path, launch):
    first = launch()
    first.registered()
    ctl.inject(tmp_path, "S9", duty=0.5)
    first.stop()

    second = launch()
    registration = second.registered()
    before = second.cpu_seconds()
    time.sleep(4 * shim.POLL_SECONDS)
    used = second.cpu_seconds() - before

    assert shim.read_json(shim.spec_path(str(tmp_path), "S9"))["kind"] == "cpu"
    assert second.state() == registration and registration["applied"] is None
    assert used < 0.1


def test_inject_fails_loudly_when_no_shim_is_registered(tmp_path):
    with pytest.raises(ctl.FaultError, match="no shim has registered for S2"):
        ctl.inject(tmp_path, "S2")
    assert events(tmp_path) == []


def test_inject_refuses_a_replica_that_is_not_running(tmp_path):
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    shim.write_json(shim.state_path(str(tmp_path), "S2"),
                    {"server": "S2", "pid": finished.pid, "token": "old", "applied": None})

    assert not ctl.pid_alive(finished.pid) and ctl.pid_alive(os.getpid())
    with pytest.raises(ctl.FaultError, match="is not running"):
        ctl.inject(tmp_path, "S2")
    assert not os.path.exists(shim.spec_path(str(tmp_path), "S2"))


def test_unconfirmed_fault_is_withdrawn_and_recorded(tmp_path):
    # Registered and alive, but nothing in this process is following the spec file.
    shim.write_json(shim.state_path(str(tmp_path), "S2"),
                    {"server": "S2", "pid": os.getpid(), "token": "deaf", "applied": None})

    with pytest.raises(ctl.FaultError, match="did not confirm within 0.3 s"):
        ctl.inject(tmp_path, "S2", wait=0.3)

    assert shim.read_json(shim.spec_path(str(tmp_path), "S2"))["kind"] == "none"
    assert [event["event"] for event in events(tmp_path)] == ["fault_unconfirmed"]


@pytest.mark.parametrize("arguments", [{"duty": 0}, {"duty": 1.2}, {"threads": 0},
                                       {"threads": shim.MAX_THREADS + 1}, {"period": 2}])
def test_inject_rejects_out_of_range_parameters(tmp_path, arguments):
    with pytest.raises(ctl.FaultError, match="must be"):
        ctl.inject(tmp_path, "S2", **arguments)


def test_command_line_reports_status_and_failures(tmp_path, launch, capsys):
    replica = launch()
    pid = replica.registered()["pid"]
    base = ["--fault-dir", str(tmp_path)]

    assert ctl.main(base + ["inject", "S9", "--duty", "0.25"]) == 0
    assert ctl.main(base + ["status"]) == 0
    assert ctl.main(base + ["clear"]) == 0
    assert ctl.main(base + ["inject", "S7"]) == 1
    assert ctl.main(base + ["env", "--shell", "bash"]) == 0
    output = capsys.readouterr().out

    assert f"S9 (pid {pid}): cpu fault: 1 thread busy 25% of every 0.1 s" in output
    assert f"S9  pid {pid}  running  cpu fault: 1 thread busy 25% of every 0.1 s for " in output
    assert f"S9 (pid {pid}): no fault" in output
    assert "error: no shim has registered for S7" in output
    assert f"export FT_FAULT_DIR='{tmp_path.resolve()}'" in output and str(ctl.SHIM_DIR) in output
