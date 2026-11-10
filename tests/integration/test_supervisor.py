import asyncio
import io

import pytest

from ft_system.common.config import ConfigError, LfdConfig, RmConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import ProtocolError, build_rm_command
from ft_system.lfd.supervisor import ReplicaSupervisor
from ft_system.server.app import ServerApp
from tests.helpers import eventually, free_ports


@pytest.fixture
def supervisor(tmp_path):
    client_port, heartbeat_port = free_ports(2)
    config = tmp_path / "local config.toml"
    config.write_text(f'''[server.S1]
bind_host = "127.0.0.1"
advertised_host = "127.0.0.1"
client_port = {client_port}
heartbeat_port = {heartbeat_port}
[client.C1]
server_ids = ["S1"]
connect_timeout = 0.2
read_timeout = 0.2
[lfd.LFD1]
server_id = "S1"
heartbeat_freq = 0.05
connect_timeout = 0.2
read_timeout = 0.2
''')
    server = ServerConfig("S1", "127.0.0.1", "127.0.0.1", client_port, heartbeat_port)
    lfd = LfdConfig("LFD1", "S1", .05, .2, .2)
    logger = EventLogger(stream=io.StringIO(), use_color=False)
    return ReplicaSupervisor(lfd, server, RmConfig("127.0.0.1", "127.0.0.1", 1),
                             config, tmp_path / "replica logs/S1.log", logger)


async def test_concurrent_commands_spawn_one_child_and_recover_twice(supervisor):
    try:
        for _ in range(3):
            results = await asyncio.gather(*(supervisor.ensure_running() for _ in range(10)))
            assert [r["payload"]["status"] for r in results].count("started") == 1
            assert len({r["payload"]["pid"] for r in results}) == 1
            process = supervisor._process
            assert int(supervisor.pid_path.read_text()) == process.pid
            process.kill()
            await asyncio.wait_for(process.wait(), 3)
            await eventually(lambda: not supervisor.pid_path.exists())
        assert supervisor.pid is None
    finally:
        await supervisor.close()


async def test_shutdown_reaps_child_and_rejects_late_commands(supervisor):
    await supervisor.ensure_running()
    process = supervisor._process
    await supervisor.close()
    assert process.returncode is not None
    assert not supervisor.pid_path.exists()
    assert (await supervisor.ensure_running())["payload"]["status"] == "failed"
    await supervisor.close()


async def test_existing_manual_replica_is_neither_duplicated_nor_stopped(supervisor):
    server = ServerApp(supervisor.server, ("C1",), supervisor.logger)
    await server.start()
    try:
        result = await supervisor.ensure_running()
        assert result["payload"]["status"] == "external"
        assert supervisor._process is None
        await supervisor.close()
        assert await supervisor._already_listening()
    finally:
        await server.close()


async def test_failed_log_setup_is_reported_and_can_be_retried(supervisor):
    supervisor.log_path.parent.write_text("a file blocks the directory")
    try:
        result = await supervisor.ensure_running()
        assert result["payload"]["status"] == "failed"
        assert supervisor._process is None
        supervisor.log_path.parent.unlink()
        assert (await supervisor.ensure_running())["payload"]["status"] == "started"
    finally:
        await supervisor.close()


async def test_spawn_permission_error_is_reported_and_retry_works(supervisor, monkeypatch):
    create = asyncio.create_subprocess_exec

    async def denied(*args, **kwargs):
        raise PermissionError("launch denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)
    assert (await supervisor.ensure_running())["payload"]["status"] == "failed"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    try:
        assert (await supervisor.ensure_running())["payload"]["status"] == "started"
    finally:
        await supervisor.close()


async def test_cancellation_during_spawn_does_not_orphan_child(supervisor, monkeypatch):
    create = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(supervisor.ensure_running())
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
    finally:
        release.set()
        await supervisor.close()


@pytest.mark.parametrize("destination,target,command", [
    ("LFD2", "S1", "ensure_replica"),
    ("LFD1", "S2", "ensure_replica"),
    ("LFD1", None, "set_ready"),
])
async def test_rejects_commands_for_other_replicas(supervisor, destination, target, command):
    with pytest.raises(ProtocolError):
        await supervisor._on_command(build_rm_command(destination, command, target))
    assert supervisor._process is None


@pytest.mark.parametrize("collision", ["log_pid", "log_config", "pid_config", "temporary_config"])
def test_rejects_colliding_output_paths_without_modifying_files(supervisor, tmp_path, collision):
    config = supervisor.config_path
    original = config.read_bytes()
    log = tmp_path / "S1.log"
    if collision == "log_pid":
        log = tmp_path / "S1.pid"
    elif collision == "log_config":
        log = config
    else:
        config = tmp_path / ("S1.pid" if collision == "pid_config" else "S1.pid.tmp")
        config.write_bytes(original)
    with pytest.raises(ConfigError, match="distinct"):
        ReplicaSupervisor(supervisor.lfd, supervisor.server, supervisor.link.config,
                          config, log, supervisor.logger)
    assert config.read_bytes() == original
    assert not list(tmp_path.rglob("*.pid.tmp")) or collision == "temporary_config"
