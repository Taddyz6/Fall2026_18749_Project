"""Socket-free replication decisions made by the Replication Manager."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class ReplicationPolicy:
    """Derive readiness work and the passive primary from membership and status reports.

    ``members`` keeps join order, so "oldest" means "joined first". The view
    number advances whenever the members, the ready set, or the primary changes.
    """

    def __init__(self, mode: str) -> None:
        if mode not in {"active", "passive"}:
            raise ValueError("mode must be active or passive")
        self.mode = mode
        self.members: list[str] = []
        self.ready: set[str] = set()
        self.primary: str | None = None
        self.view_num = 0
        self.ever_ready = False

    def view(self) -> dict[str, Any]:
        return {
            "view_num": self.view_num,
            "mode": self.mode,
            "members": sorted(self.members),
            "ready": sorted(self.ready),
            "primary": self.primary,
        }

    def _signature(self) -> tuple:
        return tuple(sorted(self.members)), tuple(sorted(self.ready)), self.primary

    def _elect(self) -> None:
        if self.mode != "passive":
            return
        ready = [member for member in self.members if member in self.ready]
        # Keep the current primary unless it left, or it holds no state while a backup does.
        if self.primary in self.members and (self.primary in self.ready or not ready):
            return
        self.primary = (ready or self.members or [None])[0]

    def _commit(self, before: tuple) -> bool:
        self._elect()
        self.ever_ready = self.ever_ready or bool(self.ready)
        if self._signature() == before:
            return False
        self.view_num += 1
        return True

    def set_members(self, members: Iterable[str], reported_ready: Mapping[str, bool] | None = None) -> bool:
        """Apply a GFD membership snapshot; return whether the view changed.

        ``reported_ready`` holds the latest status each connected replica sent;
        a joiner that already reports ready (for example after a brief false
        suspicion) keeps that state instead of receiving a transfer.
        """

        before = self._signature()
        current = set(members)
        self.members = [member for member in self.members if member in current]
        self.ready &= current
        for member in sorted(current - set(self.members)):
            self.members.append(member)
            if reported_ready is not None and reported_ready.get(member):
                self.ready.add(member)
        return self._commit(before)

    def set_status(self, replica_id: str, ready: bool) -> bool:
        """Apply a replica's own readiness report; return whether the view changed."""

        if replica_id not in self.members:
            return False
        before = self._signature()
        if ready:
            self.ready.add(replica_id)
        else:
            self.ready.discard(replica_id)
        return self._commit(before)

    def assignments(self) -> dict[str, str | None]:
        """Map each replica that must become ready to its state donor.

        ``None`` means the RM tells the replica to become ready with the state it
        already holds: the first replica of its kind, or the last resort when no
        ready replica remains. Passive backups need no assignment because the
        primary's periodic checkpoints make them ready.
        """

        ready = [member for member in self.members if member in self.ready]
        waiting = [member for member in self.members if member not in self.ready]
        if self.mode == "passive":
            if self.primary is not None and self.primary not in self.ready:
                return {self.primary: None}
            return {}
        if ready:
            return {target: ready[0] for target in waiting}
        return {waiting[0]: None} if waiting else {}
