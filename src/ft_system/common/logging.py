"""Human-readable, rubric-aligned console event logging."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, TextIO


_COLORS = {
    "received": "\033[36m",
    "sending": "\033[33m",
    "state": "\033[35m",
    "heartbeat": "\033[34m",
    "failed": "\033[31m",
    "recovered": "\033[32m",
    "error": "\033[31m",
}
_RESET = "\033[0m"


def default_clock() -> str:
    """Return a local wall-clock timestamp suitable for console debugging."""

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _message_tuple(message: Mapping[str, Any], direction: str) -> str:
    try:
        client_id = message["client_id"]
        replica_id = message["replica_id"]
        request_num = message["request_num"]
    except KeyError as exc:
        raise ValueError(f"message is missing {exc.args[0]}") from exc
    if direction not in {"request", "reply"}:
        raise ValueError("direction must be request or reply")
    return f"<{client_id}, {replica_id}, {request_num}, {direction}>"


class EventLogger:
    """Print required course events with stable wording."""

    def __init__(
        self,
        stream: TextIO = sys.stdout,
        clock: Callable[[], str] = default_clock,
        use_color: bool | None = None,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._use_color = stream.isatty() if use_color is None else use_color

    def _write(self, text: str, color: str | None = None) -> None:
        line = f"[{self._clock()}] {text}"
        if self._use_color and color is not None:
            line = f"{_COLORS[color]}{line}{_RESET}"
        print(line, file=self._stream, flush=True)

    def received(self, message: Mapping[str, Any], direction: str) -> None:
        self._write(f"Received {_message_tuple(message, direction)}", "received")

    def sending(self, message: Mapping[str, Any], direction: str) -> None:
        self._write(f"Sending {_message_tuple(message, direction)}", "sending")

    def state(
        self,
        replica_id: str,
        state: Mapping[str, int],
        phase: str,
        request: Mapping[str, Any],
    ) -> None:
        if phase not in {"before", "after"}:
            raise ValueError("phase must be before or after")
        rendered_state = json.dumps(dict(state), sort_keys=True)
        request_tuple = _message_tuple(request, "request")
        self._write(
            f"my_state_{replica_id} = {rendered_state} "
            f"{phase} processing {request_tuple}",
            "state",
        )

    def heartbeat_sent(self, lfd_id: str, replica_id: str, count: int) -> None:
        self._write(
            f"[{count}] {lfd_id} sending heartbeat to {replica_id}", "heartbeat"
        )

    def heartbeat_ack(self, lfd_id: str, replica_id: str, count: int) -> None:
        self._write(
            f"[{count}] {lfd_id} receives heartbeat from {replica_id}", "heartbeat"
        )

    def heartbeat_failed(
        self, lfd_id: str, replica_id: str, count: int, reason: str
    ) -> None:
        self._write(
            f"[{count}] {lfd_id} heartbeat to {replica_id} failed: {reason}", "failed"
        )

    def server_heartbeat_received(
        self, replica_id: str, lfd_id: str, count: int
    ) -> None:
        self._write(
            f"[{count}] {replica_id} receives heartbeat from {lfd_id}", "heartbeat"
        )

    def server_heartbeat_ack_sent(
        self, replica_id: str, lfd_id: str, count: int
    ) -> None:
        self._write(
            f"[{count}] {replica_id} sending heartbeat ACK to {lfd_id}", "heartbeat"
        )

    def server_failed(self, replica_id: str) -> None:
        self._write(f"{replica_id} has died", "failed")

    def server_recovered(self, replica_id: str) -> None:
        self._write(f"{replica_id} has recovered", "recovered")

    def error(self, component_id: str, detail: str) -> None:
        self._write(f"{component_id} ERROR: {detail}", "error")
