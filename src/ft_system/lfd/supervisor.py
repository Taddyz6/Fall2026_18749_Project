"""Launch only this LFD's configured replica, on explicit commands from the RM."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from ft_system.common.config import LfdConfig, RmConfig, ServerConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType, ProtocolError, build_heartbeat, build_launch_result,
)
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.rm_link import RmLink
from ft_system.common.transport import close_writer, read_message_with_timeout, write_message


class ReplicaSupervisor:
    """Idempotent local process ownership; GFD/RM still decide health and readiness.

    A missing heartbeat does not justify spawning a second copy of a live child.
    The project fault model is process crashes, not hung processes or partitions.
    """

    def __init__(self, lfd: LfdConfig, server: ServerConfig, rm: RmConfig,
                 config_path: str | Path, log_path: str | Path, logger: EventLogger):
        self.lfd, self.server, self.logger = lfd, server, logger
        self.config_path = Path(config_path).resolve()
        self.log_path = Path(log_path).resolve()
        self.pid_path = self.log_path.with_suffix(".pid")
        self.link = RmLink(lfd.lfd_id, "lfd", rm, logger, self._on_command)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._monitors: set[asyncio.Task] = set()
        self._closing = False

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.returncode is None else None

    async def _on_command(self, message: dict) -> None:
        payload = message["payload"]
        if (message["type"] != MessageType.RM_COMMAND or message["source"] != "RM"
                or message["destination"] != self.lfd.lfd_id
                or payload.get("command") != "ensure_replica"
                or payload.get("target") != self.server.replica_id):
            raise ProtocolError("expected ensure_replica for this LFD's configured server")
        await self.link.send(await self.ensure_running())

    def _result(self, status: str, pid: int | None, detail: str) -> dict:
        return build_launch_result(self.lfd.lfd_id, self.server.replica_id, status, pid, detail)

    async def _already_listening(self) -> bool:
        writer = None
        try:
            reader, writer = await open_connection_with_timeout(
                self.server.advertised_host, self.server.heartbeat_port, self.lfd.connect_timeout)
            heartbeat = build_heartbeat(self.lfd.lfd_id, self.server.replica_id, 1)
            await asyncio.wait_for(write_message(writer, heartbeat), self.lfd.read_timeout)
            ack = await read_message_with_timeout(reader, self.lfd.read_timeout)
            return (ack["type"] == MessageType.HEARTBEAT_ACK
                    and ack["source"] == self.server.replica_id
                    and ack["destination"] == self.lfd.lfd_id and ack["heartbeat_count"] == 1)
        except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError):
            return False
        finally:
            await close_writer(writer)

    async def ensure_running(self) -> dict:
        async with self._lock:
            if self._closing:
                return self._result("failed", None, "supervisor is stopping")
            if self.pid is not None:
                return self._result("running", self.pid, "owned child is still running")
            if await self._already_listening():
                return self._result("external", None, "existing heartbeat responder; no child launched")
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                # Use a fixed argv, local interpreter/config and no shell. PYTHONPATH
                # is absolute so each host can keep its checkout in a different path.
                source = str(Path(__file__).resolve().parents[2])
                environment = os.environ | {
                    "PYTHONPATH": source + os.pathsep + os.environ.get("PYTHONPATH", ""),
                    "PYTHONUNBUFFERED": "1",
                }
                with self.log_path.open("ab", buffering=0) as output:
                    launch = asyncio.create_task(asyncio.create_subprocess_exec(
                        sys.executable, "-m", "ft_system.server.main", "--config", str(self.config_path),
                        "--server-id", self.server.replica_id,
                        stdout=output, stderr=asyncio.subprocess.STDOUT,
                        stdin=asyncio.subprocess.DEVNULL, env=environment,
                        start_new_session=(os.name == "posix"),
                    ))
                    try:
                        self._process = await asyncio.shield(launch)
                    except asyncio.CancelledError:
                        # Cancellation during spawn must not orphan the newly created child.
                        self._process = await launch
                        await self._stop_child(self._process)
                        raise
                process = self._process
                temporary = self.pid_path.with_suffix(".pid.tmp")
                temporary.write_text(f"{process.pid}\n", encoding="utf-8")
                temporary.replace(self.pid_path)
                self.logger.event(f"{self.lfd.lfd_id}: launched {self.server.replica_id} "
                                  f"pid={process.pid}; log={self.log_path}", "recovered")
                monitor = asyncio.create_task(self._watch(process))
                self._monitors.add(monitor)
                monitor.add_done_callback(self._monitors.discard)
                return self._result("started", process.pid, f"stdout: {self.log_path}")
            except OSError as exc:
                if self._process is not None and self._process.returncode is None:
                    await self._stop_child(self._process)
                self.logger.error(self.lfd.lfd_id, f"cannot launch {self.server.replica_id}: {exc}")
                return self._result("failed", None, str(exc) or type(exc).__name__)

    def _remove_pid(self, pid: int) -> None:
        try:
            # An older exit callback must not erase a replacement's PID file.
            if self.pid_path.read_text(encoding="utf-8").strip() == str(pid):
                self.pid_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.logger.error(self.lfd.lfd_id, f"cannot remove PID file: {exc}")

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        self._remove_pid(process.pid)
        self.logger.event(f"{self.lfd.lfd_id}: {self.server.replica_id} pid={process.pid} "
                          f"exited code={code}; waiting for RM", "failed")

    @staticmethod
    async def _stop_child(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await process.wait()

    async def close(self) -> None:
        self._closing = True
        async with self._lock:
            if self._process is not None:
                await self._stop_child(self._process)
                self._remove_pid(self._process.pid)
        await asyncio.gather(*self._monitors)
