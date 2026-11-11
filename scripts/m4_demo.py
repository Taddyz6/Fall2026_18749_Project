"""Open the eleven M4 processes in separate terminal windows on one computer.

    python scripts/m4_demo.py                   # active mode; then drive the demo by hand
    python scripts/m4_demo.py --mode passive
    python scripts/m4_demo.py --autopilot       # also crashes and relaunches replicas, checking each step
    python scripts/m4_demo.py --stop            # close the windows of the last run

The windows open in the rubric's start order and are tiled in four columns:
RM and GFD | C1-C3 | S1-S3 | LFD1-LFD3. In any process window:

    Ctrl-C                   crash that process (a fault)
    Enter, while it runs     print its status
    Enter, after it stopped  relaunch it (manual recovery)

Windows uses Windows Terminal when available; macOS uses Terminal (untested).
Each run's console logs are saved under logs/demo/<time>-<mode>/.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEMO_LOGS = ROOT / "logs" / "demo"
WINDOWS = sys.platform == "win32"

# name -> (component, extra arguments, tile as fractions of the screen: left, top, width, height)
PROCESSES = {
    "RM": ("rm", [], (0, 0, .25, .5)),
    "GFD": ("gfd", [], (0, .5, .25, .5)),
    **{f"C{i}": ("client", ["--client-id", f"C{i}"], (.25, (i - 1) / 3, .25, 1 / 3)) for i in range(1, 4)},
    **{f"S{i}": ("server", ["--server-id", f"S{i}"], (.5, (i - 1) / 3, .25, 1 / 3)) for i in range(1, 4)},
    **{f"LFD{i}": ("lfd", ["--lfd-id", f"LFD{i}"], (.75, (i - 1) / 3, .25, 1 / 3)) for i in range(1, 4)},
}


# --- Inside one process window -------------------------------------------------

def _prepare_windows_console(title, tile):
    """Title this console, enable ANSI colors, and move its window to its tile."""
    import ctypes
    from ctypes import wintypes

    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    kernel32.SetConsoleTitleW(title)
    handle, mode = kernel32.GetStdHandle(-11), wintypes.DWORD()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # Real pixels on scaled screens.
    except (AttributeError, OSError):
        pass
    window = 0
    for _ in range(50):  # Windows Terminal shows the console title on its own window.
        window = user32.FindWindowW("CASCADIA_HOSTING_WINDOW_CLASS", title)
        if window:
            break
        time.sleep(.1)
    if not window:
        window = kernel32.GetConsoleWindow()  # Classic console window.
    work = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
    width, height = work.right - work.left, work.bottom - work.top
    left, top, w, h = tile
    user32.MoveWindow(window, work.left + int(left * width), work.top + int(top * height),
                      int(w * width), int(h * height), True)


def _wait_for_enter_or_signal(signal_file):
    """Return True on Enter or when the autopilot asks for a relaunch; False on Ctrl-C."""
    try:
        while True:
            if signal_file.exists():
                signal_file.unlink()
                return True
            if WINDOWS:
                import msvcrt
                while msvcrt.kbhit():
                    if msvcrt.getwch() in "\r\n":
                        return True
                time.sleep(.2)
            else:
                import select
                if select.select([sys.stdin], [], [], .2)[0]:
                    sys.stdin.readline()
                    return True
    except KeyboardInterrupt:
        return False


def run_window(name, title, tile, run_dir, command):
    run_dir = Path(run_dir)
    if WINDOWS:
        try:
            _prepare_windows_console(title, tile)
        except OSError:
            pass
    environment = os.environ | {"PYTHONPATH": str(SRC), "PYTHONUNBUFFERED": "1", "FT_LOG_DIR": str(run_dir)}
    relaunch_file = run_dir / "control" / f"{name}.relaunch"
    while True:
        print(f"===== {name}: Ctrl-C crashes it; Enter prints its status =====", flush=True)
        # Ctrl-C reaches both this window and the process; only the process should stop.
        previous = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            child = subprocess.Popen(command, cwd=ROOT, env=environment)
            (run_dir / "pids" / f"{name}.json").write_text(
                json.dumps({"title": title, "window": os.getpid(), "child": child.pid}))
            code = child.wait()
        finally:
            signal.signal(signal.SIGINT, previous)
        print(f"\n***** {name} stopped (exit code {code}). Press Enter to relaunch {name} "
              "(manual recovery), or Ctrl-C to close this window. *****", flush=True)
        if name.startswith("C"):
            print("      Note: a relaunched client starts again at request_num 1, which the replicas "
                  "have already seen. Restart the whole demo instead.", flush=True)
        if not _wait_for_enter_or_signal(relaunch_file):
            return
        print(f"\n***** relaunching {name} *****", flush=True)


# --- The launcher --------------------------------------------------------------

class Demo:
    def __init__(self, mode, config, interval, pace):
        sys.path.insert(0, str(SRC))
        from ft_system.common.config import load_config

        self.mode, self.config_path, self.interval, self.pace = mode, config, interval, pace
        self.config = load_config(config)
        self.run_dir = DEMO_LOGS / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{mode}"
        self.marks = {}

    # Windows and logs

    def check_ports(self):
        listeners = [("RM", self.config.rm.bind_host, self.config.rm.port),
                     ("GFD", self.config.gfd.bind_host, self.config.gfd.port)]
        for server in self.config.servers.values():
            for purpose in ("client_port", "heartbeat_port", "checkpoint_port"):
                listeners.append((f"{server.replica_id} {purpose}", server.bind_host, getattr(server, purpose)))
        busy = []
        for name, host, port in listeners:
            with socket.socket() as sock:
                try:
                    sock.bind((host, port))
                except OSError:
                    busy.append(f"{name} {host}:{port}")
        if busy:
            raise SystemExit("These ports are in use, probably by a previous demo: " + ", ".join(busy)
                             + "\nRun: python scripts/m4_demo.py --stop")

    def open(self, name):
        component, extra, tile = PROCESSES[name]
        title = f"M4 {self.mode} - {name}"
        program = [sys.executable, "-m", f"ft_system.{component}.main", "--config", str(self.config_path), *extra]
        if component == "client":
            program += ["--interval", str(self.interval)]
        command = [sys.executable, str(Path(__file__).resolve()), "--window", name, "--title", title,
                   "--tile", ",".join(f"{value:.4f}" for value in tile), "--run-dir", str(self.run_dir),
                   "--", *program]
        if WINDOWS:
            terminal = shutil.which("wt")
            if terminal:
                subprocess.Popen([terminal, "-w", "new", "--title", title, "-d", str(ROOT), *command])
            else:
                subprocess.Popen(command, cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
        elif sys.platform == "darwin":
            shell = f"cd {shlex.quote(str(ROOT))} && {shlex.join(command)}"
            script = 'tell application "Terminal" to do script "' + shell.replace("\\", "\\\\").replace('"', '\\"') + '"'
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
        else:
            raise SystemExit("opening windows is supported on Windows and macOS; on Linux run the guide's commands")

    def log(self, name):
        path = self.run_dir / f"{name}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def mark(self):
        """Later checks only look at log lines written after this point."""
        self.marks = {name: len(self.log(name)) for name in PROCESSES}

    def since_mark(self, name):
        return self.log(name)[self.marks.get(name, 0):]

    def expect(self, name, text, label, timeout=30, fresh=False):
        deadline = time.monotonic() + timeout
        while True:
            body = self.since_mark(name) if fresh else self.log(name)
            if (text(body) if callable(text) else text in body):
                print(f"  ok    {label}", flush=True)
                return body
            if time.monotonic() > deadline:
                raise SystemExit(f"  FAIL  {label}: not seen in {name}'s window within {timeout} s "
                                 f"(logs: {self.run_dir})")
            time.sleep(.2)

    def expect_progress(self, label, amount=3):
        def count(client):
            return len(re.findall(rf"Received <{client}, S\d, \d+, reply>", self.log(client)))
        start = {client: count(client) for client in ("C1", "C2", "C3")}
        deadline = time.monotonic() + 30
        while not all(count(client) >= start[client] + amount for client in start):
            if time.monotonic() > deadline:
                raise SystemExit(f"  FAIL  {label}: clients stopped making progress (logs: {self.run_dir})")
            time.sleep(.2)
        print(f"  ok    {label}", flush=True)

    def step(self, text):
        print(f"\n>> {text}", flush=True)
        time.sleep(self.pace)
        self.mark()

    def crash(self, name):
        child = json.loads((self.run_dir / "pids" / f"{name}.json").read_text())["child"]
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True)
        else:
            os.kill(child, 9)

    def relaunch(self, name):
        (self.run_dir / "control" / f"{name}.relaunch").touch()

    # Scenarios

    def start(self):
        # Recorded only now, so a failed port check keeps --stop pointed at the previous run.
        for folder in ("pids", "control"):
            (self.run_dir / folder).mkdir(parents=True, exist_ok=True)
        (DEMO_LOGS / "latest.txt").write_text(str(self.run_dir))
        print(f"Opening the eleven M4 {self.mode} windows; logs in {self.run_dir}\n", flush=True)
        self.open("RM")
        self.expect("RM", "RM: 0 members", "RM: 0 members")
        self.open("GFD")
        self.expect("RM", lambda body: body.count("RM: 0 members") >= 2, "GFD registered with the RM; RM: 0 members")
        for i in range(1, 4):
            self.open(f"LFD{i}")
            self.expect("GFD", f"GFD: registered LFD{i}", f"LFD{i} registered with the GFD")
        members = []
        for replica in ("S1", "S2", "S3"):
            members.append(replica)
            self.open(replica)
            count = f"{len(members)} member{'s' if len(members) > 1 else ''}: {', '.join(members)}"
            self.expect("GFD", f"GFD: {count}", f"GFD: {count}")
            self.expect("RM", f"RM: {count}", f"RM: {count}")
            self.expect(replica, "i_am_ready = 1", f"{replica}: i_am_ready = 1")
        for client in ("C1", "C2", "C3"):
            self.open(client)
            self.expect(client, f"Received <{client},", f"{client} is sending requests and receiving replies")
        if self.mode == "passive":
            self.expect("RM", "RM: primary is S1", "RM: primary is S1")

    def autopilot_active(self):
        self.step("Fault-free: every client gets three replies per request and discards two")
        self.expect("C1", lambda body: len(set(re.findall(r"Discarded duplicate reply from (S\d)", body))) == 2,
                    "C1 discards duplicates from two replicas", fresh=True)
        self.step("Fault: crash S2")
        self.crash("S2")
        self.expect("LFD2", "S2 has died", "LFD2: S2 has died", fresh=True)
        self.expect("RM", "RM: 2 members: S1, S3", "GFD and RM: 2 members: S1, S3", fresh=True)
        self.expect_progress("clients keep running on S1 and S3")
        self.step("Manual recovery: relaunch S2")
        self.relaunch("S2")
        self.expect("RM", "RM: 3 members: S1, S2, S3", "RM: 3 members: S1, S2, S3", fresh=True)
        self.expect("S2", "i_am_ready = 0; logged <", "S2 logs requests without replying (i_am_ready = 0)",
                    fresh=True)
        self.expect("S1", "sending checkpoint", "S1 sends its checkpoint to S2", fresh=True)
        self.expect("S2", "i_am_ready = 1 (checkpoint installed)", "S2: i_am_ready = 1 (checkpoint installed)",
                    fresh=True)
        self.expect("C1", "Discarded duplicate reply from S2", "clients receive S2's replies again", fresh=True)

    def autopilot_passive(self):
        self.step("Fault-free: only the primary S1 replies; S1 checkpoints S2 and S3")
        self.expect("S2", "received checkpoint", "S2 receives S1's checkpoints", fresh=True)
        self.step("Backup fault: crash S2")
        self.crash("S2")
        self.expect("RM", "RM: 2 members: S1, S3", "GFD and RM: 2 members: S1, S3", fresh=True)
        self.expect_progress("clients keep running on the primary S1")
        self.step("Backup recovery: relaunch S2")
        self.relaunch("S2")
        self.expect("S2", "role = backup (primary is S1)", "S2 rejoins as a backup", fresh=True)
        self.expect("S2", "i_am_ready = 1 (checkpoint installed)", "S2 becomes ready from S1's checkpoint",
                    fresh=True)
        self.step("Primary fault: crash S1")
        self.crash("S1")
        body = self.expect("RM", "new primary is", "RM elects a new primary", fresh=True)
        primary = re.search(r"new primary is (S\d)", body).group(1)
        print(f"        new primary: {primary} (the ready backup that joined earliest)", flush=True)
        self.expect("C1", f"primary changed S1 -> {primary}", f"clients switch to {primary}", fresh=True)
        self.expect("C1", f"Received <C1, {primary},", f"{primary} answers the clients", fresh=True)
        self.step("Recovery of the old primary: relaunch S1")
        self.relaunch("S1")
        self.expect("S1", f"role = backup (primary is {primary})", f"S1 returns as a backup of {primary}",
                    fresh=True)
        self.expect("S1", "i_am_ready = 1 (checkpoint installed)", f"S1 becomes ready from {primary}'s checkpoint",
                    fresh=True)

    def finish_autopilot(self):
        self.expect_progress("clients still making progress", amount=5)
        for client in ("C1", "C2", "C3"):
            if "divergence" in self.log(client):
                raise SystemExit(f"  FAIL  {client} reported replica divergence (logs: {self.run_dir})")
        print("  ok    no client saw diverging replicas", flush=True)
        print("\nAUTOPILOT PASSED. The windows stay open for inspection; close them with:\n"
              "  python scripts/m4_demo.py --stop", flush=True)

    def instructions(self):
        if self.mode == "active":
            steps = ["Ctrl-C in the S2 window: LFD2 reports the crash; GFD and RM show 2 members; clients keep going.",
                     "Enter in the S2 window: S2 relaunches, logs requests with i_am_ready = 0, gets S1's "
                     "checkpoint, and becomes ready.",
                     "Enter in the RM window at any time prints the RM's status."]
        else:
            steps = ["Ctrl-C in S2 (a backup), then Enter to relaunch it: it rejoins as a backup.",
                     "Ctrl-C in S1 (the primary): RM elects a new primary; clients follow it.",
                     "Enter in S1's window: S1 returns as a backup of the new primary."]
        print("\nAll eleven processes are running. Try:", flush=True)
        for number, text in enumerate(steps, 1):
            print(f"  {number}. {text}", flush=True)
        print("Close everything with: python scripts/m4_demo.py --stop\n"
              f"Merge the logs afterwards with: python scripts/merge_logs.py {self.run_dir} --events", flush=True)


def stop():
    latest = DEMO_LOGS / "latest.txt"
    if not latest.exists():
        raise SystemExit("no demo run recorded")
    run_dir = Path(latest.read_text().strip())
    for record in sorted((run_dir / "pids").glob("*.json")):
        info = json.loads(record.read_text())
        if WINDOWS:
            import ctypes
            window = ctypes.windll.user32.FindWindowW("CASCADIA_HOSTING_WINDOW_CLASS", info["title"])
            if window:
                ctypes.windll.user32.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(info["window"])], capture_output=True)
        else:
            for pid in (info["child"], info["window"]):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
        print(f"stopped {record.stem}")


def main():
    if "--window" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--window")
        parser.add_argument("--title")
        parser.add_argument("--tile")
        parser.add_argument("--run-dir")
        split = sys.argv.index("--")
        args = parser.parse_args(sys.argv[1:split])
        run_window(args.window, args.title, tuple(map(float, args.tile.split(","))), args.run_dir,
                   sys.argv[split + 1:])
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["active", "passive"], default="active")
    parser.add_argument("--config", type=Path, help="default: configs/m4.<mode>.local.toml")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between each client's requests")
    parser.add_argument("--autopilot", action="store_true", help="perform the rubric's faults and check each step")
    parser.add_argument("--pace", type=float, default=3.0, help="autopilot pause before each step, in seconds")
    parser.add_argument("--stop", action="store_true", help="close the windows of the last run")
    args = parser.parse_args()
    if args.stop:
        stop()
        return
    config = (args.config or ROOT / "configs" / f"m4.{args.mode}.local.toml").resolve()
    demo = Demo(args.mode, config, args.interval, args.pace)
    if demo.config.rm is None or demo.config.replication is None or demo.config.replication.mode != args.mode:
        raise SystemExit(f"{config} is not an M4 {args.mode} config")
    demo.check_ports()
    demo.start()
    if args.autopilot:
        demo.autopilot_active() if args.mode == "active" else demo.autopilot_passive()
        demo.finish_autopilot()
    else:
        demo.instructions()


if __name__ == "__main__":
    main()
