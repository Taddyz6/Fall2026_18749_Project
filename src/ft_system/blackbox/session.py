"""One live session on this computer: the system, the monitor, the dashboard and the controls.

``scripts/ec_run.py`` drives it. Everything the demo does to the running system
goes through the session's actions, whether from a button on the page or from
another terminal, so each one is recorded as ground truth in the same way.

Needs psutil (through the cluster and the agent).
"""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ft_system.blackbox import collector
from ft_system.blackbox.cluster import ClusterError, LocalCluster, kill_tree, launch
from ft_system.blackbox.collector import Store
from ft_system.blackbox.dashboard import DEFAULT_PORT as DASHBOARD_PORT
from ft_system.blackbox.dashboard import Dashboard
from ft_system.blackbox.diagnose import and_join
from ft_system.blackbox.faults import ctl
from ft_system.blackbox.fingerpoint import FAULT, HEALTHY, WORKLOAD
from ft_system.common.logging import EventLogger

DEFAULT_CONFIG = "configs/ec.active.local.toml"
DEFAULT_TARGET = "S2"
DEMO_DUTY = 0.75  # busy fraction of the injected fault: plainly slow, yet no heartbeat is missed


class Session:
    def __init__(self, run_dir: str | Path, baseline: str | Path, config: str | Path = DEFAULT_CONFIG,
                 profile: str = "paper", interval: float = 0.2, clients: int = 3,
                 collector_port: int = collector.DEFAULT_PORT, dashboard_port: int = DASHBOARD_PORT,
                 logger: EventLogger | None = None) -> None:
        self.run_dir, self.baseline, self.config = Path(run_dir).resolve(), Path(baseline), config
        self.profile, self.interval, self.clients = profile, interval, clients
        self.collector_port, self.dashboard_port = collector_port, dashboard_port
        self.logger = logger or EventLogger()
        self.stopping = asyncio.Event()
        self.cluster: LocalCluster | None = None
        self.dashboard: Dashboard | None = None
        self.store: Store | None = None
        self._agent = None
        self._servers: list[asyncio.Server] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._busy = threading.Lock()  # actions come from several threads; the cluster takes one at a time

    async def start(self) -> None:
        """Start the collector, the dashboard, the system and the agent, in that order."""

        self._loop = asyncio.get_running_loop()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.run_dir)
        signature, diagnoser = collector.attach_diagnosis(self.store, self.baseline, self.profile, self.logger)
        self.cluster = LocalCluster(self.run_dir, self.config, self.interval, self.clients)
        self.dashboard = Dashboard(signature, diagnoser, self.run_dir, self.actions(), self.controls())
        self.store.subscribe(self.dashboard.observe)
        self._servers.append(await collector.start(self.store, "127.0.0.1", self.collector_port, self.logger))
        self._servers.append(await self.dashboard.start("127.0.0.1", self.dashboard_port))
        await self._loop.run_in_executor(None, self.cluster.start)  # takes a while; the page stays up
        self._agent = launch("ft_system.blackbox.agent", "--collector", f"127.0.0.1:{self.collector_port}",
                             "--volume", str(self.run_dir), log=self.run_dir / "agent.log")
        replication = self.cluster.config.replication
        ctl.record(self.run_dir, {"t": time.time(), "event": "recording_started", "config": str(self.config),
                                  "mode": replication.mode if replication else None,
                                  "client_interval": self.interval, "clients": self.clients})

    async def stop(self) -> None:
        if self._agent is not None and self._agent.poll() is None:
            kill_tree(self._agent.pid)
        if self.cluster is not None:
            await asyncio.get_running_loop().run_in_executor(None, self.cluster.stop)
        for server in self._servers:
            server.close()  # not waited for: an open page would keep it waiting
        if self.store is not None:
            self.store.close()

    # --- what can be done to the running system ---

    def actions(self) -> dict[str, Callable[[str], str]]:
        def one_at_a_time(action: Callable[[str], str]) -> Callable[[str], str]:
            def run(argument: str) -> str:
                with self._busy:
                    return action(argument.strip())
            return run

        return {name: one_at_a_time(action) for name, action in (
            ("inject", self.inject), ("clear", self.clear), ("load", self.load),
            ("rejuvenate", self.rejuvenate), ("down", self.down))}

    def controls(self) -> dict:
        servers = list(self.cluster.servers)
        return {"targets": servers, "default": DEFAULT_TARGET if DEFAULT_TARGET in servers else servers[0],
                "buttons": [{"label": "Inject fault", "action": "inject", "target": True},
                            {"label": "Clear fault", "action": "clear", "target": True},
                            {"label": "Restart replica", "action": "rejuvenate", "target": True},
                            {"label": "Double the load", "action": "load", "argument": str(2 * self.clients)},
                            {"label": "Normal load", "action": "load", "argument": str(self.clients)}]}

    def inject(self, argument: str) -> str:
        """``S2`` or ``S2:0.5``: start the CPU fault in that replica."""

        server, _, duty = argument.partition(":")
        state = ctl.inject(self.run_dir, server, duty=float(duty) if duty else DEMO_DUTY)
        return f"{server}: {ctl.describe(state['applied'])}"

    def clear(self, argument: str) -> str:
        ctl.clear(self.run_dir, argument)
        return f"{argument}: fault cleared"

    def load(self, argument: str) -> str:
        """Run this many clients."""

        count = int(argument)
        self.cluster.set_clients(count)
        ctl.record(self.run_dir, {"t": time.time(), "event": "load_changed", "clients": count})
        return f"{count} clients running"

    def rejuvenate(self, argument: str) -> str:
        """Kill a replica so that the RM relaunches it: the named one, or the one now diagnosed.

        Refused unless every replica is ready, so that recovery never takes the group
        below two working replicas.
        """

        server = argument or self.dashboard.status.get("culprit")
        if not server:
            raise ClusterError("no replica is diagnosed now; name the one to restart")
        ready = self.cluster.view()[1]
        if ready != set(self.cluster.servers):
            raise ClusterError(f"not restarting {server}: only {and_join(sorted(ready)) if ready else 'no replica'} "
                               "ready, and the group must not lose a second one")
        pid = self.cluster.kill_replica(server)
        ctl.record(self.run_dir, {"t": time.time(), "event": "replica_killed", "server": server, "pid": pid,
                                  "reason": "rejuvenation"})
        return f"{server} (pid {pid}) killed; the RM relaunches it"

    def down(self, argument: str) -> str:
        self._loop.call_soon_threadsafe(self.stopping.set)
        return "shutting down"


