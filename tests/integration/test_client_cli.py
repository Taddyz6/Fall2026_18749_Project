import asyncio
import io
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ft_system.common.config import ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.server.app import ServerApp
from tests.helpers import eventually


@pytest.mark.parametrize("interactive", [False, True])
async def test_cli_sends_and_exits_in_automatic_and_manual_modes(tmp_path, interactive):
    server = ServerApp(
        ServerConfig("S1", "127.0.0.1", "127.0.0.1", 0, 0),
        ("C1", "C2", "C3"),
        EventLogger(stream=io.StringIO(), use_color=False),
    )
    await server.start()
    process = None
    try:
        root = Path(__file__).resolve().parents[2]
        config_text = (root / "configs/local.toml").read_text()
        config_text = config_text.replace("client_port = 5001", f"client_port = {server.bound_client_port}")
        config_text = config_text.replace("heartbeat_port = 6001", f"heartbeat_port = {server.bound_heartbeat_port}")
        config = tmp_path / "client.toml"
        config.write_text(config_text)
        arguments = ["--interactive"] if interactive else ["--interval", "0.02"]
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ft_system.client.main",
            "--config", str(config), "--client-id", "C1", *arguments,
            stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if interactive:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(b"increment\n\nquit\n"), timeout=5,
            )
            assert server.state_machine.snapshot()["C1"] == 2
        else:
            await eventually(lambda: server.state_machine.snapshot()["C1"] >= 2, timeout=5)
            # Keep the same endpoint when restarting S1 so the running CLI must reconnect.
            restart_config = replace(server.config, client_port=server.bound_client_port)
            await server.close()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
                assert line, "client exited before reporting the disconnection"
                if b"ERROR:" in line:
                    break
            assert process.returncode is None
            server = ServerApp(
                restart_config,
                ("C1", "C2", "C3"),
                EventLogger(stream=io.StringIO(), use_color=False),
            )
            await server.start()
            await eventually(lambda: server.state_machine.snapshot()["C1"] >= 2, timeout=5)
            process.send_signal(signal.SIGINT)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)

        assert process.returncode == 0
        assert b"reply>" in stdout
        assert stderr == b""
        assert server.state_machine.snapshot()["C2"] == 0
        assert server.state_machine.snapshot()["C3"] == 0
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await server.close()
