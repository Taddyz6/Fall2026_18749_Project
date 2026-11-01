import asyncio
import io

from ft_system.client.active import ActiveClientApp
from ft_system.client.app import ServerEndpoint
from ft_system.client.passive import PassiveClientApp
from ft_system.client.rm_subscriber import subscribe
from ft_system.common.config import GfdConfig, ReplicationConfig, RmConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import ProtocolError
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
