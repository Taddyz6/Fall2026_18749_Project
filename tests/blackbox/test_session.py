import asyncio
import json

import pytest

pytest.importorskip("psutil")

from ft_system.blackbox import cluster as cluster_module  # noqa: E402
from ft_system.blackbox.baseline import Signature, train  # noqa: E402
from ft_system.blackbox.cluster import ClusterError, LocalCluster  # noqa: E402
from ft_system.blackbox.dashboard import Dashboard  # noqa: E402
from ft_system.blackbox.detect import PAPER  # noqa: E402
from ft_system.blackbox.diagnose import Diagnoser  # noqa: E402
from ft_system.blackbox.faults.ctl import EVENTS_FILE  # noqa: E402
from ft_system.blackbox.session import Session, send  # noqa: E402
from tests.blackbox.builders import run  # noqa: E402


class FakeProcess:
    count = 0

    def __init__(self):
        FakeProcess.count += 1
        self.pid, self.alive = 100000 + FakeProcess.count, True

    def poll(self):
        return None if self.alive else 0


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    """A cluster on the real spare-client config whose processes are stand-ins."""

    made = {}

    def launch(self, name, module, *arguments, shim=False):
        made[name] = self._processes[name] = FakeProcess()

    def kill(pid):
        for process in made.values():
            if process.pid == pid:
                process.alive = False

    monkeypatch.setattr(LocalCluster, "_launch", launch)
    monkeypatch.setattr(cluster_module, "kill_tree", kill)
    result = LocalCluster(tmp_path, "configs/ec.active.local.toml", clients=3)
    result.set_clients(3)
    result._base = result.client_ids[:3]
    return result


def test_raising_the_load_uses_spare_clients_that_have_not_run_and_never_the_same_one_twice(cluster):
    assert cluster.running_clients() == ["C1", "C2", "C3"]

    cluster.set_clients(6)
    assert cluster.running_clients() == ["C1", "C2", "C3", "C4", "C5", "C6"]
    cluster.set_clients(3)
    assert cluster.running_clients() == ["C1", "C2", "C3"]
    cluster.set_clients(6)
    assert cluster.running_clients() == ["C1", "C2", "C3", "C7", "C8", "C9"]      # C4 to C6 are spent
    cluster.set_clients(4)
    assert cluster.running_clients() == ["C1", "C2", "C3", "C7"]
    cluster.set_clients(4)
    assert cluster.running_clients() == ["C1", "C2", "C3", "C7"]                  # no change asked for


def test_the_load_cannot_go_below_the_starting_clients_or_beyond_the_spares(cluster):
    with pytest.raises(ClusterError, match="cannot run fewer than the 3 clients the session started with"):
        cluster.set_clients(2)
    cluster.set_clients(12)
    cluster.set_clients(3)
    with pytest.raises(ClusterError, match="only 0 unused spare clients are left"):
        cluster.set_clients(4)
    assert cluster.running_clients() == ["C1", "C2", "C3"]


@pytest.fixture
def session(tmp_path, cluster, monkeypatch):
    signature = Signature(train(run(), warmup=90.0))
    result = Session(tmp_path, tmp_path / "baseline.json")
    result.cluster = cluster
    result.dashboard = Dashboard(signature, Diagnoser(signature, PAPER))
    cluster.view = lambda: ({"S1", "S2", "S3"}, {"S1", "S2", "S3"})
    cluster.killed = []
    cluster.kill_replica = lambda server: cluster.killed.append(server) or 4242
    return result


def recorded(session):
    return [json.loads(line) for line in (session.run_dir / EVENTS_FILE).read_text(encoding="utf-8").splitlines()]


def test_controls_offer_the_demo_buttons_with_the_load_doubled_from_the_start(session):
    controls = session.controls()

    assert (controls["targets"], controls["default"]) == (["S1", "S2", "S3"], "S2")
    assert [(button["label"], button.get("argument"), button.get("target")) for button in controls["buttons"]] == [
        ("Inject fault", None, True), ("Clear fault", None, True), ("Restart replica", None, True),
        ("Double the load", "6", None), ("Normal load", "3", None)]
    assert set(session.actions()) == {"inject", "clear", "load", "rejuvenate", "down"}


def test_load_changes_the_clients_and_records_it(session):
    assert session.actions()["load"](" 6 ") == "6 clients running"

    assert session.cluster.running_clients() == ["C1", "C2", "C3", "C4", "C5", "C6"]
    assert [(event["event"], event["clients"]) for event in recorded(session)] == [("load_changed", 6)]
    with pytest.raises(ValueError):
        session.actions()["load"]("many")


def test_rejuvenate_restarts_the_named_or_the_diagnosed_replica_and_records_it(session):
    with pytest.raises(ClusterError, match="no replica is diagnosed now"):
        session.rejuvenate("")

    assert session.rejuvenate("S1") == "S1 (pid 4242) killed; the RM relaunches it"
    session.dashboard.status = {"state": "fault", "culprit": "S2"}
    session.rejuvenate("")

    assert session.cluster.killed == ["S1", "S2"]
    assert [(event["event"], event["server"], event["reason"]) for event in recorded(session)] == [
        ("replica_killed", "S1", "rejuvenation"), ("replica_killed", "S2", "rejuvenation")]


def test_rejuvenate_is_refused_while_a_replica_is_not_ready(session):
    session.cluster.view = lambda: ({"S1", "S2", "S3"}, {"S1", "S3"})

    with pytest.raises(ClusterError, match="not restarting S1: only S1 and S3 ready"):
        session.rejuvenate("S1")
    assert session.cluster.killed == []


async def test_send_reaches_a_running_dashboard_and_reports_refusals(session):
    def refuse(argument):
        raise ClusterError("not now")

    board = Dashboard(session.dashboard.signature, session.dashboard.diagnoser,
                      actions={"load": lambda argument: f"{argument} clients", "rejuvenate": refuse})
    server = await board.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    try:
        assert await loop.run_in_executor(None, send, "load", "6", port) == (True, "6 clients")
        assert await loop.run_in_executor(None, send, "rejuvenate", "", port) == (False, "not now")
        assert (await loop.run_in_executor(None, send, "missing", "", port))[0] is False
    finally:
        server.close()
    with pytest.raises(ConnectionError, match="no session is listening"):
        await loop.run_in_executor(None, send, "load", "6", port)
