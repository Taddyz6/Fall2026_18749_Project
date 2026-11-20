"""Inject, clear and inspect faults in running server replicas.

Run these on the computer where the replica runs, because the fault directory is
a local folder shared with that replica's shim (``shim/sitecustomize.py``):

    python -m ft_system.blackbox.faults.ctl env        # how to start an LFD with the shim
    python -m ft_system.blackbox.faults.ctl status
    python -m ft_system.blackbox.faults.ctl inject S2 --duty 0.75
    python -m ft_system.blackbox.faults.ctl clear S2

A command succeeds only after the replica confirms the change. Every confirmed
change is appended to ``events.jsonl`` in the fault directory; that file is the
ground truth the diagnosis is later scored against.

Standard library only, like the shim, so faults can be injected on a computer
that has nothing but the project checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from ft_system.blackbox.faults.shim.sitecustomize import (
    MAX_THREADS, normalize, read_json, safe_id, spec_path, state_path, write_json,
)

DEFAULT_FAULT_DIR = Path("logs/ec/faults")
SHIM_DIR = Path(__file__).resolve().parent / "shim"
EVENTS_FILE = "events.jsonl"
STATE_SUFFIX = ".shim.json"


class FaultError(RuntimeError):
    """The requested change was not made, or could not be confirmed."""


def resolve_fault_dir(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("FT_FAULT_DIR") or DEFAULT_FAULT_DIR).resolve()


def shim_environment(fault_dir: str | Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for an LFD whose replica should carry the shim."""

    environment = dict(os.environ if base is None else base)
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (str(SHIM_DIR), inherited)))
    environment["FT_FAULT_DIR"] = str(Path(fault_dir).resolve())
    return environment


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) would terminate the process on Windows, so ask the kernel.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, but is not ours
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def replicas(fault_dir: str | Path) -> dict[str, dict]:
    """Registration of every replica whose shim has reported into ``fault_dir``."""

    found = {}
    for path in sorted(Path(fault_dir).glob(f"*{STATE_SUFFIX}")):
        state = read_json(str(path))
        if state is not None and isinstance(state.get("pid"), int):
            found[path.name[:-len(STATE_SUFFIX)]] = state
    return found


def describe(fault: Mapping | None) -> str:
    if not fault:
        return "no fault"
    threads = fault["threads"]
    return (f"cpu fault: {threads} thread{'s' if threads != 1 else ''} busy "
            f"{fault['duty']:.0%} of every {fault['period']:g} s")


