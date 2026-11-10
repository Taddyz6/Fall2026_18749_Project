"""Run real M5 processes and inject three successive crashes in each replication mode."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
REPLICAS = ("S1", "S2", "S3")
CLIENTS = ("C1", "C2", "C3")
REQUESTS = 240
CONFIG_PORTS = (7300, 7200, 5201, 6201, 8201, 5202, 6202, 8202, 5203, 6203, 8203)


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def process_alive(pid):
    # os.kill(pid, 0) is a non-destructive probe on POSIX only.
    if os.name != "posix":
        raise RuntimeError("Run the M5 process smoke test on macOS, Linux, or WSL")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class Run:
    def __init__(self, mode, output, first="S1"):
        if os.name != "posix":
            raise RuntimeError("Run the M5 process smoke test on macOS, Linux, or WSL")
        self.mode, self.first, self.output = mode, first, Path(output).resolve()
        # A fresh directory prevents old logs from satisfying current assertions.
        self.output.mkdir(parents=True, exist_ok=False)
        config = (ROOT / "configs" / f"m5.{mode}.local.toml").read_text()
        self.ports = free_ports(len(CONFIG_PORTS))
        for old, new in zip(CONFIG_PORTS, self.ports):
            config = config.replace(f"= {old}\n", f"= {new}\n")
        for old, new in [("transfer_delay = 2.0", "transfer_delay = 0.2"),
                         ("restart_delay = 1.0", "restart_delay = 0.5"),
                         ("restart_retry = 1.0", "restart_retry = 0.2"),
                         ("heartbeat_freq = 1.0", "heartbeat_freq = 0.05"),
                         ("checkpoint_freq = 1.0", "checkpoint_freq = 0.05"),
                         ("connect_timeout = 1.0", "connect_timeout = 0.2"),
                         ("read_timeout = 1.0", "read_timeout = 0.3")]:
            config = config.replace(old, new)
        self.config_path = self.output / "deployment.toml"
        self.config_path.write_text(config, encoding="utf-8")
        self.environment = os.environ | {"PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1"}
        self.processes, self.handles, self.rounds = {}, [], []

    def launch(self, name, component, *args):
        assert component != "server", "Only the LFD may start/restart replicas in an M5 test"
        output = (self.output / f"{name}.log").open("w", encoding="utf-8")
        self.handles.append(output)
        self.processes[name] = subprocess.Popen(
            [sys.executable, "-m", f"ft_system.{component}.main", "--config", str(self.config_path), *args],
            cwd=ROOT, env=self.environment, stdout=output, stderr=subprocess.STDOUT,
        )

    def log(self, name):
        path = self.output / f"{name}.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def pid(self, replica):
        try:
            return int((self.output / f"{replica}.pid").read_text())
        except FileNotFoundError:
            return None

    def wait_for(self, predicate, label, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            failed = {name: p.returncode for name, p in self.processes.items()
                      if p.poll() is not None and (name not in CLIENTS or p.returncode != 0)}
            if failed:
                raise AssertionError(f"unexpected process exits {failed}; inspect {self.output}")
            if time.monotonic() >= deadline:
                raise AssertionError(f"[{self.mode}] timeout: {label}; inspect {self.output}")
            time.sleep(.02)

    def replies(self, client):
        text = self.log(client)
        if self.mode == "active":
            return [(int(n), int(value)) for n, value in re.findall(
                r"request_num (\d+): Delivered reply from \S+; client_value = (\d+)", text)]
        return [(int(n), None) for n in re.findall(rf"Received <{client}, S\d+, (\d+), reply>", text)]

    def counts(self):
        return {client: len(self.replies(client)) for client in CLIENTS}

    def progress(self, before, amount=3):
        self.wait_for(lambda: all(self.counts()[c] >= before[c] + amount for c in CLIENTS),
                      "all three clients continue receiving replies")

    def ready(self):
        lines = [line for line in self.log("RM").splitlines() if "RM: view " in line]
        return bool(lines) and "ready: S1, S2, S3" in lines[-1]

    def primary(self):
        values = re.findall(r"RM: view .*; primary: (S\d+)", self.log("RM"))
        return values[-1] if values else None

    def bootstrap(self):
        self.launch("RM", "rm")
        self.wait_for(lambda: "RM: 0 members" in self.log("RM"), "clean RM startup")
        self.launch("GFD", "gfd")
        self.wait_for(lambda: self.log("RM").count("RM: 0 members") >= 2, "initial GFD membership")
        order = [self.first] + [r for r in REPLICAS if r != self.first]
        for replica in order:
            lfd = f"LFD{replica[1:]}"
            self.launch(lfd, "lfd", "--lfd-id", lfd,
                        "--replica-log", str(self.output / f"{replica}.log"))
            self.wait_for(lambda: f"RM: {replica} is ready" in self.log("RM"), f"{replica} initially ready")
        self.wait_for(self.ready, "three ready replicas")
        if self.mode == "passive":
            assert self.primary() == self.first
        for client in CLIENTS:
            self.launch(client, "client", "--client-id", client, "--interval", "0.025",
                        "--count", str(REQUESTS))
        self.progress({c: 0 for c in CLIENTS}, 5)

    def crash_and_recover(self, replica):
        old_pid = self.pid(replica)
        assert old_pid is not None
        offsets = {name: len(self.log(name)) for name in ("RM", replica, *CLIENTS)}
        before = self.counts()
        started = time.monotonic()
        # Actual crash fault. No test-harness call starts the replacement.
        os.kill(old_pid, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
        rm_log = lambda: self.log("RM")[offsets["RM"]:]
        self.wait_for(lambda: "RM: 2 members" in rm_log(), f"{replica} removal")
        # With restart_delay=0.5, all clients must progress while the replica is absent.
        self.progress(self.counts(), 1)
        assert self.pid(replica) in (None, old_pid), "progress must occur before the replacement starts"
        self.wait_for(lambda: self.pid(replica) not in (None, old_pid), f"{replica} auto-relaunch")
        new_pid = self.pid(replica)
        self.wait_for(lambda: self.ready() and f"RM: {replica} is ready" in rm_log(), "three ready replicas again")
        recovered = self.log(replica)[offsets[replica]:]
        assert "i_am_ready = 0 (waiting for the RM)" in recovered
        recovered = recovered[recovered.index("i_am_ready = 0 (waiting for the RM)"):]
        assert "i_am_ready = 1 (checkpoint installed)" in recovered
        assert "RM: 3 members: S1, S2, S3" in rm_log()
        self.progress(before, 5)
        if self.mode == "active":
            assert "logged <C" in recovered, "recovering active replica must log before replying"
            assert recovered.index("logged <C") < recovered.index("i_am_ready = 1 (checkpoint installed)")
            self.wait_for(lambda: all(re.search(rf"Received <{c}, {replica}, \d+, reply>",
                                               self.log(c)[offsets[c]:]) for c in CLIENTS),
                          "clients receive responses after recovery")
            recovered = self.log(replica)[offsets[replica]:]
            recovered = recovered[recovered.index("i_am_ready = 0 (waiting for the RM)"):]
            assert recovered.index("Sending <C") > recovered.index("i_am_ready = 1 (checkpoint installed)")
        else:
            primary = self.primary()
            assert primary != replica
            assert f"role = backup (primary is {primary})" in recovered
            assert re.search(rf"received checkpoint .* from {primary}", recovered)
            assert not re.search(r"Sending <C\d+,", recovered)
        self.rounds.append({"replica": replica, "old_pid": old_pid, "new_pid": new_pid,
                            "seconds_until_ready": round(time.monotonic() - started, 3),
                            "replies_before": before, "replies_after": self.counts(),
                            "primary": self.primary()})

    def state(self, replica):
        snapshots = re.findall(rf"(?:my_state_{replica} = |received checkpoint <my_state = )(\{{[^\n]*?\}})",
                               self.log(replica))
        return json.loads(snapshots[-1]) if snapshots else None

    def verify_final(self):
        self.wait_for(lambda: all(self.processes[c].poll() == 0 for c in CLIENTS), "clients finish workload", 25)
        for client in CLIENTS:
            replies = self.replies(client)
            assert [n for n, _ in replies] == list(range(1, REQUESTS + 1)), client
            assert "divergence" not in self.log(client)
            if self.mode == "active":
                assert all(n == value for n, value in replies)
                assert "Discarded duplicate reply" in self.log(client)
        expected = {client: REQUESTS for client in CLIENTS}
        if self.mode == "active":
            self.wait_for(lambda: all(self.state(r) == expected for r in REPLICAS), "final active state consistency")
        else:
            self.wait_for(lambda: len({json.dumps(self.state(r), sort_keys=True) for r in REPLICAS}) == 1
                          and self.state(self.primary()) is not None, "final passive checkpoint consistency")
        gaps = {}
        for client in CLIENTS:
            token = "Delivered reply" if self.mode == "active" else f"Received <{client},"
            times = [datetime.fromisoformat(line[1:24]) for line in self.log(client).splitlines() if token in line]
            gaps[client] = max((b - a).total_seconds() for a, b in zip(times, times[1:]))
            assert gaps[client] < 5, f"{client} stalled for {gaps[client]} seconds"
        return {"mode": self.mode, "initial_replica": self.first, "status": "passed",
                "rounds": self.rounds, "replies": self.counts(), "max_reply_gap_seconds": gaps,
                "final_states": {r: self.state(r) for r in REPLICAS}}

    def close(self):
        # Stop RM first so teardown cannot trigger a new recovery. LFDs own/reap servers.
        names = [name for name in ("RM", *CLIENTS, "LFD1", "LFD2", "LFD3", "GFD") if name in self.processes]
        children = [pid for replica in REPLICAS if (pid := self.pid(replica)) is not None]
        try:
            for name in names:
                process = self.processes[name]
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            # A failed LFD shutdown must not leave this test's server processes behind.
            remaining = [pid for pid in children if process_alive(pid)]
            for pid in remaining:
                os.kill(pid, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
            assert not remaining, f"LFD shutdown leaked children: {remaining}"
            assert not list(self.output.glob("S*.pid")), "stale PID files after LFD shutdown"
        finally:
            for handle in self.handles:
                handle.close()


def scenario(run):
    run.bootstrap()
    target = run.first
    run.crash_and_recover(target)
    if run.mode == "passive":
        new_primary = run.primary()
        assert new_primary != target
        for client in CLIENTS:
            assert f"primary changed {target} -> {new_primary}" in run.log(client)
    run.crash_and_recover(target)
    other = next(r for r in REPLICAS if r != target and (run.mode == "active" or r != run.primary()))
    run.crash_and_recover(other)
    return run.verify_final()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("active", "passive", "both"), default="both")
    parser.add_argument("--first", choices=REPLICAS, default="S1", help="Initial primary in passive mode")
    parser.add_argument("--output", type=Path, default=ROOT / "logs/m5-smoke")
    args = parser.parse_args()
    output = args.output.resolve() / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for mode in (("active", "passive") if args.mode == "both" else (args.mode,)):
        run = Run(mode, output / mode, args.first)
        result = {"mode": mode, "status": "failed"}
        try:
            result = scenario(run)
        except BaseException as exc:
            result["error"] = str(exc) or type(exc).__name__
            raise
        finally:
            try:
                run.close()
            except BaseException as exc:
                result.update(status="failed", cleanup_error=str(exc))
                raise
            finally:
                (run.output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"PASS: M5 {mode}, three auto-recoveries; evidence: {run.output}")


if __name__ == "__main__":
    main()
