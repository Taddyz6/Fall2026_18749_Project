"""Opt-in four-host scenarios on one Mac using real Terminal.app windows."""

import os
import signal
import subprocess
import sys

import pytest

from scripts.m5_smoke import Run, scenario


WINDOW_TEST = os.environ.get("M5_TEST_TERMINAL")
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or WINDOW_TEST not in ("1", "confirm-close"),
    reason="set M5_TEST_TERMINAL=1 in a macOS desktop session to open real windows",
)


def terminal_script(script, *args):
    return subprocess.run(["/usr/bin/osascript", "-e", script, *args], check=True,
                          capture_output=True, text=True, timeout=15).stdout.strip()


def replica_window(run, replica):
    pid = run.pid(replica)
    info = subprocess.check_output(
        ["/bin/ps", "-p", str(pid), "-o", "tty=,pgid=,tpgid=,command="], text=True,
    ).strip()
    tty, group, foreground, command = info.split(maxsplit=3)
    assert group == foreground and tty != "??"
    assert "ft_system.server.main" in command and str(run.config_path) in command
    return int(terminal_script('''on run argv
        tell application id "com.apple.Terminal"
            repeat with w in windows
                set t to selected tab of w
                if (tty of t is item 1 of argv) and (custom title of t is item 2 of argv) then
                    if (count tabs of w) is 1 then return id of w
                end if
            end repeat
        end tell
        error "No Terminal window for the test replica"
    end run''', "/dev/" + tty, replica))


@pytest.mark.parametrize("mode", ["active", "passive"])
def test_real_mac_windows_recover_with_clients(tmp_path, mode, monkeypatch):
    run = Run(mode, tmp_path / mode, first="S2", demo_timing=True)
    run.config_path.write_text(run.config_path.read_text().replace("[recovery]", "[recovery]\nmac_terminal = true"))
    windows = set()
    previous = {}
    actions = iter(("interrupt", "close", "interrupt"))

    def fault(replica, pid):
        for name in ("S1", "S2", "S3"):
            window = replica_window(run, name)
            windows.add(window)
            if name in previous and run.pid(name) != previous[name][0]:
                assert window != previous[name][1]
            previous[name] = (run.pid(name), window)
        assert previous[replica][0] == pid
        action = next(actions)
        if action == "close" and WINDOW_TEST != "confirm-close":
            # Exercise a terminal hangup without requiring a person to answer
            # Terminal.app's close confirmation. The PTY test closes the device.
            os.killpg(os.getpgid(pid), signal.SIGHUP)
            return
        # Address the exact verified test window; never send keys to the frontmost app.
        terminal_script('''on run argv
            tell application id "com.apple.Terminal"
                set w to window id ((item 1 of argv) as integer)
                if item 2 of argv is "close" then
                    close w saving no
                else
                    do script (ASCII character 3) in selected tab of w
                end if
            end tell
        end run''', str(previous[replica][1]), action)
        if action == "close":
            window = str(previous[replica][1])
            state = terminal_script('''on run argv
                tell application id "com.apple.Terminal" to return exists window id ((item 1 of argv) as integer)
            end run''', window)
            if state == "true":
                print(f"{mode}: confirm Terminate in the test {replica} window ({window})", flush=True)

    monkeypatch.setattr(run, "inject_crash", fault)
    try:
        result = scenario(run)
        for name in previous:
            windows.add(replica_window(run, name))
        assert len(windows) == 6  # Three original windows plus three replacements.
        assert result["status"] == "passed"
    finally:
        run.close()
        # Terminal profiles may keep finished windows visible after shell exit.
        for window in windows:
            terminal_script('''on run argv
                tell application id "com.apple.Terminal"
                    set wid to (item 1 of argv) as integer
                    if exists window id wid then
                        set w to window id wid
                        if (count tabs of w) is 1 and not (busy of selected tab of w) then close w saving no
                    end if
                end tell
            end run''', str(window))
