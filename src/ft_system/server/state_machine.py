"""Deterministic application state owned by a server replica."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


class StateMachineError(ValueError):
    """Raised when an operation cannot be applied to application state."""


@dataclass(frozen=True)
class Transition:
    before: dict[str, int]
    after: dict[str, int]
    client_value: int


class DeterministicStateMachine:
    """Maintain one independent monotonically increasing slot per client."""

    def __init__(self, client_ids: Iterable[str]) -> None:
        ids = tuple(client_ids)
        if not ids:
            raise StateMachineError("at least one client is required")
        if any(not isinstance(client_id, str) or not client_id for client_id in ids):
            raise StateMachineError("client ids must be non-empty strings")
        if len(ids) != len(set(ids)):
            raise StateMachineError("client ids must be unique")
        self._state = {client_id: 0 for client_id in ids}

    def snapshot(self) -> dict[str, int]:
        return dict(self._state)

    def apply(self, client_id: str, operation: str) -> Transition:
        if client_id not in self._state:
            raise StateMachineError(f"unknown client: {client_id}")
        if operation != "increment":
            raise StateMachineError(f"unsupported operation: {operation}")

        before = self.snapshot()
        self._state[client_id] += 1
        after = self.snapshot()
        return Transition(before=before, after=after, client_value=after[client_id])
