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

import psutil

from ft_system.blackbox import collector
from ft_system.blackbox.cluster import ClusterError, LocalCluster, kill_tree, launch
from ft_system.blackbox.collector import Store
from ft_system.blackbox.dashboard import DEFAULT_PORT as DASHBOARD_PORT
from ft_system.blackbox.dashboard import Dashboard
from ft_system.blackbox.diagnose import and_join
from ft_system.blackbox.faults import ctl
from ft_system.blackbox.fingerpoint import FAULT, HEALTHY, WORKLOAD
from ft_system.blackbox.recover import RecoveryPolicy
from ft_system.common.logging import EventLogger

DEFAULT_CONFIG = "configs/ec.active.local.toml"
DEFAULT_TARGET = "S2"
DEMO_DUTY = 0.75  # busy fraction of the injected fault: plainly slow, yet no heartbeat is missed
THREADS_SHOWN = 6


def thread_times(pid: int) -> dict[int, float]:
    """CPU seconds used so far by each thread of a process and of the processes it started."""

    root = psutil.Process(pid)
    return {thread.id: thread.user_time + thread.system_time
            for process in (root, *root.children(recursive=True)) for thread in process.threads()}


class Session:
    def __init__(self, run_dir: str | Path, baseline: str | Path, config: str | Path = DEFAULT_CONFIG,
                 profile: str = "paper", interval: float = 0.2, clients: int = 3,
                 collector_port: int = collector.DEFAULT_PORT, dashboard_port: int = DASHBOARD_PORT,
                 logger: EventLogger | None = None, auto_recover: bool = False) -> None:
        self.run_dir, self.baseline, self.config = Path(run_dir).resolve(), Path(baseline), config
        self.profile, self.interval, self.clients = profile, interval, clients
        self.collector_port, self.dashboard_port = collector_port, dashboard_port
        self.logger = logger or EventLogger()
        self.stopping = asyncio.Event()
        self.auto_recover = auto_recover
        self.policy = RecoveryPolicy()
        self._recovery_note = ""
        self._threads: dict | None = None     # CPU by thread of the diagnosed replica
        self._tasks: list[asyncio.Task] = []
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
        self.dashboard = Dashboard(signature, diagnoser, self.run_dir, self.actions(), self.controls(), self.info)
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
        self._tasks = [asyncio.ensure_future(self._recover()), asyncio.ensure_future(self._watch_threads())]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
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
            ("rejuvenate", self.rejuvenate), ("auto", self.auto), ("down", self.down))}

    def info(self) -> dict:
        """What the page should know about this session, sent with every frame."""

        return {"auto_recover": self.auto_recover, "recovery": self._recovery_note, "threads": self._threads}

    def controls(self) -> dict:
        servers = list(self.cluster.servers)
        return {"targets": servers, "default": DEFAULT_TARGET if DEFAULT_TARGET in servers else servers[0],
                "buttons": [{"label": "Inject fault", "action": "inject", "target": True},
                            {"label": "Clear fault", "action": "clear", "target": True},
                            {"label": "Restart replica", "action": "rejuvenate", "target": True},
                            {"label": "Double the load", "action": "load", "argument": str(2 * self.clients)},
                            {"label": "Normal load", "action": "load", "argument": str(self.clients)},
                            {"label": "Auto-recovery on", "action": "auto", "argument": "on"},
                            {"label": "Auto-recovery off", "action": "auto", "argument": "off"}]}

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
        return self._restart(server, "restart requested")

    def _restart(self, server: str, reason: str) -> str:
        ready = self.cluster.view()[1]
        if ready != set(self.cluster.servers):
            raise ClusterError(f"not restarting {server}: only {and_join(sorted(ready)) if ready else 'no replica'} "
                               "ready, and the group must not lose a second one")
        pid = self.cluster.kill_replica(server)
        self.policy.restarted(time.time())
        ctl.record(self.run_dir, {"t": time.time(), "event": "replica_killed", "server": server, "pid": pid,
                                  "reason": reason})
        return f"{server} (pid {pid}) killed; the RM relaunches it"

    def auto(self, argument: str) -> str:
        """``on`` or ``off``: may the session restart a diagnosed replica by itself?"""

        if argument not in ("on", "off"):
            raise ValueError("say on or off")
        self.auto_recover = argument == "on"
        return f"automatic recovery {argument}"

    async def _recover(self) -> None:
        """Restart the diagnosed replica when automatic recovery is on and the policy allows it."""

        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.5)
            if not self.auto_recover:
                self._recovery_note = ""
                continue
            server, why = self.policy.decide(time.time(), self.dashboard.status, self.cluster.view()[1],
                                             self.cluster.servers)
            self._recovery_note = why
            if server is None:
                continue

            def restart(target: str = server) -> str:
                with self._busy:
                    return self._restart(target, "automatic recovery")

            try:
                self.logger.event(f"RECOVER: {why}: {await loop.run_in_executor(None, restart)}")
            except ClusterError as exc:
                self._recovery_note = str(exc)

    async def _watch_threads(self) -> None:
        """While a replica is diagnosed, measure which of its threads use the CPU."""

        loop = asyncio.get_running_loop()
        watched, before, moment = None, {}, 0.0
        while True:
            await asyncio.sleep(2)
            culprit = self.dashboard.status.get("culprit") if self.dashboard.status["state"] == FAULT else None
            sample = self.dashboard.latest(culprit) if culprit else None
            pid = sample.get("pid") if sample else None
            if pid is None:
                watched, self._threads = None, None
                continue
            try:
                after, now = await loop.run_in_executor(None, thread_times, pid), time.monotonic()
            except psutil.Error:
                continue
            if watched == pid:
                used = sorted(((100 * (seconds - before.get(thread, seconds)) / (now - moment), thread)
                               for thread, seconds in after.items()), reverse=True)
                self._threads = {"server": culprit, "count": len(after),
                                 "threads": [{"id": thread, "cpu": round(cpu, 1)} for cpu, thread in used[:THREADS_SHOWN]]}
            watched, before, moment = pid, after, now

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
    replica must bring everything back to normal. Then the fault is injected again with
    automatic recovery on, and the session must repair it by itself.
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

    # The same fault again, this time with nobody pressing the restart button.
    await act("auto", "on")
    before, injected = last()["servers"][target]["incarnation"], time.monotonic()
    await act("inject", target)
    await until(lambda: state() == FAULT and board.status["culprit"] == target, 40, "the second fault to be diagnosed")
    note(f"diagnosed again; automatic recovery says: {session.info()['recovery']}")
    await until(lambda: state() == HEALTHY and target in last()["servers"]
                and last()["servers"][target]["incarnation"] != before
                and last()["ft"] is not None and set(last()["ft"]["ready"]) == servers,
                150, f"{target} to be restarted automatically")
    timings["recovered automatically"] = time.monotonic() - injected
    expect(any(event["text"] == f"{target} killed (automatic recovery)" for event in board.events),
           "the restart was not recorded as automatic")
    note(f"{target} restarted without being asked; normal {timings['recovered automatically']:.1f} s after the "
         "injection, which includes waiting out the minute between restarts")
    await act("auto", "off")
    return timings
