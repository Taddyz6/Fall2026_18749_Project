"""Live dashboard: one web page with the replicas' metrics against their normal
range, the anomaly matrix and the current diagnosis.

    python -m ft_system.blackbox.collector --run-dir logs/ec/demo --baseline logs/ec/baseline.json --dashboard
    python -m ft_system.blackbox.dashboard --replay logs/ec/fault-ec --speed 5    # play back a recording

Then open http://127.0.0.1:8750/. The page is one self-contained file, so it
needs no internet. The server sends one frame a second over server-sent events.

Beside the diagnosis the page shows what the fault-tolerance layer itself
reports (the RM's view and LFD heartbeat timeouts, read from their logs when
they are in the run directory). That is for contrast only: the diagnosis never
reads it. Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ft_system.blackbox import impact
from ft_system.blackbox.baseline import ANOMALY_SIGMAS, DEFAULT_PATH, METRIC_NAMES, WARN_SIGMAS, Signature, spread
from ft_system.blackbox.collector import read_run, when
from ft_system.blackbox.detect import PROFILES
from ft_system.blackbox.diagnose import STALE_SECONDS, Diagnoser
from ft_system.blackbox.faults.ctl import EVENTS_FILE
from ft_system.blackbox.fingerpoint import HEALTHY
from ft_system.common.logging import EventLogger

DEFAULT_PORT = 8750
PAGE = Path(__file__).with_name("dashboard.html")
CHARTS = ("cpu", "ctx_voluntary_s", "io_write_count_s", "rss_mb")
MATRIX = ("cpu", "cpu_user", "cpu_sys", "ctx_voluntary_s", "io_write_count_s", "io_other_count_s",
          "rss_mb", "threads")
HISTORY = 900          # frames kept for a page that opens late: 15 minutes
TIMEOUT_WINDOW = 60.0  # seconds over which heartbeat timeouts are counted
# Things done to the system on purpose. They are written to the events file by
# whoever did them, not sent through the collector.
GROUND_TRUTH = ("fault_injected", "fault_cleared", "replica_killed", "load_changed")
_VIEW = re.compile(r"RM: view \d+: members: (.*?); ready: (.*)")


def describe(record: Mapping) -> dict | None:
    """The line the page shows for an event; ``None`` for an event it does not show."""

    kind = record.get("event")
    who = record.get("server") or record.get("label") or record.get("culprit")
    if kind == "diagnosis":
        group, text = record["status"], record["headline"]
    elif kind == "fault_injected":
        group, text = "action", f"fault injected into {who}"
    elif kind == "fault_cleared":
        group, text = "action", f"fault cleared in {who}"
    elif kind == "replica_killed":
        group, text = "action", f"{who} killed"
    elif kind == "load_changed":
        group, text = "action", f"client count set to {record.get('clients')}"
    elif kind == "process_exited" and record.get("role") == "server":
        group, text = "process", f"{who} exited"
    elif kind == "process_started" and record.get("role") == "server" and record.get("incarnation", 1) > 1:
        group, text = "process", f"{who} relaunched"
    else:
        return None
    return {"t": round(when(record), 3), "group": group, "text": text, "who": who}


class Tail:
    """Whole lines appended to a file since the last look."""

    def __init__(self, path: Path) -> None:
        self.path, self._offset = path, 0

    def lines(self) -> list[str]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return []
        end = chunk.rfind(b"\n") + 1  # leave a line that is still being written for next time
        self._offset += end
        return chunk[:end].decode("utf-8", errors="replace").splitlines()


class LogWatcher:
    """What the fault-tolerance layer reports about itself, from its own logs."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._rm = Tail(folder / "RM.log")
        self._lfds: dict[Path, Tail] = {}
        self._timeouts: deque[float] = deque()
        self.members: list[str] | None = None
        self.ready: list[str] = []

    def poll(self, now: float) -> dict | None:
        for line in self._rm.lines():
            match = _VIEW.search(line)
            if match:
                self.members, self.ready = ([name.strip() for name in part.split(",") if name.strip() != "-"]
                                            for part in match.groups())
        for path in self.folder.glob("LFD*.log"):
            tail = self._lfds.setdefault(path, Tail(path))
            for stamp, text in impact.timed_lines("\n".join(tail.lines())):
                if "heartbeat to" in text and "failed" in text:
                    self._timeouts.append(stamp)
        while self._timeouts and now - self._timeouts[0] > TIMEOUT_WINDOW:
            self._timeouts.popleft()
        if self.members is None:
            return None
        return {"members": self.members, "ready": self.ready, "timeouts": len(self._timeouts)}


