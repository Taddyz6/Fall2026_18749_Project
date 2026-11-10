"""Exercise the eleven M4 processes (RM, GFD, LFD1-3, S1-3, C1-3) through kills and manual relaunches."""

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
# RM, GFD, then client/heartbeat/checkpoint ports of S1-S3 in configs/m4.*.local.toml.
CONFIG_PORTS = [7300, 7200, 5201, 6201, 8201, 5202, 6202, 8202, 5203, 6203, 8203]


def reserve_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class Run:
    def __init__(self, mode, output, demo_timing=False):
        self.mode, self.output = mode, output
        # demo_timing keeps the config's real timers and one request per second, as in the live demo.
        self.interval = "1" if demo_timing else "0.05"
        output.mkdir(parents=True, exist_ok=True)
        config = (ROOT / "configs" / f"m4.{mode}.local.toml").read_text()
        for old, new in zip(CONFIG_PORTS, reserve_ports(len(CONFIG_PORTS))):
            config = config.replace(f"= {old}\n", f"= {new}\n")
        for old, new in [("transfer_delay = 2.0", "transfer_delay = 0.5"),
                         ("heartbeat_freq = 1.0", "heartbeat_freq = 0.1"),
                         ("checkpoint_freq = 1.0", "checkpoint_freq = 0.1"),
                         ("read_timeout = 1.0", "read_timeout = 0.3")]:
            if not demo_timing:
                config = config.replace(old, new)
        self.config_path = output / "deployment.toml"
        self.config_path.write_text(config)
        # Output is already captured per process, so skip the logs/console copies.
        self.environment = os.environ | {"PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1",
                                         "FT_LOG_DIR": "off"}
        self.processes, self.handles = {}, []

    def launch(self, name, component, *extra, log=None):
        handle = (self.output / f"{log or name}.log").open("w")
        self.handles.append(handle)
        self.processes[name] = subprocess.Popen(
            [sys.executable, "-m", f"ft_system.{component}.main", "--config", str(self.config_path), *extra],
            cwd=ROOT, env=self.environment, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
        )

    def crash(self, name):
        # A hard kill models a crash fault on every platform (SIGINT is unavailable on Windows).
        self.processes[name].kill()
        self.processes[name].wait(timeout=5)

    def log(self, name):
        return (self.output / f"{name}.log").read_text()

    def wait_for(self, predicate, label, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"[{self.mode}] timeout: {label}; inspect {self.output}")
            time.sleep(.05)

    def deliveries(self, client):
        return self.log(client).count("Delivered reply" if self.mode == "active" else "Received <")

    def wait_progress(self, label, amount=5):
        start = {f"C{i}": self.deliveries(f"C{i}") for i in range(1, 4)}
        self.wait_for(lambda: all(self.deliveries(c) >= start[c] + amount for c in start), label)

    def bootstrap(self):
        self.launch("RM", "rm")
        self.wait_for(lambda: "RM: 0 members" in self.log("RM"), "RM startup")
        self.launch("GFD", "gfd")
        self.wait_for(lambda: self.log("RM").count("RM: 0 members") >= 2, "GFD reports to RM")
        for i in range(1, 4):
            self.launch(f"LFD{i}", "lfd", "--lfd-id", f"LFD{i}")
            self.wait_for(lambda: f"GFD: registered LFD{i}" in self.log("GFD"), f"LFD{i} registration")
        for members in ("S1", "S1, S2", "S1, S2, S3"):
            replica = members[-2:]
            self.launch(replica, "server", "--server-id", replica)
            self.wait_for(lambda: f"RM: {members.count('S')} member{'s' if ',' in members else ''}: {members}"
                          in self.log("RM"), f"{replica} membership")
        for replica in ("S1", "S2", "S3"):
            self.wait_for(lambda: "i_am_ready = 1" in self.log(replica), f"{replica} ready")
        for i in range(1, 4):
            self.launch(f"C{i}", "client", "--client-id", f"C{i}", "--interval", self.interval)
        self.wait_progress("fault-free traffic")

    def close(self):
        for process in self.processes.values():
            if process.poll() is None:
                process.kill()
        for process in self.processes.values():
            process.wait(timeout=5)
        for handle in self.handles:
            handle.close()