def _record(fault_dir: Path, event: dict) -> None:
    with (fault_dir / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def apply(fault_dir: str | Path, server_id: str, fault: Mapping | None, wait: float = 3.0) -> dict:
    """Make one replica run ``fault`` (``None`` clears it) and return its confirmed state."""

    fault_dir = Path(fault_dir)
    if not safe_id(server_id):
        raise FaultError(f"{server_id!r} is not a usable server ID")
    registration = state_path(str(fault_dir), server_id)
    before = read_json(registration)
    if before is None:
        raise FaultError(f"no shim has registered for {server_id} in {fault_dir}; start its LFD "
                         "with the environment printed by the 'env' command")
    if not pid_alive(before["pid"]):
        raise FaultError(f"{server_id} (pid {before['pid']}) is not running; "
                         "if it is being relaunched, try again once it is back")

    request = {"kind": "none"} | dict(fault or {}) | {
        "id": secrets.token_hex(8), "token": before["token"], "pid": before["pid"],
        "requested_at": time.time()}
    target = spec_path(str(fault_dir), server_id)
    write_json(target, request)
    deadline = time.monotonic() + wait
    while True:
        state = read_json(registration)
        if state is not None and state.get("token") != before["token"]:
            raise FaultError(f"{server_id} was relaunched (now pid {state['pid']}) before it "
                             "confirmed; the new process is running without a fault")
        if state is not None and state.get("applied_id") == request["id"]:
            break
        if time.monotonic() >= deadline:
            if fault:
                # Do not leave an unconfirmed fault behind for the replica to pick up later.
                write_json(target, request | {"kind": "none", "id": secrets.token_hex(8)})
                _record(fault_dir, {"t": time.time(), "event": "fault_unconfirmed",
                                    "server": server_id, "pid": before["pid"]})
            raise FaultError(f"{server_id} (pid {before['pid']}) did not confirm within {wait:g} s")
        time.sleep(0.05)

    if state["applied"] != normalize(request):
        raise FaultError(f"{server_id} is running {describe(state['applied'])}, not what was asked")
    _record(fault_dir, {
        "t": state["applied_at"], "event": "fault_injected" if state["applied"] else "fault_cleared",
        "server": server_id, "pid": state["pid"], "fault": state["applied"],
        "previous": before["applied"], "requested_at": request["requested_at"]})
    return state


def inject(fault_dir: str | Path, server_id: str, duty: float = 0.75, threads: int = 1,
           period: float = 0.1, wait: float = 3.0) -> dict:
    if not 0 < duty <= 1:
        raise FaultError("duty must be above 0 and at most 1")
    if not 1 <= threads <= MAX_THREADS:
        raise FaultError(f"threads must be between 1 and {MAX_THREADS}")
    if not 0.01 <= period <= 1:
        raise FaultError("period must be between 0.01 and 1 second")
    fault = {"kind": "cpu", "duty": duty, "threads": threads, "period": period}
    return apply(fault_dir, server_id, fault, wait)


def clear(fault_dir: str | Path, server_id: str, wait: float = 3.0) -> dict:
    return apply(fault_dir, server_id, None, wait)


def env_lines(fault_dir: Path, shell: str) -> list[str]:
    if shell == "powershell":
        return [f'$env:FT_FAULT_DIR = "{fault_dir}"',
                f'$env:PYTHONPATH = (@("{SHIM_DIR}", $env:PYTHONPATH) | Where-Object {{ $_ }}) -join ";"']
    return [f"export FT_FAULT_DIR='{fault_dir}'",
            f"export PYTHONPATH='{SHIM_DIR}'\"${{PYTHONPATH:+{os.pathsep}$PYTHONPATH}}\""]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inject faults into running server replicas")
    parser.add_argument("--fault-dir", help="Folder shared with the replicas' shims "
                        f"(default: $FT_FAULT_DIR, else {DEFAULT_FAULT_DIR.as_posix()})")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("inject", help="Start a CPU-intensive task inside one replica")
    add.add_argument("server_id", help="Replica ID, for example S2")
    add.add_argument("--duty", type=float, default=0.75,
                     help="Fraction of each period the task is busy (default 0.75)")
    add.add_argument("--threads", type=int, default=1, help="Number of busy threads (default 1)")
    add.add_argument("--period", type=float, default=0.1, help="Seconds per busy/idle cycle")
    add.add_argument("--wait", type=float, default=3.0, help="Seconds to wait for confirmation")

    remove = commands.add_parser("clear", help="Stop the injected fault")
    remove.add_argument("server_ids", nargs="*", help="Replica IDs; none means every replica")
    remove.add_argument("--wait", type=float, default=3.0, help="Seconds to wait for confirmation")

    commands.add_parser("status", help="List replicas that carry the shim and their faults")

    env = commands.add_parser("env", help="Print the environment an LFD needs to load the shim")
    env.add_argument("--shell", choices=("powershell", "bash"),
                     default="powershell" if os.name == "nt" else "bash")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fault_dir = resolve_fault_dir(args.fault_dir)
    if args.command == "env":
        print("\n".join(env_lines(fault_dir, args.shell)))
        return 0

    registered = replicas(fault_dir)
    if args.command == "status":
        print(f"fault directory: {fault_dir}")
        if not registered:
            print("no replica has registered a shim here")
        for server_id, state in registered.items():
            if not pid_alive(state["pid"]):
                print(f"{server_id}  pid {state['pid']}  not running")
                continue
            since = f" for {time.time() - state['applied_at']:.0f} s" if state["applied"] else ""
            print(f"{server_id}  pid {state['pid']}  running  {describe(state['applied'])}{since}")
        return 0

    if args.command == "inject":
        targets = [args.server_id]
    else:
        targets = args.server_ids or [server_id for server_id, state in registered.items()
                                      if pid_alive(state["pid"])]
    if not targets:
        print("no running replica has registered a shim here")
    failed = False
    for server_id in targets:
        try:
            if args.command == "inject":
                state = inject(fault_dir, server_id, args.duty, args.threads, args.period, args.wait)
            else:
                state = clear(fault_dir, server_id, args.wait)
            print(f"{server_id} (pid {state['pid']}): {describe(state['applied'])}")
        except FaultError as exc:
            failed = True
            print(f"error: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
