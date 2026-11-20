"""Run the whole Milestone 5 system on this computer for diagnosis experiments.

RM, GFD, one LFD per replica (each launching its own server) and the clients,
started with the same commands as the demo. The only difference is the LFDs'
environment, which carries the fault shim into the servers they launch.

Needs ``psutil``: stopping an LFD must also stop the server it launched, and on
Windows that means walking the process tree.
"""

from __future__ import annotations

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
                 client_interval: float = 0.2, clients: int | None = None) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.config_path = (PROJECT_ROOT / config).resolve()
        self.config = load_config(self.config_path)
        self.client_interval = client_interval
        self.servers = tuple(self.config.servers)
        self.client_ids = tuple(self.config.clients)
        self.clients = len(self.client_ids) if clients is None else clients  # how many run at the start
        self.lfd_of = {lfd.server_id: lfd_id for lfd_id, lfd in self.config.lfds.items()}
        self._processes: dict[str, subprocess.Popen] = {}

    def __enter__(self) -> LocalCluster:
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

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
            shim_dir=self.run_dir if shim else None)

    def view(self) -> tuple[set[str], set[str]]:
        """Members and ready replicas in the RM's latest view."""

        views = _VIEW.findall(self.log_text("RM"))
        if not views:
            return set(), set()
        return tuple({name.strip() for name in part.split(",")} - {"-"} for part in views[-1])

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
        except BaseException:
            self.stop()
            raise

    def set_clients(self, count: int) -> None:
        """Run exactly the first ``count`` configured clients: start or stop the rest.

        A stopped client must not be started again in the same run: it would number its
        requests from 1 again, and the servers would take them for duplicates.
        """

        if not 0 <= count <= len(self.client_ids):
            raise ClusterError(f"the config has {len(self.client_ids)} clients; cannot run {count}")
        for position, client_id in enumerate(self.client_ids):
            process = self._processes.get(client_id)
            running = process is not None and process.poll() is None
            if position < count and not running:
                if process is not None:
                    raise ClusterError(f"{client_id} already ran once in this run")
                self._launch(client_id, "ft_system.client.main", "--client-id", client_id,
                             "--interval", str(self.client_interval))
            elif position >= count and running:
                kill_tree(process.pid)
        self.clients = count

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
