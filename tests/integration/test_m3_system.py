import asyncio
import io
from dataclasses import replace

import pytest

from ft_system.client.app import ServerEndpoint
from ft_system.client.passive import PassiveClientApp
from ft_system.common.config import PassiveConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.server.app import ServerApp
from tests.helpers import eventually


def logger(stream):
    return EventLogger(stream=stream, use_color=False)


async def start_passive_cluster():
    base = [ServerConfig(f"S{i}", "127.0.0.1", "127.0.0.1", 0, 0, 0) for i in range(1, 4)]
    backups = [ServerApp(config, ("C1",), logger(io.StringIO()), PassiveConfig("S1", .02), base)
               for config in base[1:]]
    for backup in backups:
        await backup.start()
    resolved = [replace(base[0], client_port=0, heartbeat_port=0, checkpoint_port=0)] + [
        replace(app.config, checkpoint_port=app.bound_checkpoint_port) for app in backups
    ]
    primary_stream = io.StringIO()
    primary = ServerApp(resolved[0], ("C1",), logger(primary_stream), PassiveConfig("S1", .02), resolved)
    await primary.start()
    return primary, backups, primary_stream


@pytest.mark.asyncio
async def test_passive_primary_only_replies_checkpoints_and_survives_backup_failures():
    primary, backups, primary_log = await start_passive_cluster()
    endpoints = [ServerEndpoint("S1", "127.0.0.1", primary.bound_client_port)] + [
        ServerEndpoint(backup.config.replica_id, "127.0.0.1", backup.bound_client_port) for backup in backups
    ]
    client = PassiveClientApp("C1", endpoints, "S1", .2, .5, logger(io.StringIO()))
    try:
        for number in range(1, 4):
            reply = await client.send_increment()
            assert reply["replica_id"] == "S1"
            assert reply["payload"]["client_value"] == number
        await eventually(lambda: all(backup.state_machine.snapshot() == {"C1": 3} for backup in backups), timeout=2)
        assert "sending checkpoint" in primary_log.getvalue()

        await backups[0].close()
        for number in range(4, 7):
            assert (await client.send_increment())["payload"]["client_value"] == number
        await eventually(lambda: backups[1].state_machine.snapshot() == {"C1": 6}, timeout=2)

        await backups[1].close()
        assert (await client.send_increment())["payload"]["client_value"] == 7
        assert primary.state_machine.snapshot() == {"C1": 7}
    finally:
        await client.close()
        await primary.close()
        await asyncio.gather(*(backup.close() for backup in backups))
