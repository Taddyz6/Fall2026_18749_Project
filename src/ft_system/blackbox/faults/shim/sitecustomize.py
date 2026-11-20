"""Fault-injection shim, loaded into a server replica without changing project code.

Python imports a module named ``sitecustomize`` at start-up when one is on
``PYTHONPATH``. An LFD passes its environment to the server it launches, so an LFD
started with this directory on ``PYTHONPATH`` and with ``FT_FAULT_DIR`` set loads
this file into every replica it launches or relaunches. In any other process (RM,
GFD, LFD, client), or without ``FT_FAULT_DIR``, the shim does nothing.

Two files per replica live in ``FT_FAULT_DIR``. Each has one writer and is always
replaced atomically, never edited in place or deleted:

    <server-id>.shim.json   written here: this process's PID and token, and the
                            fault it is running now
    <server-id>.json        written by ``ctl.py``: the fault this process should run

A fault is addressed to one process by its token. A relaunched replica has a new
token, so it starts clean, as a restarted process with a runaway thread would.

Standard library only. Nothing here may break the server it rides in: no output,
and no exception leaves the control thread.
"""

from __future__ import annotations

import json
import os
import sys
import time

POLL_SECONDS = 0.25
MAX_THREADS = 8


def server_id_from(argv: list[str]) -> str | None:
    """Value of ``--server-id``, the option that only the server command line takes."""

    for index, arg in enumerate(argv):
        if arg == "--server-id" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--server-id="):
            return arg.partition("=")[2]
    return None


def safe_id(server_id: str | None) -> bool:
    """Server IDs become file names, so accept only plain ones."""

    return bool(server_id) and all(char.isalnum() or char in "-_" for char in server_id)


def spec_path(fault_dir: str, server_id: str) -> str:
    return os.path.join(fault_dir, f"{server_id}.json")


def state_path(fault_dir: str, server_id: str) -> str:
    return os.path.join(fault_dir, f"{server_id}.shim.json")


def write_json(path: str, payload: dict) -> None:
    """Replace ``path`` in one step, so a reader sees the old or the new file, never half."""

    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    for attempt in range(40):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            # Windows refuses to replace a file that another process is reading.
            if attempt == 39:
                raise
            time.sleep(0.025)


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def normalize(spec: dict) -> dict | None:
    """The fault a spec asks for, with bounded parameters; ``None`` means no fault."""

    if spec.get("kind") != "cpu":
        return None
    return {
        "kind": "cpu",
        "duty": min(1.0, max(0.01, float(spec.get("duty", 1.0)))),
        "threads": min(MAX_THREADS, max(1, int(spec.get("threads", 1)))),
        "period": min(1.0, max(0.01, float(spec.get("period", 0.1)))),
    }


def _spin(stop, duty: float, period: float) -> None:
    busy, idle = duty * period, (1.0 - duty) * period
    counter = 0
    while not stop.is_set():
        end = time.perf_counter() + busy
        while time.perf_counter() < end:
            for _ in range(2000):
                counter += 1
        if idle > 0:
            # Not stop.wait(): on Windows only sleep() has sub-tick resolution.
            time.sleep(idle)


class CpuHog:
    """Threads that each spin for ``duty * period`` seconds, then sleep for the rest.

    They compete with the server's event loop for the interpreter lock, so the
    replica keeps answering, only late: a fail-slow fault rather than a crash.
    """

    def __init__(self) -> None:
        self.current: dict | None = None
        self._stop = None
        self._threads: list = []

    def apply(self, spec: dict) -> dict | None:
        import threading

        wanted = normalize(spec)
        if wanted == self.current:
            return wanted
        if self._stop is not None:
            self._stop.set()
            for thread in self._threads:
                thread.join()
        self._stop, self._threads, self.current = None, [], wanted
        if wanted is not None:
            self._stop = threading.Event()
            for number in range(wanted["threads"]):
                thread = threading.Thread(
                    target=_spin, args=(self._stop, wanted["duty"], wanted["period"]),
                    name=f"fault-cpu-{number}", daemon=True)
                thread.start()
                self._threads.append(thread)
        return wanted


def control_loop(fault_dir: str, server_id: str, poll: float = POLL_SECONDS) -> None:
    """Register this process, then follow its fault spec until the process exits."""

    import secrets

    state = {"server": server_id, "pid": os.getpid(), "token": secrets.token_hex(8),
             "started": time.time(), "applied": None, "applied_id": None, "applied_at": None}
    hog = CpuHog()
    source, seen = spec_path(fault_dir, server_id), None
    try:
        os.makedirs(fault_dir, exist_ok=True)
        write_json(state_path(fault_dir, server_id), state)
    except OSError:
        return
    while True:
        time.sleep(poll)
        try:
            # stat() instead of reading every time: the shim should add as little
            # file activity as it can to the process being measured.
            status = os.stat(source)
            signature = (status.st_mtime_ns, status.st_size, status.st_ino)
            if signature == seen:
                continue
            spec = read_json(source)
            if spec is None:
                continue  # mid-replacement; look again at the next poll
            seen = signature
            if spec.get("token") != state["token"]:
                continue  # addressed to an earlier incarnation of this replica
            state.update(applied=hog.apply(spec), applied_id=spec.get("id"), applied_at=time.time())
            write_json(state_path(fault_dir, server_id), state)
        except Exception:
            continue


def install() -> None:
    fault_dir = os.environ.get("FT_FAULT_DIR")
    server_id = server_id_from(getattr(sys, "orig_argv", sys.argv))
    if not fault_dir or not safe_id(server_id):
        return
    import threading

    threading.Thread(target=control_loop, args=(fault_dir, server_id),
                     name="fault-shim", daemon=True).start()


# Only when Python loads this file as its start-up hook; importing it as
# ft_system.blackbox.faults.shim.sitecustomize (ctl.py, tests) starts nothing.
if __name__ == "sitecustomize":
    try:
        install()
    except Exception:
        pass