class Dashboard:
    """Turns the records it is given into one frame a second, and serves the page.

    ``run_dir`` is where the events file and the system's logs are, when this runs
    beside a live system on the same computer. ``actions`` maps a name to a function
    taking one text argument; the page shows a button for each.
    """

    def __init__(self, signature: Signature, diagnoser: Diagnoser, run_dir: Path | None = None,
                 actions: Mapping[str, Callable[[str], str]] | None = None, role: str = "server") -> None:
        self.signature, self.diagnoser, self.role = signature, diagnoser, role
        self.actions = dict(actions or {})
        self.frames: deque[dict] = deque(maxlen=HISTORY)
        self.events: deque[dict] = deque(maxlen=300)
        self.status = {"state": HEALTHY, "headline": "Normal",
                       "detail": "every replica is inside its normal range", "since": None}
        self._latest: dict[str, Mapping] = {}
        self._listeners: set[asyncio.Queue] = set()
        self._truth = Tail(Path(run_dir) / EVENTS_FILE) if run_dir else None
        self._logs = LogWatcher(Path(run_dir)) if run_dir else None

    # --- records in ---

    def observe(self, record: Mapping) -> None:
        if "event" in record:
            self._event(record)
        elif record.get("type") != "sample":
            return
        elif record.get("role") == self.role:
            self._latest[record["label"]] = record
        elif record.get("role") == "host":
            # The host sample ends an agent's batch: every replica is up to date.
            now = when(record)
            for line in self._truth.lines() if self._truth else ():
                try:
                    done = json.loads(line)
                except ValueError:
                    continue
                if isinstance(done, dict) and done.get("event") in GROUND_TRUTH:
                    self._event(done)
            frame = self._frame(now, record)
            self.frames.append(frame)
            self._send({"type": "frame", "frame": frame})

    def _event(self, record: Mapping) -> None:
        if record.get("event") == "diagnosis":
            self.status = {"state": record["status"], "headline": record["headline"],
                           "detail": record["detail"], "action": record.get("action"),
                           "culprit": record.get("culprit"), "since": record["t"]}
        shown = describe(record)
        if shown is not None:
            self.events.append(shown)
            self._send({"type": "event", "event": shown})

    def _frame(self, now: float, host: Mapping) -> dict:
        servers = {}
        for label, sample in sorted(self._latest.items()):
            if now - when(sample) > STALE_SECONDS:
                continue  # silent: the page shows a gap, as the diagnosis leaves it out
            detector = self.diagnoser.replicas.get(label)
            tracks = detector.tracks if detector is not None else {}
            servers[label] = {
                "age": sample.get("age"), "incarnation": sample.get("incarnation"),
                "v": {metric: sample["m"][metric] for metric in MATRIX if metric in sample["m"]},
                "level": {metric: tracks[metric].levels[-1] for metric in MATRIX
                          if metric in tracks and tracks[metric].levels},
            }
        return {"t": round(now, 3), "servers": servers, "status": self.status,
                "ft": self._logs.poll(now) if self._logs else None,
                "host": {name: host["m"].get(name) for name in ("cpu", "agent_cpu")}}

    # --- page out ---

    def snapshot(self) -> dict:
        """Everything a page needs when it opens: what to draw, and the history so far."""

        pooled = self.signature.data["roles"].get(self.role, {}).get("steady", {})

        def metric(key: str) -> dict:
            entry, band = pooled.get(key), None
            if entry is not None:
                width, mean = spread(key, entry), entry["mean"]
                band = {f"{side}{int(sigmas)}": round(max(0.0, mean + sign * sigmas * width), 3)
                        for sigmas in (WARN_SIGMAS, ANOMALY_SIGMAS) for side, sign in (("low", -1), ("high", 1))}
            return {"key": key, "name": METRIC_NAMES.get(key, key), "band": band}

        return {"charts": [metric(key) for key in CHARTS], "matrix": [metric(key) for key in MATRIX],
                "trained": {key: self.signature.meta.get(key) for key in ("host", "config", "client_interval")},
                "actions": sorted(self.actions), "frames": list(self.frames), "events": list(self.events)}

    def _send(self, message: dict) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._listeners.discard(queue)  # a page that cannot keep up reconnects and catches up

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1").split()
            headers = {}
            while (line := await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            method, path = (request[0], request[1].partition("?")[0]) if len(request) >= 2 else ("", "")
            if method == "GET" and path == "/":
                await self._reply(writer, 200, "text/html; charset=utf-8", PAGE.read_bytes())
            elif method == "GET" and path == "/snapshot":
                await self._reply(writer, 200, "application/json", json.dumps(self.snapshot()).encode())
            elif method == "GET" and path == "/stream":
                await self._stream(reader, writer)
            elif method == "POST" and path.startswith("/action/"):
                await self._act(writer, path.removeprefix("/action/"), headers, reader)
            else:
                await self._reply(writer, 404, "text/plain", b"not found")
        except (TimeoutError, ConnectionError, EOFError, ValueError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int, kind: str, body: bytes) -> None:
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}[code]
        writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
                     "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=120)
        self._listeners.add(queue)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"Connection: keep-alive\r\n\r\n")
        closed = asyncio.ensure_future(reader.read())  # a page sends nothing more: this ends when it leaves
        try:
            while True:
                message = asyncio.ensure_future(queue.get())
                await asyncio.wait({message, closed}, return_when=asyncio.FIRST_COMPLETED)
                if not message.done():
                    message.cancel()
                    return
                writer.write(b"data: " + json.dumps(message.result(), separators=(",", ":")).encode() + b"\n\n")
                await writer.drain()
        finally:
            closed.cancel()
            self._listeners.discard(queue)

    async def _act(self, writer: asyncio.StreamWriter, name: str, headers: Mapping[str, str],
                   reader: asyncio.StreamReader) -> None:
        # A page from another site cannot send this header without permission we never
        # give, so only this page can press the buttons.
        if headers.get("x-dashboard") != "1":
            await self._reply(writer, 403, "text/plain", b"not from the dashboard page")
            return
        if name not in self.actions:
            await self._reply(writer, 404, "text/plain", b"no such action")
            return
        argument = (await reader.readexactly(min(int(headers.get("content-length", 0)), 200))).decode()
        try:
            # Actions start and stop processes and wait for confirmations: keep the page alive meanwhile.
            result = await asyncio.get_running_loop().run_in_executor(None, self.actions[name], argument)
        except Exception as exc:  # shown on the page; an action must not take the dashboard down
            await self._reply(writer, 409, "text/plain", str(exc).encode())
            return
        await self._reply(writer, 200, "text/plain", str(result or "done").encode())

    async def start(self, bind: str = "127.0.0.1", port: int = DEFAULT_PORT) -> asyncio.Server:
        return await asyncio.start_server(self._handle, bind, port)