def active_scenario(run):
    run.bootstrap()
    assert "RM: S1 is the first replica" in run.log("RM")
    assert "S1: secondary channel to S2 open" in run.log("S1")

    run.crash("S2")
    run.wait_for(lambda: "RM: 2 members: S1, S3" in run.log("RM"), "S2 removal")
    run.wait_progress("traffic with S2 down")
    duplicates_before = run.log("C1").count("Discarded duplicate reply from S2")

    run.launch("S2", "server", "--server-id", "S2", log="S2-relaunch")
    run.wait_for(lambda: run.log("RM").count("RM: 3 members: S1, S2, S3") >= 2, "S2 rejoins")
    run.wait_for(lambda: "i_am_ready = 0; logged <C" in run.log("S2-relaunch"), "S2 logs without replying")
    run.wait_for(lambda: "i_am_ready = 1 (checkpoint installed)" in run.log("S2-relaunch"), "S2 state transfer")
    run.wait_for(lambda: run.log("C1").count("Discarded duplicate reply from S2") > duplicates_before + 3,
                 "S2 answering again")

    run.crash("S1")  # A second fault: S2 must now carry correct state with S3.
    run.wait_for(lambda: "RM: 2 members: S2, S3" in run.log("RM"), "S1 removal")
    run.wait_progress("traffic served by S2 and S3")
    for i in range(1, 4):
        assert "divergence" not in run.log(f"C{i}"), f"C{i} saw diverging replicas"


def passive_scenario(run):
    run.bootstrap()
    assert "RM: primary is S1" in run.log("RM")
    assert "Received <C1, S2," not in run.log("C1")

    run.crash("S2")
    run.wait_for(lambda: "RM: 2 members: S1, S3" in run.log("RM"), "backup S2 removal")
    run.wait_progress("traffic with backup S2 down")
    run.launch("S2", "server", "--server-id", "S2", log="S2-relaunch")
    run.wait_for(lambda: "S2 received checkpoint" in run.log("S2-relaunch")
                 and "i_am_ready = 1 (checkpoint installed)" in run.log("S2-relaunch"), "S2 rejoins as backup")

    run.crash("S1")
    marker = "RM: primary S1 failed; new primary is "
    run.wait_for(lambda: marker in run.log("RM"), "primary election")
    # The oldest ready backup wins; S2 was just relaunched, so this is normally S3.
    primary = run.log("RM").split(marker, 1)[1][:2]
    backup, backup_log = ("S2", "S2-relaunch") if primary == "S3" else ("S3", "S3")
    run.wait_for(lambda: all(f"primary changed S1 -> {primary}" in run.log(f"C{i}") for i in range(1, 4)),
                 "clients redirect")
    run.wait_for(lambda: all(f"Received <C{i}, {primary}," in run.log(f"C{i}") for i in range(1, 4)),
                 f"{primary} replies")
    run.wait_for(lambda: any(f"{backup} received checkpoint" in line and line.endswith(f"from {primary}")
                             for line in run.log(backup_log).splitlines()), f"{primary} checkpoints {backup}")
    run.launch("S1", "server", "--server-id", "S1", log="S1-relaunch")
    run.wait_for(lambda: f"S1: role = backup (primary is {primary})" in run.log("S1-relaunch")
                 and "i_am_ready = 1 (checkpoint installed)" in run.log("S1-relaunch"), "S1 returns as backup")
    run.wait_progress("traffic after both recoveries")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["active", "passive", "both"], default="both")
    parser.add_argument("--output", type=Path, default=ROOT / "logs" / "m4-smoke")
    parser.add_argument("--demo-timing", action="store_true",
                        help="use the config's real timers and one request per second (several minutes)")
    args = parser.parse_args()
    for mode in (["active", "passive"] if args.mode == "both" else [args.mode]):
        run = Run(mode, args.output.resolve() / mode, args.demo_timing)
        try:
            (active_scenario if mode == "active" else passive_scenario)(run)
        finally:
            run.close()
        print(f"PASS: M4 {mode} evidence saved to {run.output}")


if __name__ == "__main__":
    main()
