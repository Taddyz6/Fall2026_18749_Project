"""Run the whole Milestone 5 system on this computer for diagnosis experiments.

RM, GFD, one LFD per replica (each launching its own server) and the clients,
started with the same commands as the demo. The only difference is the LFDs'
environment, which carries the fault shim into the servers they launch.

Needs ``psutil``: stopping an LFD must also stop the server it launched, and on
Windows that means walking the process tree.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

from ft_system.blackbox.faults import ctl
from ft_system.common.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_COMPONENT = re.compile(r"^ft_system\.(rm|gfd|lfd|server|client)\.main$")
_VIEW = re.compile(r"RM: view \d+: members: (.*?); ready: (.*)")


class ClusterError(RuntimeError):
    pass


def launch(module: str, *arguments: str, log: Path, shim_dir: Path | None = None) -> subprocess.Popen:
    """Start ``python -m module`` from the checkout, with its output appended to ``log``.

    ``shim_dir`` is the fault directory; give it only for an LFD, whose replica
    should carry the fault shim.
    """

    source = str(PROJECT_ROOT / "src")
    environment = os.environ | {
        "PYTHONPATH": os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH")))),
        "PYTHONUNBUFFERED": "1",
    }
    if shim_dir is not None:
        environment = ctl.shim_environment(shim_dir, environment)
    with log.open("ab") as output:
        return subprocess.Popen([sys.executable, "-m", module, *arguments], cwd=PROJECT_ROOT,
                                env=environment, stdout=output, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)


def component_processes() -> list[psutil.Process]:
    """Every running process of the fault-tolerant system on this computer."""

    found = []
    for process in psutil.process_iter(["cmdline"]):
        if any(_COMPONENT.match(arg) for arg in process.info["cmdline"] or ()):
            found.append(process)
    return found


def kill_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
        family = [root, *root.children(recursive=True)]
    except psutil.Error:
        return
    for process in family:
        try:
            process.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(family, timeout=5)


class LocalCluster:
    """All processes of one local run; its logs and fault files share ``run_dir``."""

    def __init__(self, run_dir: str | Path, config: str | Path = "configs/m5.active.local.toml",
                 client_interval: float = 0.2, clients: int | None = None, shim: bool = True) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.config_path = (PROJECT_ROOT / config).resolve()
        self.config = load_config(self.config_path)
        self.client_interval = client_interval
        self.shim = shim  # False runs the system exactly as the M5 demo does, with nothing added
        self.servers = tuple(self.config.servers)
        self.client_ids = tuple(self.config.clients)
        self.clients = len(self.client_ids) if clients is None else clients  # how many run at the start
        self._base: tuple[str, ...] = ()  # the clients that run for the whole session
        self.lfd_of = {lfd.server_id: lfd_id for lfd_id, lfd in self.config.lfds.items()}
        self._processes: dict[str, subprocess.Popen] = {}

    def __enter__(self) -> LocalCluster:
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    @property
    def config_hash(self) -> str:
        """Fingerprint of the config's contents: the same file name can hold a different system.

        A server logs one counter per configured client with every request, so even the
        number of clients in the config changes what its normal I/O looks like.
        """

        text = self.config_path.read_text(encoding="utf-8")  # read as text: line endings do not count
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    def log_path(self, name: str) -> Path:
        return self.run_dir / f"{name}.log"

    def log_text(self, name: str) -> str:
        try:
            return self.log_path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _launch(self, name: str, module: str, *arguments: str, shim: bool = False) -> None:
        self._processes[name] = launch(
            module, "--config", str(self.config_path), *arguments, log=self.log_path(name),
            shim_dir=self.run_dir if shim and self.shim else None)

    def view(self) -> tuple[set[str], set[str]]:
        """Members and ready replicas in the RM's latest view."""

        views = _VIEW.findall(self.log_text("RM"))
        if not views:
            return set(), set()
        return tuple({name.strip() for name in part.split(",")} - {"-"} for part in views[-1])

    def server_processes(self) -> dict[str, psutil.Process]:
        """The running server processes by label, found the way the agent finds them."""

        from ft_system.blackbox.discover import identify

        found = {}
        for process in component_processes():
            identity = identify(process.info["cmdline"] or ())
            if identity and identity[0] == "server" and str(self.config_path) in process.info["cmdline"]:
                found[identity[1]] = process
        return found

    def wait_ready(self, servers, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while not set(servers) <= self.view()[1]:
            dead = [name for name, process in self._processes.items() if process.poll() is not None]
            if dead:
                raise ClusterError(f"{', '.join(dead)} exited; see {self.run_dir}")
            if time.monotonic() >= deadline:
                raise ClusterError(f"{sorted(servers)} not all ready after {timeout:g} s; "
                                   f"RM view: {self.view()}")
            time.sleep(0.1)

    def start(self) -> None:
        running = component_processes()
        if running:
            raise ClusterError("the fault-tolerant system is already running here (pids "
                               f"{', '.join(str(process.pid) for process in running)}); stop it first")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._launch("RM", "ft_system.rm.main")
            time.sleep(1)
            self._launch("GFD", "ft_system.gfd.main")
            time.sleep(1)
            for count, server_id in enumerate(self.servers, start=1):
                self._launch(self.lfd_of[server_id], "ft_system.lfd.main", "--lfd-id",
                             self.lfd_of[server_id], "--replica-log", str(self.log_path(server_id)),
                             shim=True)
                self.wait_ready(self.servers[:count])
            self.set_clients(self.clients)
            self._base = self.client_ids[:self.clients]
        except BaseException:
            self.stop()
            raise

    def running_clients(self) -> list[str]:
        return [client_id for client_id in self.client_ids
                if client_id in self._processes and self._processes[client_id].poll() is None]

    def set_clients(self, count: int) -> None:
        """Change how many clients run: start spare ones that have not run yet, or stop spares.

        A client is never started twice in one session: it would number its requests
        from 1 again, and the servers would take them for duplicates. The clients that
        ran from the start are never stopped.
        """

        running = self.running_clients()
        if count > len(running):
            unused = [client_id for client_id in self.client_ids if client_id not in self._processes]
            if count - len(running) > len(unused):
                raise ClusterError(f"only {len(unused)} unused spare clients are left in this session; "
                                   f"cannot run {count}")
            for client_id in unused[:count - len(running)]:
                self._launch(client_id, "ft_system.client.main", "--client-id", client_id,
                             "--interval", str(self.client_interval))
        else:
            spares = [client_id for client_id in running if client_id not in self._base]
            if len(running) - count > len(spares):
                raise ClusterError(f"cannot run fewer than the {len(self._base)} clients the session started with")
            for client_id in spares[count - len(running):] if count < len(running) else ():
                kill_tree(self._processes[client_id].pid)

    def replica_pid(self, server_id: str) -> int | None:
        """PID of the running server process, as its shim registered it."""

        state = ctl.replicas(self.run_dir).get(server_id)
        try:
            # The PID could have passed to an unrelated process since the shim wrote it.
            command = psutil.Process(state["pid"]).cmdline() if state else ()
        except psutil.Error:
            return None
        return state["pid"] if "--server-id" in command and server_id in command else None

    def kill_replica(self, server_id: str) -> int:
        pid = self.replica_pid(server_id)
        if pid is None:
            raise ClusterError(f"{server_id} is not running")
        kill_tree(pid)
        return pid

    def stop(self) -> None:
        # RM and GFD first, so no LFD is told to relaunch a replica during teardown.
        for name in sorted(self._processes, key=lambda name: name not in ("RM", "GFD")):
            if self._processes[name].poll() is None:
                kill_tree(self._processes[name].pid)
        self._processes.clear()
        # A replica whose LFD died first is no longer in any tree we started.
        for process in component_processes():
            if str(self.config_path) in (process.info["cmdline"] or ()):
                kill_tree(process.pid)


def sweep() -> list[int]:
    """Kill every process of the system and of the monitor on this computer; return their PIDs.

    For cleaning up after a session that did not shut down. It does not ask whose they are.
    """

    monitor = ("ft_system.blackbox.agent", "ft_system.blackbox.collector")
    found = component_processes()
    for process in psutil.process_iter(["cmdline"]):
        command = process.info["cmdline"] or ()
        if "-m" in command and any(argument in monitor for argument in command):
            found.append(process)
    for process in found:
        kill_tree(process.pid)
    return [process.pid for process in found]
