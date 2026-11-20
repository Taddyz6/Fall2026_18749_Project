"""Collection agent: once a second, report operating-system metrics for every
process of the fault-tolerant system on this computer, and for the computer.

    python -m ft_system.blackbox.agent --collector 127.0.0.1:8749
    python -m ft_system.blackbox.agent --stdout        # print the records instead

Black-box: it uses only what the OS exposes about any process (CPU time, memory,
threads, context switches, I/O counters), and it finds the processes by their
command lines. Needs psutil.

One JSON object per line:

    {"type": "sample", "label": "S2", "role": "server", "pid": 4242, "incarnation": 2,
     "age": 31.0, "m": {"cpu": 71.9, "rss_mb": 27.0, "threads": 10, ...}, ...}
    {"type": "sample", "label": "host:NAME", "role": "host", "m": {...}, ...}
    {"type": "event", "event": "process_started", "label": "S2", "pid": 4242, ...}

``cpu`` values are percent of one core. Names ending in ``_s`` are per second.
Both are averages over the time since the previous reading. Thread count and
context switches are read every other second and repeated in between.
"""

from __future__ import annotations

import argparse
import json
import platform
import secrets
import socket
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence

import psutil

from ft_system.blackbox.collector import DEFAULT_PORT
from ft_system.blackbox.discover import Seen, group, identify

# Thread count and context switches cost a system-wide snapshot per process on
# Windows (about 4 ms each here; two thirds of the agent's CPU when read every
# tick). So only the processes under diagnosis get them, every DETAIL_EVERY ticks.
FULL_ROLES = frozenset({"server"})
DETAIL_EVERY = 2
MIN_AGE = 0.3       # seconds; a forked child shows its parent's command line until it execs
RESCAN_EVERY = 30   # ticks between full rescans, because the OS hands old PIDs to new processes
SEARCH_SECONDS = 30  # after a component exits, rescan every tick this long to catch its successor
SLOW_EVERY = 5      # ticks between readings of the slow host counters (network)


def rates_between(before: Mapping[str, float], after: Mapping[str, float],
                  elapsed: float) -> dict[str, float] | None:
    """Per-second rates from two readings of cumulative counters.

    CPU seconds become percent of one core. A counter that went backwards is left
    out: Windows counts context switches over a process's live threads only, so
    the total drops when a thread exits.
    """

    if elapsed <= 0 or before.keys() != after.keys():
        return None
    rates = {}
    for name, value in after.items():
        change = value - before[name]
        if change < 0:
            continue
        if name.startswith("cpu"):
            rates[name] = round(100 * change / elapsed, 2)
        else:
            rates[f"{name}_s"] = round(change / elapsed, 2)
    return rates


def read(members: Sequence[psutil.Process]) -> tuple[dict[str, float], dict[str, float]]:
    """Cumulative counters and current gauges, summed over a component's processes."""

    counters: dict[str, float] = {}
    gauges: dict[str, float] = {}

    def add(target: dict[str, float], name: str, value: float) -> None:
        target[name] = target.get(name, 0) + value

    for process in members:
        with process.oneshot():
            times = process.cpu_times()
            add(counters, "cpu_user", times.user)
            add(counters, "cpu_sys", times.system)
            add(gauges, "rss_mb", process.memory_info().rss / 2**20)
            if hasattr(process, "io_counters"):  # macOS has no per-process I/O counters
                for name, value in process.io_counters()._asdict().items():
                    add(counters, f"io_{name}", value)
    return counters, gauges


def read_detail(members: Sequence[psutil.Process]) -> tuple[dict[str, float], dict[str, float]]:
    """The costly pair: context-switch counters and the thread count, summed likewise."""

    counters = {"ctx_voluntary": 0, "ctx_involuntary": 0}
    threads = 0
    for process in members:
        with process.oneshot():
            threads += process.num_threads()
            switches = process.num_ctx_switches()
            counters["ctx_voluntary"] += switches.voluntary
            counters["ctx_involuntary"] += switches.involuntary
    return counters, {"threads": threads}


class Component:
    """One labelled process of the system (S2, LFD2, C1, RM ...) across its restarts."""

    def __init__(self, label: str, role: str) -> None:
        self.label, self.role = label, role
        self.incarnation = 0
        self.root: Seen | None = None
        self.members: list[psutil.Process] = []
        self.reference: tuple[float, dict[str, float], tuple[int, ...]] | None = None
        self.detail_reference: tuple[float, dict[str, float], tuple[int, ...]] | None = None
        self.detail: dict[str, float] = {}  # the latest thread count and context-switch rates
        self.missing_since: float | None = None

    def forget(self) -> None:
        """Drop the reference points: the next readings are of a different process."""

        self.reference = self.detail_reference = None
        self.detail = {}


