import asyncio
import io

import pytest

import ft_system.client.active as active_module
from ft_system.client.active import ActiveClientApp
from ft_system.client.app import ServerEndpoint
from ft_system.client.passive import PassiveClientApp
from ft_system.client.rm_subscriber import subscribe
from ft_system.common.config import GfdConfig, ReplicationConfig, RmConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import ProtocolError
from ft_system.common.rm_link import RmLink
from ft_system.gfd.app import GfdApp
from ft_system.lfd.reporter import GfdReporter
from ft_system.rm.app import RmApp
from ft_system.server.app import ServerApp
from tests.helpers import eventually, free_ports


CLIENTS = ("C1", "C2", "C3")
REPLICAS = ("S1", "S2", "S3")


class Cluster:
    """RM, GFD, and three replicas in one process; GFD reporters stand in for the LFDs."""

    def __init__(self, mode):
        ports = free_ports(11)
        self.mode = mode
        self.rm_config = RmConfig("127.0.0.1", "127.0.0.1", ports[0], .5, transfer_delay=.2, transfer_retry=.3)
        self.gfd_config = GfdConfig("127.0.0.1", "127.0.0.1", ports[1], .05, .5)
        self.servers = {
            replica: ServerConfig(replica, "127.0.0.1", "127.0.0.1", *ports[2 + 3 * i:5 + 3 * i])
            for i, replica in enumerate(REPLICAS)
        }
        self.replication = ReplicationConfig(mode, .05)
        self.logs = {}
        self.replicas = {}
        self.reporters = {}
        self.tasks = []
        self.clients = []

    def logger(self, name):
        self.logs[name] = io.StringIO()
        return EventLogger(stream=self.logs[name], use_color=False)

    def log(self, name):
        return self.logs[name].getvalue()

    async def start(self):
        self.rm = RmApp(self.rm_config, self.mode, REPLICAS, CLIENTS, self.logger("RM"))
        await self.rm.start()
        self.gfd = GfdApp(self.gfd_config, {f"LFD{i}": replica for i, replica in enumerate(REPLICAS, 1)},
                          self.logger("GFD"), self.rm_config)
        await self.gfd.start()
        for i, replica in enumerate(REPLICAS, 1):
            reporter = GfdReporter(f"LFD{i}", replica, self.gfd_config, self.logger(f"LFD{i}"))
            self.reporters[replica] = reporter
            self.tasks.append(asyncio.create_task(reporter.run()))
        await eventually(lambda: len(self.gfd._sessions) == 3, timeout=3)

    async def launch(self, replica, log_name=None):
        app = ServerApp(self.servers[replica], CLIENTS, self.logger(log_name or replica),
                        self.replication, self.servers.values(), self.rm_config)
        await app.start()
        self.replicas[replica] = app
        self.reporters[replica].set_health(True)  # The LFD's first successful heartbeat.
        await eventually(lambda: replica in self.rm.membership, timeout=3)
        return app

    async def crash(self, replica):
        await self.replicas.pop(replica).close()
        self.reporters[replica].set_health(False)
        await eventually(lambda: replica not in self.rm.membership, timeout=3)

    def add_client(self, client_class, client_id, *args):
        endpoints = [ServerEndpoint(r, "127.0.0.1", self.servers[r].client_port) for r in REPLICAS]
        logger = self.logger(client_id)
        client = client_class(client_id, endpoints, *args, logger)
        self.clients.append(client)
        self.tasks.append(asyncio.create_task(subscribe(client, self.rm_config, logger).run()))
        return client

    def states(self):
        return [app.state_machine.snapshot() for app in self.replicas.values()]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(client.close() for client in self.clients))
        await asyncio.gather(*(app.close() for app in self.replicas.values()))
        await self.gfd.close()
        await self.rm.close()