async def play(records: Sequence[Mapping], diagnoser: Diagnoser, dashboard: Dashboard, speed: float,
               until: float | None = None) -> None:
    """Feed a recorded run through the diagnosis and the dashboard, ``speed`` times faster than life.

    ``speed`` 0 feeds everything at once. ``until`` stops that many seconds after the first sample.
    """

    previous = None
    start = next((when(record) for record in records if record.get("type") == "sample"), 0.0)
    for record in records:
        if record.get("event") == "diagnosis":
            continue  # recorded live; the replay makes its own
        moment = when(record)
        if until is not None and moment - start > until:
            break
        if speed > 0 and previous is not None and moment > previous:
            await asyncio.sleep(min((moment - previous) / speed, 2.0))
        previous = moment
        for diagnosis in diagnoser.observe(record):
            dashboard.observe(diagnosis)
        dashboard.observe(record)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Play a recorded run back in the dashboard")
    parser.add_argument("--replay", type=Path, required=True, metavar="RUN_DIR")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_PATH,
                        help=f"Signature file (default {DEFAULT_PATH.as_posix()})")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="paper")
    parser.add_argument("--speed", type=float, default=1.0, help="Times faster than life; 0 loads it all at once")
    parser.add_argument("--until", type=float, help="Stop this many seconds into the recording")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


async def serve_replay(args: argparse.Namespace, logger: EventLogger) -> None:
    signature = Signature.load(args.baseline)
    diagnoser = Diagnoser(signature, PROFILES[args.profile])
    dashboard = Dashboard(signature, diagnoser)
    server = await dashboard.start(args.bind, args.port)
    logger.event(f"dashboard: replaying {args.replay} at http://{args.bind}:{args.port}/")
    await play(read_run(args.replay), diagnoser, dashboard, args.speed, args.until)
    logger.event("dashboard: the recording has ended; the page stays up until this is stopped")
    async with server:
        await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve_replay(args, EventLogger()))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        parser.error(f"cannot serve on {args.bind}:{args.port}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