class Agent:
    def __init__(self, host: str | None = None, volume: str = ".", interval: float = 1.0,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.host = host or socket.gethostname()
        self.volume, self.interval = volume, interval
        self.clock, self.monotonic = clock, monotonic
        self._session = f"{self.host}/{secrets.token_hex(4)}"
        self._seq = self._ticks = 0
        self._found: dict[int, tuple[psutil.Process, Seen]] = {}
        self._ignored: set[int] = set()
        self._components: dict[str, Component] = {}
        self._self = psutil.Process()
        self._host_reference: tuple[float, dict[str, float]] | None = None
        self._network_reference: tuple[float, dict[str, float]] | None = None
        self._network_rates: dict[str, float] = {}
        psutil.cpu_percent(percpu=True)  # the first call only sets the reference point

    def _record(self, kind: str, **fields) -> dict:
        # The collector uses (agent, seq) to drop a batch it receives twice.
        self._seq += 1
        return {"type": kind, "agent": self._session, "seq": self._seq,
                "t": round(self.clock(), 3), "host": self.host, **fields}

    def _discover(self) -> list[dict]:
        """Update which processes belong to which component; return start and exit events."""

        alive = set(psutil.pids())
        self._found = {pid: found for pid, found in self._found.items()
                       if pid in alive and found[0].is_running()}
        moment, now = self.monotonic(), self.clock()
        searching = any(component.missing_since is not None
                        and moment - component.missing_since < SEARCH_SECONDS
                        for component in self._components.values())
        # Only unfamiliar PIDs are examined, so a new process that inherited a PID
        # we had dismissed is found by the next full rescan.
        self._ignored = set() if searching or self._ticks % RESCAN_EVERY == 0 else self._ignored & alive
        for pid in alive - self._found.keys() - self._ignored:
            try:
                process = psutil.Process(pid)
                # Command line first: for a process we may not inspect it fails at once,
                # whereas create_time() would fall back to a slow system-wide query.
                identity = identify(process.cmdline())
                started = process.create_time()
                if now - started < MIN_AGE:
                    continue  # look again at the next tick
                if identity is not None:
                    self._found[pid] = (process, Seen(pid, process.ppid(), started, *identity))
                    continue
            except psutil.Error:
                pass
            self._ignored.add(pid)

        events = []
        families = group(seen for _, seen in self._found.values())
        for label, family in families.items():
            component = self._components.setdefault(label, Component(label, family[0].role))
            if component.root != family[0]:
                component.incarnation += 1
                component.root, component.missing_since = family[0], None
                component.forget()
                events.append(self._record(
                    "event", event="process_started", label=label, role=component.role,
                    pid=family[0].pid, incarnation=component.incarnation,
                    started=round(family[0].started, 3)))
            component.members = [self._found[member.pid][0] for member in family]
        for label, component in self._components.items():
            if label not in families and component.root is not None:
                events.append(self._record(
                    "event", event="process_exited", label=label, role=component.role,
                    pid=component.root.pid, incarnation=component.incarnation))
                component.root, component.members, component.missing_since = None, [], moment
                component.forget()
        return events

    def _host(self, moment: float) -> dict | None:
        per_core = psutil.cpu_percent(percpu=True)
        memory = psutil.virtual_memory()
        counters = {"cpu_agent": sum(self._self.cpu_times()[:2])}
        disk = psutil.disk_io_counters()
        if disk is not None:
            counters |= {"disk_read_bytes": disk.read_bytes, "disk_write_bytes": disk.write_bytes}
        if self._ticks % SLOW_EVERY == 1:
            network = psutil.net_io_counters()
            reading = {"net_sent_bytes": network.bytes_sent, "net_recv_bytes": network.bytes_recv}
            if self._network_reference is not None:
                before, earlier = self._network_reference
                self._network_rates = rates_between(earlier, reading, moment - before) or {}
            self._network_reference = (moment, reading)
        reference, self._host_reference = self._host_reference, (moment, counters)
        rates = rates_between(reference[1], counters, moment - reference[0]) if reference else None
        if rates is None:
            return None
        rates["agent_cpu"] = rates.pop("cpu_agent")
        return self._record("sample", label=f"host:{self.host}", role="host", m=rates | self._network_rates | {
            "cpu": round(sum(per_core) / len(per_core), 2),
            "cpu_max_core": max(per_core),
            "mem_available_mb": round(memory.available / 2**20, 1),
            "mem_used_pct": memory.percent,
            "swap_used_mb": round(psutil.swap_memory().used / 2**20, 1),
            "disk_free_mb": round(psutil.disk_usage(self.volume).free / 2**20, 1),
            "agent_rss_mb": round(self._self.memory_info().rss / 2**20, 1),
            "components": sum(1 for component in self._components.values() if component.members),
        })

    def _refresh_detail(self, component: Component, moment: float, pids: tuple[int, ...]) -> None:
        """Read the costly pair on its own slower schedule; between readings the last values stand."""

        reference = component.detail_reference
        if reference is not None and reference[2] != pids:
            reference, component.detail = None, {}
        if reference is not None and self._ticks % DETAIL_EVERY:
            return
        counters, gauges = read_detail(component.members)
        component.detail_reference = (moment, counters, pids)
        rates = rates_between(reference[1], counters, moment - reference[0]) if reference else None
        component.detail = (rates or {}) | gauges

    def tick(self) -> list[dict]:
        """Everything to report now: process events, one sample per component, one for the host."""

        records = []
        if self._ticks == 0:
            # What produced the numbers: a signature only holds for the same kind of machine.
            records.append(self._record(
                "event", event="agent_started", os=platform.platform(),
                python=platform.python_version(), psutil=psutil.__version__,
                cores=psutil.cpu_count(), interval=self.interval))
        records += self._discover()
        self._ticks += 1
        moment = self.monotonic()
        for component in self._components.values():
            if not component.members:
                continue
            pids = tuple(process.pid for process in component.members)
            try:
                counters, gauges = read(component.members)
                if component.role in FULL_ROLES:
                    self._refresh_detail(component, moment, pids)
            except psutil.Error:
                component.forget()  # it exited mid-read; discovery catches up next tick
                continue
            reference, component.reference = component.reference, (moment, counters, pids)
            if reference is None or reference[2] != pids:
                continue  # first reading of this set of processes: nothing to compare with yet
            rates = rates_between(reference[1], counters, moment - reference[0])
            if rates is None:
                continue
            rates["cpu"] = round(rates["cpu_user"] + rates["cpu_sys"], 2)
            records.append(self._record(
                "sample", label=component.label, role=component.role, pid=component.root.pid,
                incarnation=component.incarnation,
                age=round(self.clock() - component.root.started, 1),
                m=rates | {name: round(value, 2) for name, value in gauges.items()} | component.detail))
        host = self._host(moment)
        if host is not None:
            records.append(host)
        return records


class Sender:
    """Deliver lines to the collector in order.

    While the collector is unreachable, the newest ``capacity`` lines wait here.
    A batch that fails part-way is sent again in full; the collector drops what
    it already has. A batch in flight when the collector dies can still be lost.
    """

    RETRY_SECONDS = 5.0

    def __init__(self, address: tuple[str, int], capacity: int = 3000, timeout: float = 0.5) -> None:
        self.address, self.timeout = address, timeout
        self._pending: deque[bytes] = deque(maxlen=capacity)
        self._socket: socket.socket | None = None
        self._retry_at = 0.0

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def send(self, lines: Sequence[str]) -> bool:
        self._pending.extend(line.encode("utf-8") + b"\n" for line in lines)
        if self._socket is None:
            if time.monotonic() < self._retry_at:
                return False
            try:
                self._socket = socket.create_connection(self.address, timeout=self.timeout)
            except OSError:
                # Connecting must not stall sampling: try again only now and then.
                self._retry_at = time.monotonic() + self.RETRY_SECONDS
                return False
        try:
            self._socket.sendall(b"".join(self._pending))
        except OSError:
            self.close()
            return False
        self._pending.clear()
        return True


def parse_address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return host or "127.0.0.1", int(port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report OS metrics of the system's processes")
    parser.add_argument("--collector", type=parse_address, default=("127.0.0.1", DEFAULT_PORT),
                        metavar="HOST:PORT", help=f"Where the collector listens (default 127.0.0.1:{DEFAULT_PORT})")
    parser.add_argument("--stdout", action="store_true", help="Print records instead of sending them")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between samples (default 1)")
    parser.add_argument("--count", type=int, default=0, help="Stop after this many samples; 0 runs forever")
    parser.add_argument("--host-name", help="Name to report for this computer (default: its hostname)")
    parser.add_argument("--volume", default=".", help="Path on the disk whose free space is reported")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agent = Agent(args.host_name, args.volume, args.interval)
    sender = None if args.stdout else Sender(args.collector)
    deadline, done = time.monotonic(), 0
    try:
        while True:
            lines = [json.dumps(record, separators=(",", ":")) for record in agent.tick()]
            if sender is not None:
                sender.send(lines)
            elif lines:
                print("\n".join(lines), flush=True)
            done += 1
            if done == args.count:
                return 0
            deadline += args.interval
            pause = deadline - time.monotonic()
            if pause < 0:
                deadline = time.monotonic()  # fell behind (a suspended laptop): start a fresh schedule
            else:
                time.sleep(pause)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
