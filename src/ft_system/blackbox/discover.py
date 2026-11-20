"""Recognise the fault-tolerant system's processes from their command lines.

A command line is all the monitor uses to find a process: no PID files, no
shared code, no cooperation from the process itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_MODULE = re.compile(r"^ft_system\.(rm|gfd|lfd|server|client)\.main$")
_SCRIPT = re.compile(r"(?:^|[\\/])ft-(rm|gfd|lfd|server|client)(?:\.exe|-script\.pyw?)?$", re.IGNORECASE)
_ID_OPTION = {"server": "--server-id", "lfd": "--lfd-id", "client": "--client-id"}


def identify(command: Sequence[str]) -> tuple[str, str] | None:
    """``(role, label)`` such as ``("server", "S2")``; ``None`` for an unrelated process."""

    role = None
    for argument in command:
        match = _MODULE.match(argument) or _SCRIPT.search(argument)
        if match:
            role = match.group(1).lower()
            break
    if role is None:
        return None
    option = _ID_OPTION.get(role)
    if option is None:
        return role, role.upper()  # one RM and one GFD: the role is the label
    for index, argument in enumerate(command):
        if argument == option and index + 1 < len(command):
            return role, command[index + 1]
        if argument.startswith(option + "="):
            return role, argument.partition("=")[2]
    return None


@dataclass(frozen=True)
class Seen:
    """One operating-system process that belongs to a component."""

    pid: int
    parent: int
    started: float
    role: str
    label: str


def group(processes: Iterable[Seen]) -> dict[str, list[Seen]]:
    """One component per label: its root process first, then same-label descendants.

    In a Windows virtual environment ``python.exe`` is a launcher whose child is the
    real interpreter with the same command line, so one component can be two
    processes. If a label has several unrelated roots, the newest is the component.
    """

    by_label: dict[str, list[Seen]] = {}
    for process in processes:
        by_label.setdefault(process.label, []).append(process)
    components = {}
    for label, members in by_label.items():
        pids = {member.pid for member in members}
        roots = [member for member in members if member.parent not in pids] or members
        root = max(roots, key=lambda member: member.started)
        family, grew = [root], True
        while grew:
            known = {member.pid for member in family}
            added = [member for member in members if member.parent in known and member.pid not in known]
            family += added
            grew = bool(added)
        components[label] = family
    return components