async def until_success(call, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await call()
        except (EOFError, TimeoutError, ConnectionError, OSError, ProtocolError):
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(.05)


async def test_active_relaunched_replica_logs_then_catches_up_without_divergence():
    cluster = Cluster("active")
    await cluster.start()
    try:
        assert cluster.log("RM").count("RM: 0 members") >= 2  # At start, then from the GFD.
        for replica in REPLICAS:
            await cluster.launch(replica)
            await eventually(lambda: replica in cluster.rm.policy.ready, timeout=3)
        assert "RM: 3 members: S1, S2, S3" in cluster.log("RM")
        assert "RM: S1 is the first replica" in cluster.log("RM")
        assert "S2 received checkpoint" in cluster.log("S2")
        status = cluster.rm.status_lines()
        assert "replicas registered: S1, S2, S3" in status[1]
        assert not any(line.startswith("hint:") for line in status)
        assert "role active, i_am_ready = 1" in cluster.replicas["S2"].status_lines()[0]

        clients = [cluster.add_client(ActiveClientApp, client_id, .5, 1.0) for client_id in CLIENTS]
        for _ in range(5):
            await asyncio.gather(*(client.send_increment() for client in clients))

        await cluster.crash("S2")
        assert "RM: 2 members: S1, S3" in cluster.log("RM")
        for _ in range(5):
            await asyncio.gather(*(client.send_increment() for client in clients))

        # Keep traffic flowing while S2 is relaunched, logs requests, and receives state.
        stop = asyncio.Event()

        async def drive(client):
            while not stop.is_set():
                await client.send_increment()
                await asyncio.sleep(.005)

        drivers = [asyncio.create_task(drive(client)) for client in clients]
        await cluster.launch("S2", "S2-relaunch")
        await eventually(lambda: any("S2 waiting for state from S1" in line
                                     for line in cluster.rm.status_lines()), timeout=2)
        await eventually(lambda: "S2" in cluster.rm.policy.ready, timeout=5)
        await eventually(lambda: "Sending <C1, S2," in cluster.log("S2-relaunch"), timeout=2)
        stop.set()
        await asyncio.gather(*drivers)

        relaunch = cluster.log("S2-relaunch")
        assert "i_am_ready = 0; logged <C" in relaunch
        assert "i_am_ready = 1 (checkpoint installed)" in relaunch
        assert "RM: asking S1 to transfer its state to S2" in cluster.log("RM")
        expected = {client.client_id: client.next_request_num - 1 for client in clients}
        await eventually(lambda: cluster.states() == [expected] * 3, timeout=3)
        assert not any("divergence" in cluster.log(client_id) for client_id in CLIENTS)
    finally:
        await cluster.close()


async def test_active_recovery_keeps_connection_when_checkpoint_overtakes_requests(monkeypatch):
    cluster = Cluster("active")
    transfer_allowed, requests_allowed, request_arrived = (asyncio.Event() for _ in range(3))
    await cluster.start()
    try:
        for replica in REPLICAS:
            await cluster.launch(replica)
            await eventually(lambda: replica in cluster.rm.policy.ready, timeout=3)
        client = cluster.add_client(ActiveClientApp, "C1", .5, 1.0)
        for _ in range(5):
            await client.send_increment()
        await cluster.crash("S2")
        for _ in range(5):
            await client.send_increment()

        transfer = cluster.replicas["S1"]._transfer_state

        async def delayed_transfer(target):
            await transfer_allowed.wait()
            await transfer(target)

        monkeypatch.setattr(cluster.replicas["S1"], "_transfer_state", delayed_transfer)
        s2 = await cluster.launch("S2", "S2-relaunch")
        process = s2.client_listener._process

        async def delayed_request(request, writer):
            request_arrived.set()
            await requests_allowed.wait()
            return await process(request, writer)

        monkeypatch.setattr(s2.client_listener, "_process", delayed_request)
        await eventually(lambda: "S2" in client._connections, timeout=2)
        connection = client._connections["S2"]
        for _ in range(5):
            await client.send_increment()
        await asyncio.wait_for(request_arrived.wait(), 1)
        # Two independent TCP channels: checkpoint 15 arrives before requests 11-15.
        transfer_allowed.set()
        await eventually(lambda: "S2" in cluster.rm.policy.ready, timeout=3)
        assert s2.state_machine.snapshot()["C1"] == 15
        requests_allowed.set()
        await eventually(lambda: connection.last_received == 15 or connection.task.done(), timeout=2)
        assert not connection.task.done(), cluster.log("C1")
        assert client._connections["S2"] is connection

        for number in range(16, 21):
            assert (await client.send_increment())["payload"]["client_value"] == number
        await eventually(lambda: cluster.states() == [{"C1": 20, "C2": 0, "C3": 0}] * 3)
        assert cluster.rm.policy.ready == set(REPLICAS)
        assert "divergence" not in cluster.log("C1")
    finally:
        requests_allowed.set()
        transfer_allowed.set()
        await cluster.close()


async def test_registering_replica_preserves_initial_transfer_delay(monkeypatch):
    cluster = Cluster("active")
    transferred = asyncio.Event()
    await cluster.start()
    try:
        donor = await cluster.launch("S1")
        await eventually(lambda: "S1" in cluster.rm.policy.ready)
        transfer = donor._transfer_state
        transfer_times = []

        async def record_transfer(target):
            transfer_times.append(asyncio.get_running_loop().time())
            transferred.set()
            await transfer(target)

        monkeypatch.setattr(donor, "_transfer_state", record_transfer)
        started = asyncio.get_running_loop().time()
        # The GFD's membership report can arrive before the replica registers with RM.
        cluster.reporters["S2"].set_health(True)
        await eventually(lambda: "S2" in cluster.rm.membership)
        await cluster.launch("S2")
        await asyncio.wait_for(transferred.wait(), 2)

        assert transfer_times[0] - started >= cluster.rm_config.transfer_delay
        await eventually(lambda: "S2" in cluster.rm.policy.ready)
    finally:
        await cluster.close()


@pytest.mark.parametrize("failure", ["initial", "disconnect"])
async def test_active_retries_current_member_without_new_view(monkeypatch, failure):
    cluster = Cluster("active")
    failed_attempt = asyncio.Event()
    allow_connection = failure == "disconnect"
    open_connection = active_module.open_connection_with_timeout

    async def unavailable_s2(host, port, timeout):
        if port == cluster.servers["S2"].client_port and not allow_connection:
            failed_attempt.set()
            raise TimeoutError("temporary client-channel outage")
        return await open_connection(host, port, timeout)

    await cluster.start()
    try:
        for replica in REPLICAS:
            await cluster.launch(replica)
            await eventually(lambda: replica in cluster.rm.policy.ready, timeout=3)
        monkeypatch.setattr(active_module, "open_connection_with_timeout", unavailable_s2)
        client = cluster.add_client(ActiveClientApp, "C1", .5, 1.0)
        await client.connect()
        view_num = cluster.rm.policy.view_num
        await eventually(lambda: f"C1: view {view_num}:" in cluster.log("C1"))
        if failure == "disconnect":
            allow_connection = False
            client._connections["S2"].writer.close()
            await eventually(lambda: "S2" not in client._connections)
        for _ in range(5):
            await client.send_increment()
        await asyncio.wait_for(failed_attempt.wait(), 2)
        allow_connection = True

        # No membership/readiness change is available to trigger the next attempt.
        await eventually(lambda: "S2" in client._connections, timeout=2)
        assert cluster.rm.policy.view_num == view_num
        assert (await client.send_increment())["payload"]["client_value"] == 6
        await eventually(lambda: cluster.states() == [{"C1": 6, "C2": 0, "C3": 0}] * 3, timeout=3)
        assert "divergence" not in cluster.log("C1")
    finally:
        await cluster.close()


@pytest.mark.parametrize("action", ["remove_member", "close_client"])
async def test_active_cancels_unneeded_connection_attempts(monkeypatch, action):
    logger = EventLogger(stream=io.StringIO(), use_color=False)
    server = ServerApp(ServerConfig("S1", "127.0.0.1", "127.0.0.1", 0, 0), CLIENTS, logger)
    await server.start()
    client = ActiveClientApp("C1", [ServerEndpoint("S1", "127.0.0.1", server.bound_client_port)],
                             .5, 1.0, logger)
    started, release = asyncio.Event(), asyncio.Event()
    open_connection = active_module.open_connection_with_timeout

    async def delayed_connection(*args):
        started.set()
        await release.wait()
        return await open_connection(*args)

    monkeypatch.setattr(active_module, "open_connection_with_timeout", delayed_connection)
    view = {"view_num": 1, "mode": "active", "members": ["S1"], "ready": ["S1"], "primary": None}
    try:
        client.apply_view(view)
        await asyncio.wait_for(started.wait(), 1)
        if action == "remove_member":
            client.apply_view(view | {"view_num": 2, "members": [], "ready": []})
        else:
            await client.close()
            client.apply_view(view)  # A late callback must not revive a closed client.
            assert not client._tasks
        release.set()
        await eventually(lambda: not client._opening)
        assert not client._connections
        await eventually(lambda: not server._active_writers)
    finally:
        release.set()
        await client.close()
        await server.close()


async def test_rm_flags_a_component_running_different_code_or_config():
    stream = io.StringIO()
    rm = RmApp(RmConfig("127.0.0.1", "127.0.0.1", 0), "active", REPLICAS, CLIENTS,
               EventLogger(stream=stream, use_color=False), {"build": "abc1234", "config": "11111111"})
    await rm.start()

    async def ignore(message):
        pass

    link = RmLink("S2", "server", RmConfig("127.0.0.1", "127.0.0.1", rm.bound_port),
                  EventLogger(stream=io.StringIO(), use_color=False), ignore,
                  identity={"build": "abc1234", "config": "22222222"})
    task = asyncio.create_task(link.run())
    try:
        await eventually(lambda: "MISMATCH: S2 runs build abc1234 with config 22222222" in stream.getvalue())
        assert any(line.startswith("MISMATCH S2") for line in rm.status_lines())
        assert any("S2 is running but is not a member" in line for line in rm.status_lines())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await rm.close()


async def test_passive_primary_failover_and_relaunched_replicas_return_as_backups():
    cluster = Cluster("passive")
    await cluster.start()
    try:
        for replica in REPLICAS:
            await cluster.launch(replica)
        await eventually(lambda: cluster.rm.policy.ready == set(REPLICAS), timeout=3)
        assert cluster.rm.policy.primary == "S1"

        client = cluster.add_client(PassiveClientApp, "C1", None, .3, .5)
        for number in range(1, 4):
            reply = await until_success(client.send_increment)
            assert (reply["replica_id"], reply["payload"]["client_value"]) == ("S1", number)
        await eventually(lambda: cluster.states() == [{"C1": 3, "C2": 0, "C3": 0}] * 3, timeout=2)
        assert "Received <C1, S2," not in cluster.log("S2")

        # Backup failure and manual relaunch: S3 rejoins as a backup via the next checkpoint.
        await cluster.crash("S3")
        assert (await until_success(client.send_increment))["payload"]["client_value"] == 4
        await cluster.launch("S3", "S3-relaunch")
        await eventually(lambda: "S3" in cluster.rm.policy.ready, timeout=3)
        assert "S3 received checkpoint" in cluster.log("S3-relaunch")
        assert cluster.replicas["S3"].role == "backup"

        # Primary failure: the RM elects the oldest ready backup and clients follow.
        await cluster.crash("S1")
        await eventually(lambda: cluster.rm.policy.primary == "S2", timeout=3)
        reply = await until_success(client.send_increment)
        assert (reply["replica_id"], reply["payload"]["client_value"]) == ("S2", 5)
        assert "RM: primary S1 failed; new primary is S2" in cluster.log("RM")
        assert "C1: primary changed S1 -> S2" in cluster.log("C1")

        # The old primary comes back as a backup of the new primary.
        await cluster.launch("S1", "S1-relaunch")
        await eventually(lambda: "S1" in cluster.rm.policy.ready, timeout=3)
        assert cluster.replicas["S1"].role == "backup"
        assert "S1 received checkpoint" in cluster.log("S1-relaunch")
        await eventually(lambda: cluster.states() == [{"C1": 5, "C2": 0, "C3": 0}] * 3, timeout=2)
        assert cluster.rm.policy.primary == "S2"
    finally:
        await cluster.close()


async def test_passive_request_keeps_its_primary_when_the_view_changes_while_connecting():
    logger = EventLogger(stream=io.StringIO(), use_color=False)
    servers = [ServerApp(ServerConfig(replica, "127.0.0.1", "127.0.0.1", 0, 0), CLIENTS, logger)
               for replica in ("S1", "S2")]
    for server in servers:
        await server.start()
    endpoints = [ServerEndpoint(s.config.replica_id, "127.0.0.1", s.bound_client_port) for s in servers]
    client = PassiveClientApp("C1", endpoints, "S1", .5, .5, logger)
    client._sync_needed = False  # No idle connections, so the primary connection is opened on demand.
    open_connection = client._open

    async def view_arrives_during_open(replica):
        connection = await open_connection(replica)
        if replica == "S1":
            client.apply_view({"view_num": 2, "mode": "passive", "members": ["S1", "S2"],
                               "ready": ["S1", "S2"], "primary": "S2"})
        return connection

    client._open = view_arrives_during_open
    try:
        first = await client.send_increment()
        assert first["replica_id"] == "S1" and client._primary[0] == "S1"
        second = await client.send_increment()
        assert second["replica_id"] == "S2" and second["request_num"] == 2
    finally:
        await client.close()
        await asyncio.gather(*(server.close() for server in servers))
