import pytest

from ft_system.blackbox.discover import Seen, group, identify

CONFIG = ["--config", "configs/m5.active.local.toml"]


@pytest.mark.parametrize("command, expected", [
    (["python", "-m", "ft_system.server.main", *CONFIG, "--server-id", "S2"], ("server", "S2")),
    (["python", "-m", "ft_system.server.main", "--server-id=S3", *CONFIG], ("server", "S3")),
    (["python", "-m", "ft_system.lfd.main", *CONFIG, "--lfd-id", "LFD2", "--replica-log", "x/S2.log"],
     ("lfd", "LFD2")),
    (["python", "-m", "ft_system.client.main", *CONFIG, "--client-id", "C1", "--interval", "0.2"],
     ("client", "C1")),
    (["python", "-m", "ft_system.rm.main", *CONFIG], ("rm", "RM")),
    (["python", "-m", "ft_system.gfd.main", *CONFIG], ("gfd", "GFD")),
    ([r"C:\repo\.venv\Scripts\python.exe", r"C:\repo\.venv\Scripts\ft-server.exe", "--server-id", "S1"],
     ("server", "S1")),
    (["/usr/bin/python3", "/repo/.venv/bin/ft-lfd", "--lfd-id", "LFD3"], ("lfd", "LFD3")),
    (["python", "ft-gfd-script.py", *CONFIG], ("gfd", "GFD")),
])
def test_identify_reads_role_and_label_from_a_command_line(command, expected):
    assert identify(command) == expected


@pytest.mark.parametrize("command", [
    [],
    ["python", "-m", "ft_system.blackbox.agent", "--stdout"],
    ["python", "-m", "ft_system.server.main", *CONFIG],            # a server without its ID
    ["python", "-m", "ft_system.server.main", "--server-id"],
    ["bash", "-c", "python -m ft_system.server.main --server-id S2"],  # mentioned, not run
    ["python", "scripts/m5_smoke.py"],
    ["notepad.exe", "ft-server-notes.txt"],
])
def test_identify_ignores_everything_else(command):
    assert identify(command) is None


def seen(pid, parent, started, label="S2"):
    return Seen(pid, parent, started, "server", label)


def test_group_makes_one_component_per_label():
    s1, s2 = seen(10, 1, 100.0, "S1"), seen(20, 2, 101.0, "S2")

    assert group([s1, s2]) == {"S1": [s1], "S2": [s2]}


def test_group_joins_a_launcher_and_the_interpreter_it_started():
    launcher, interpreter = seen(20, 2, 100.0), seen(21, 20, 100.1)

    assert group([interpreter, launcher]) == {"S2": [launcher, interpreter]}


def test_group_follows_a_chain_of_descendants():
    family = [seen(20, 2, 100.0), seen(21, 20, 100.1), seen(22, 21, 100.2)]

    assert group(reversed(family)) == {"S2": family}


def test_group_takes_the_newest_of_unrelated_processes_with_one_label():
    old, new = seen(20, 2, 100.0), seen(30, 2, 200.0)

    assert group([old, new]) == {"S2": [new]}
