"""When may the monitor restart a replica on its own?

Recovery reuses the system's own crash handling: killing the diagnosed replica
makes its LFD report it and the RM relaunch it, which turns a fail-slow fault
into a crash fault the system already knows how to repair. The risk is in doing
that at the wrong moment, so every rule here says no unless all of them pass:

- the diagnosis is of a kind a restart cures;
- it has stood for a few seconds, so a brief excursion does not cost a replica;
- every replica is ready, so the group never loses a second one;
- no replica was restarted in the last minute, so a wrong diagnosis cannot
  take the replicas down one after another.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ft_system.blackbox.fingerpoint import FAULT

CURABLE = frozenset({"cpu_task", "memory_growth"})


@dataclass
class RecoveryPolicy:
    persist: float = 5.0     # seconds a diagnosis must have stood
    cooldown: float = 60.0   # seconds between restarts, whoever asked for them
    last_restart: float | None = None

    def decide(self, now: float, status: Mapping, ready: Iterable[str],
               servers: Iterable[str]) -> tuple[str | None, str]:
        """The replica to restart now, or ``None``; and the reason either way."""

        if status.get("state") != FAULT:
            return None, "nothing is diagnosed"
        culprit, cause = status["culprit"], status.get("cause")
        if cause not in CURABLE:
            return None, f"a restart does not cure this kind of problem ({cause})"
        stood = now - status["since"]
        if stood < self.persist:
            return None, f"the diagnosis is {stood:.0f} s old; waiting for {self.persist:g} s"
        if set(ready) != set(servers):
            return None, "not every replica is ready"
        if self.last_restart is not None and now - self.last_restart < self.cooldown:
            return None, (f"a replica was restarted {now - self.last_restart:.0f} s ago; "
                          f"waiting {self.cooldown:g} s between restarts")
        return culprit, f"{culprit} has been diagnosed for {stood:.0f} s"

    def restarted(self, now: float) -> None:
        self.last_restart = now
