"""Successful passive replies must survive an immediate primary crash."""

import asyncio
from dataclasses import replace

import pytest

from ft_system.client.passive import PassiveClientApp
from ft_system.common.protocol import build_checkpoint_ack, build_client_request
from ft_system.common.transport import close_writer, read_message, write_message
from tests.helpers import eventually
from tests.integration.test_m4_system import Cluster, REPLICAS, until_success


async def test_acknowledged_request_survives_primary_crash_before_next_checkpoint():
    cluster = Cluster("passive")
    cluster.replication = replace(cluster.replication, checkpoint_freq=.5)
    await cluster.start()
    try:
        for replica in REPLICAS:
            await cluster.launch(replica)
        await eventually(lambda: cluster.rm.policy.ready == set(REPLICAS), timeout=4)
        client = cluster.add_client(PassiveClientApp, "C1", None, .3, 2)
        reply = await until_success(client.send_increment)
        assert reply["payload"]["client_value"] == 1

        # No wait for another checkpoint after the successful reply.
        await cluster.crash("S1")
        await eventually(lambda: client.primary_id == "S2", timeout=2)
        next_reply = await until_success(client.send_increment)
        assert next_reply["payload"]["client_value"] == 2
    finally:
        await cluster.close()


@pytest.mark.parametrize("drop_ack", [False, True])
async def test_success_waits_for_checkpoint_installation_ack_from_every_backup(drop_ack):
    cluster = Cluster("passive")
    installed, acknowledge = asyncio.Event(), asyncio.Event()
    handlers = set()
    dropped = False

    async def checkpoint_backup(reader, writer):
        nonlocal dropped
        task = asyncio.current_task()
        handlers.add(task)
        try:
            while True:
                checkpoint = await read_message(reader)
                if checkpoint["payload"]["state"]["C1"] == 1:
                    if drop_ack and not dropped:
                        dropped = True
                        return  # Installed, but the acknowledgement was lost.
                    installed.set()
                    await acknowledge.wait()
                await write_message(writer, build_checkpoint_ack(checkpoint))
        except (EOFError, ConnectionError):
            pass
        finally:
            handlers.discard(task)
            await close_writer(writer)

    # A wire-level backup deliberately delays its installation acknowledgement.
    backup = await asyncio.start_server(checkpoint_backup, "127.0.0.1",
                                        cluster.servers["S3"].checkpoint_port)
    await cluster.start()
    pending = None
    try:
        for replica in ("S1", "S2"):
            await cluster.launch(replica)
        cluster.reporters["S3"].set_health(True)
        await eventually(lambda: cluster.rm.member_count == 3 and "S2" in cluster.rm.policy.ready, timeout=3)
        client = cluster.add_client(PassiveClientApp, "C1", None, .3, 2)
        pending = asyncio.create_task(client.send_increment())
        await asyncio.wait_for(installed.wait(), 2)
        await eventually(lambda: cluster.replicas["S2"].state_machine.snapshot()["C1"] == 1)
        await asyncio.sleep(.05)
        assert not pending.done(), "a TCP send or just one backup's ACK is not sufficient"

        acknowledge.set()
        assert (await asyncio.wait_for(pending, 2))["payload"]["client_value"] == 1
    finally:
        acknowledge.set()
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await cluster.close()
        backup.close()
        await backup.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_retry_while_waiting_for_checkpoint_and_after_failover_is_not_applied_twice():
    cluster = Cluster("passive")
    cluster.replication = replace(cluster.replication, checkpoint_freq=.3)
    await cluster.start()
    writers = []
    try:
        for replica in REPLICAS:
            await cluster.launch(replica)
        await eventually(lambda: cluster.rm.policy.ready == set(REPLICAS), timeout=3)
        readers = []
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", cluster.servers["S1"].client_port)
            readers.append(reader)
            writers.append(writer)
            await write_message(writer, build_client_request("C1", "S1", 1))
        replies = await asyncio.wait_for(asyncio.gather(*(read_message(reader) for reader in readers)), 2)
        assert [reply["payload"]["client_value"] for reply in replies] == [1, 1]

        await cluster.crash("S1")
        client = cluster.add_client(PassiveClientApp, "C1", None, .3, 2)
        # A client that did not receive the original reply retries request 1.
        assert (await until_success(client.send_increment))["payload"]["client_value"] == 1
        assert (await until_success(client.send_increment))["payload"]["client_value"] == 2
    finally:
        await asyncio.gather(*(close_writer(writer) for writer in writers))
        await cluster.close()
