"""Demo-day diagnostics: code/config identity, console log copies, and a status key."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from datetime import date
from functools import lru_cache
from pathlib import Path

from ft_system.common.config import AppConfig
from ft_system.common.logging import EventLogger


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def build_id() -> str:
    """Return the short commit of the running code, marked if src/ has local edits."""

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=3, check=True).stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        edited = git("status", "--porcelain", "--untracked-files=no", "--", "src")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return commit + ("+modified" if edited else "")


def identity(app_config: AppConfig) -> dict[str, str]:
    return {"build": build_id(), "config": app_config.fingerprint}


def make_logger(component_id: str) -> EventLogger:
    """Console logger that also appends to logs/console/<date>/<component>.log.

    FT_LOG_DIR chooses another directory; FT_LOG_DIR=off disables the copy.
    """

    setting = os.environ.get("FT_LOG_DIR", "")
    if setting.lower() == "off":
        return EventLogger()
    directory = Path(setting) if setting else Path("logs") / "console" / date.today().isoformat()
    path = directory / f"{component_id}.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        copy = path.open("a", encoding="utf-8")
    except OSError:
        return EventLogger()
    logger = EventLogger(copy=copy)
    logger.copy_path = str(path)
    return logger


def interactive_terminal() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def announce(logger: EventLogger, component_id: str, app_config: AppConfig, config_path: str) -> dict[str, str]:
    """Print what this process runs, so mismatched computers stand out in the demo."""

    ident = identity(app_config)
    mode = app_config.replication.mode if app_config.replication is not None else "no replication"
    logger.event(f"{component_id}: build {ident['build']}, config {ident['config']} "
                 f"({Path(config_path).name}, {mode})")
    extras = []
    if logger.copy_path is not None:
        extras.append(f"log copy: {logger.copy_path}")
    if interactive_terminal():
        extras.append("press Enter for status")
    if extras:
        logger.event(f"{component_id}: " + "; ".join(extras))
    return ident


def on_enter(callback: Callable[[], None]) -> None:
    """Call ``callback`` on the running loop each time Enter is pressed in this terminal."""

    if not interactive_terminal():
        return
    loop = asyncio.get_running_loop()

    def read_lines() -> None:
        for _ in sys.stdin:
            try:
                loop.call_soon_threadsafe(callback)
            except RuntimeError:
                return  # The loop has closed.

    # A daemon thread never delays exit, unlike a blocked asyncio.to_thread worker.
    threading.Thread(target=read_lines, name="status-key", daemon=True).start()


def print_status(logger: EventLogger, component_id: str, lines: Iterable[str]) -> None:
    logger.event(f"----- STATUS {component_id} -----", "view")
    for line in lines:
        logger.event(f"  {line}", "failed" if line.startswith(("hint:", "MISMATCH")) else "view")


def names(items: Iterable[str]) -> str:
    items = list(items)
    return ", ".join(items) if items else "none"
