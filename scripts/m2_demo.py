"""Open the ten M2 processes in separate macOS Terminal windows."""

from __future__ import annotations

import argparse
import math
import socket
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

APPLESCRIPT = r'''
on startJob(projectRoot, pythonPath, configPath, runTag, titleText, component, arguments)
    set launchCommand to "cd " & quoted form of projectRoot & " && PYTHONPATH=" & quoted form of (projectRoot & "/src") & " PYTHONUNBUFFERED=1 " & quoted form of pythonPath & " -m ft_system." & component & ".main --config " & quoted form of configPath & arguments
    tell application "Terminal"
        set demoTab to do script launchCommand
        set roleTitle to "M2 - " & titleText & " [" & runTag & "]"
        set custom title of demoTab to roleTitle
        repeat with candidate in every window
            if custom title of selected tab of candidate is roleTitle then return id of candidate
        end repeat
        error "Could not identify the Terminal window for " & roleTitle
    end tell
end startJob

on waitForText(windowId, expectedText, descriptionText)
    repeat 100 times
        tell application "Terminal" to set terminalText to contents of tab 1 of window id windowId
        if terminalText contains expectedText then return
        delay 0.2
    end repeat
    error "Timed out waiting for " & descriptionText & ". Inspect the open Terminal windows."
end waitForText

on run argv
    set projectRoot to item 1 of argv
    set pythonPath to item 2 of argv
    set configPath to item 3 of argv
    set clientInterval to item 4 of argv
    set runTag to item 5 of argv

    set gfdTab to my startJob(projectRoot, pythonPath, configPath, runTag, "GFD", "gfd", "")
    my waitForText(gfdTab, "GFD: 0 members", "GFD startup")

    repeat with i from 1 to 3
        set lfdName to "LFD" & i
        my startJob(projectRoot, pythonPath, configPath, runTag, lfdName, "lfd", " --lfd-id " & lfdName)
        my waitForText(gfdTab, "registered " & lfdName, lfdName & " registration")
    end repeat

    repeat with i from 1 to 3
        set serverName to "S" & i
        my startJob(projectRoot, pythonPath, configPath, runTag, serverName, "server", " --server-id " & serverName)
        if i is 1 then
            set expectedMembers to "GFD: 1 member: S1"
        else if i is 2 then
            set expectedMembers to "GFD: 2 members: S1, S2"
        else
            set expectedMembers to "GFD: 3 members: S1, S2, S3"
        end if
        my waitForText(gfdTab, expectedMembers, serverName & " membership")
    end repeat

    repeat with i from 1 to 3
        set clientName to "C" & i
        set clientTab to my startJob(projectRoot, pythonPath, configPath, runTag, clientName, "client", " --client-id " & clientName & " --interval " & clientInterval)
        my waitForText(clientTab, "Sending <" & clientName & ",", clientName & " requests")
    end repeat

    tell application "Terminal" to activate
    return "Opened GFD, LFD1-3, S1-3, and C1-3 in ten Terminal windows."
end run
'''


def positive_interval(value: str) -> float:
    interval = float(value)
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError("interval must be a finite positive number")
    return interval


def check_ports_available(config_path: Path) -> None:
    """Avoid opening another set of windows on occupied local demo ports."""
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    listeners = [("GFD", config["gfd"]["bind_host"], config["gfd"]["port"])]
    for number in range(1, 4):
        server = config["server"][f"S{number}"]
        listeners.extend((
            (f"S{number} client", server["bind_host"], server["client_port"]),
            (f"S{number} heartbeat", server["bind_host"], server["heartbeat_port"]),
        ))

    reserved = []
    try:
        for name, host, port in listeners:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((host, port))
            except OSError as exc:
                listener.close()
                raise RuntimeError(f"{name} port {host}:{port} is unavailable: {exc}") from exc
            reserved.append(listener)
    finally:
        for listener in reserved:
            listener.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=ROOT / "configs" / "m2.local.toml",
        help="Shared M2 config (default: configs/m2.local.toml)",
    )
    parser.add_argument(
        "--interval", type=positive_interval, default=1.0,
        help="Seconds between automatic client requests (default: 1.0)",
    )
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("the visual demo requires macOS Terminal; run scripts/m2_smoke.py for a headless check")
    config_path = args.config.resolve()
    if not config_path.is_file():
        parser.error(f"config file does not exist: {config_path}")
    try:
        check_ports_available(config_path)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))

    try:
        result = subprocess.run(
            ["osascript", "-", str(ROOT), sys.executable, str(config_path),
             str(args.interval), uuid.uuid4().hex[:6]],
            input=APPLESCRIPT,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"无法启动 Terminal 演示：{exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        print("请检查已打开的 Terminal 窗口和 macOS 自动化权限；已启动的进程可在各自窗口按 Ctrl-C 停止。", file=sys.stderr)
        return result.returncode

    print(result.stdout.strip())
    print("在 M2 - S1 窗口按 Ctrl-C，观察 GFD 降为 2 个成员；再停止 S2，观察 C1–C3 继续收到 S3 回复。")
    print("演示结束后，在剩余窗口按 Ctrl-C。自动验收请运行 python3 scripts/m2_smoke.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
