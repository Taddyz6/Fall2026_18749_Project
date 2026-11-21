"""Run a managed replica in the foreground of its own macOS Terminal window."""

import asyncio
import os
from pathlib import Path
import shlex
import signal
import socket
import sys
from tempfile import TemporaryDirectory

from ft_system.common.transport import close_writer


async def _open_window(command: str, title: str) -> None:
    script = '''on run argv
        tell application id "com.apple.Terminal"
            set replicaTab to do script (item 1 of argv)
            set custom title of replicaTab to (item 2 of argv)
            activate
        end tell
    end run'''
    opener = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript", "-e", script, command, title,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await opener.communicate()
        if opener.returncode:
            raise OSError(error.decode(errors="replace").strip() or "Terminal failed to open")
    finally:
        if opener.returncode is None:
            opener.kill()
            await opener.wait()


class TerminalReplica:
    """Terminal owns the OS child. Its private socket closes when that exact PID exits."""

    def __init__(self, pid: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.pid = pid
        self.exited = asyncio.create_task(self._disconnected(reader, writer))

    @staticmethod
    async def _disconnected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
        except ConnectionResetError:
            pass
        finally:
            await close_writer(writer)

    async def wait(self) -> None:
        await asyncio.shield(self.exited)

    def terminate(self) -> None:
        if not self.exited.done():
            os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        if not self.exited.done():
            os.kill(self.pid, signal.SIGKILL)


async def launch_terminal(argv: list[str], pythonpath: str, log_path: Path, title: str) -> TerminalReplica:
    # A short private path fits macOS's Unix socket address limit regardless of checkout location.
    with TemporaryDirectory(prefix="m5-", dir="/tmp") as directory, socket.socket(socket.AF_UNIX) as listener:
        address = str(Path(directory) / "launch")
        listener.bind(address)
        listener.listen(1)
        listener.setblocking(False)
        command = shlex.join(["/usr/bin/env", f"PYTHONPATH={pythonpath}", "PYTHONUNBUFFERED=1",
                              sys.executable, "-m", "ft_system.lfd.terminal", address, *argv])
        # Replace the terminal's shell with the server itself. tee copies output
        # but never owns the foreground job that receives interrupts and hangups.
        foreground = f"exec {command} > >(/usr/bin/tee -ai {shlex.quote(str(log_path))}) 2>&1"
        try:
            async with asyncio.timeout(30):
                await _open_window("exec " + shlex.join(["/bin/bash", "-c", foreground]), title)
                peer, _ = await asyncio.get_running_loop().sock_accept(listener)
                reader, writer = await asyncio.open_unix_connection(sock=peer)
                try:
                    pid = int.from_bytes(await reader.readexactly(8), "big")
                    writer.write(b"+")
                    await writer.drain()
                except BaseException:
                    await close_writer(writer)
                    raise
                return TerminalReplica(pid, reader, writer)
        except asyncio.IncompleteReadError as exc:
            raise OSError("replica window closed during launch") from exc
        except TimeoutError as exc:
            raise TimeoutError("timed out opening the replica in Terminal") from exc


if __name__ == "__main__":
    # exec preserves both PID and foreground process group. A late window whose
    # LFD has given up cannot start a second server without the acknowledgement.
    with socket.socket(socket.AF_UNIX) as owner:
        owner.connect(sys.argv[1])
        owner.sendall(os.getpid().to_bytes(8, "big"))
        if owner.recv(1) == b"+":
            owner.set_inheritable(True)
            os.execv(sys.argv[2], sys.argv[2:])
