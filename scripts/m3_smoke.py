"""Exercise ten M3 processes: passive checkpoints and two sequential backup failures."""

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def reserve_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "logs" / "m3-smoke")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ports = reserve_ports(10)
    config = (ROOT / "configs" / "m3.local.toml").read_text()
    for old, new in zip([7100, 5101, 6101, 8101, 5102, 6102, 8102, 5103, 6103, 8103], ports):
        config = config.replace(f"= {old}\n", f"= {new}\n")
    config = config.replace("checkpoint_freq = 0.5", "checkpoint_freq = 0.08")
    config = config.replace("heartbeat_freq = 1.0", "heartbeat_freq = 0.08")
    config = config.replace("read_timeout = 1.0", "read_timeout = 0.25")
    config_path = output / "deployment.toml"
    config_path.write_text(config)
    environment = os.environ | {"PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1"}
    processes, handles = {}, []

    def launch(name, component, *extra):
        handle = (output / f"{name}.log").open("w")
        handles.append(handle)
        processes[name] = subprocess.Popen(
            [sys.executable, "-m", f"ft_system.{component}.main", "--config", str(config_path), *extra],
            cwd=ROOT, env=environment, stdout=handle, stderr=subprocess.STDOUT,
        )

    def log(name):
        return (output / f"{name}.log").read_text()

    def wait_for(predicate, label, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"timeout: {label}; inspect {output}")
            time.sleep(.02)

    try:
        launch("GFD", "gfd")
        wait_for(lambda: "GFD: 0 members" in log("GFD"), "GFD startup")
        for i in range(1, 4):
            launch(f"LFD{i}", "lfd", "--lfd-id", f"LFD{i}")
        for i in range(1, 4):
            launch(f"S{i}", "server", "--server-id", f"S{i}")
        wait_for(lambda: "GFD: 3 members: S1, S2, S3" in log("GFD"), "all members")
        for i in range(1, 4):
            launch(f"C{i}", "client", "--client-id", f"C{i}", "--interval", "0.03")
        wait_for(lambda: all(log(f"C{i}").count(f"Received <C{i}, S1,") >= 5 for i in range(1, 4)), "primary replies")
        wait_for(lambda: "S1 sending checkpoint" in log("S1") and "S2 received checkpoint" in log("S2") and "S3 received checkpoint" in log("S3"), "checkpoint exchange")
        processes["S2"].send_signal(signal.SIGINT)
        processes["S2"].wait(timeout=3)
        wait_for(lambda: "GFD: 2 members: S1, S3" in log("GFD"), "S2 removal")
        wait_for(lambda: all(log(f"C{i}").count(f"Received <C{i}, S1,") >= 10 for i in range(1, 4)), "progress after S2")
        processes["S3"].send_signal(signal.SIGINT)
        processes["S3"].wait(timeout=3)
        wait_for(lambda: "GFD: 1 member: S1" in log("GFD"), "S3 removal")
        wait_for(lambda: all(log(f"C{i}").count(f"Received <C{i}, S1,") >= 15 for i in range(1, 4)), "progress after S3")
        assert "Sending <C1, S2," not in log("C1") and "Sending <C1, S3," not in log("C1")
        print(f"PASS: M3 evidence saved to {output}")
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