def send(action: str, argument: str = "", port: int = DASHBOARD_PORT, timeout: float = 30.0) -> tuple[bool, str]:
    """Ask the running session to do something; ``(done, what it answered)``.

    Raises ``ConnectionError`` if no session is listening.
    """

    request = urllib.request.Request(f"http://127.0.0.1:{port}/action/{action}", data=argument.encode(),
                                     method="POST", headers={"X-Dashboard": "1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return True, reply.read().decode()
    except urllib.error.HTTPError as exc:
        return False, exc.read().decode()
    except OSError as exc:
        raise ConnectionError(f"no session is listening on port {port}: {exc}") from exc


class StepFailed(RuntimeError):
    """The scripted demo did not see what it expects."""


async def autopilot(session: Session, say: Callable[[str], None], target: str = DEFAULT_TARGET) -> dict[str, float]:
    """Run the demo unattended and check each step; return how long each reaction took.

    A load change must be reported as shared, a fault must be pinned on its replica with
    the right cause while the fault-tolerance layer still sees nothing, and restarting the
    replica must bring everything back to normal.
    """

    board, cluster = session.dashboard, session.cluster
    servers, began, timings = set(cluster.servers), time.monotonic(), {}

    def note(text: str) -> None:
        say(f"[autopilot +{time.monotonic() - began:5.1f} s] {text}")

    def state() -> str:
        return board.status["state"]

    def last() -> dict:
        return board.frames[-1]

    async def until(condition: Callable[[], bool], timeout: float, what: str) -> float:
        start = time.monotonic()
        while not condition():
            if time.monotonic() - start > timeout:
                raise StepFailed(f"waited {timeout:g} s for {what}; the page shows: {board.status['headline']}")
            await asyncio.sleep(0.25)
        return time.monotonic() - start

    async def act(name: str, argument: str) -> None:
        note(f"{name} {argument}".rstrip() + ": " + await asyncio.get_running_loop().run_in_executor(
            None, session.actions()[name], argument))

    def expect(condition: bool, what: str) -> None:
        if not condition:
            raise StepFailed(what)

    await until(lambda: len(board.frames) >= 20 and set(last()["servers"]) == servers and state() == HEALTHY
                and all(server["level"] for server in last()["servers"].values()), 90,
                "every replica to report and be judged")
    note("normal: every replica is inside its range")

    seen = len(board.events)
    await act("load", str(2 * session.clients))
    timings["load change reported"] = await until(lambda: state() == WORKLOAD, 40, "the load change to be reported")
    expect(not any(event["group"] == FAULT for event in list(board.events)[seen:]),
           "a replica was blamed for the load change")
    note(f"reported as a workload change after {timings['load change reported']:.1f} s; no replica blamed")
    await act("load", str(session.clients))
    await until(lambda: state() == HEALTHY, 50, "normal after the load returned")
    note("normal again")

    await act("inject", target)
    timings["fault diagnosed"] = await until(lambda: state() == FAULT, 40, "the fault to be diagnosed")
    status, layer = board.status, last()["ft"]
    expect((status["culprit"], status["cause"]) == (target, "cpu_task"),
           f"diagnosed {status['culprit']} as {status['cause']}, not {target} as cpu_task")
    expect(layer is not None and set(layer["ready"]) == servers and layer["timeouts"] == 0,
           f"the fault-tolerance layer noticed the fault: {layer}")
    note(f"diagnosed after {timings['fault diagnosed']:.1f} s: {status['headline']}; meanwhile the RM reports "
         f"{len(layer['ready'])} ready and the LFDs no heartbeat timeout")

    before = last()["servers"][target]["incarnation"]
    await act("rejuvenate", "")
    timings["recovered"] = await until(
        lambda: state() == HEALTHY and target in last()["servers"]
        and last()["servers"][target]["incarnation"] != before
        and last()["ft"] is not None and set(last()["ft"]["ready"]) == servers,
        90, f"{target} to be relaunched and ready")
    expect(ctl.replicas(session.run_dir)[target]["applied"] is None, "the relaunched replica still runs the fault")
    note(f"{target} relaunched, ready and normal {timings['recovered']:.1f} s after it was killed")

    await asyncio.sleep(15)
    expect(state() == HEALTHY, f"did not stay normal: {board.status['headline']}")
    note("still normal 15 s later")
    return timings
