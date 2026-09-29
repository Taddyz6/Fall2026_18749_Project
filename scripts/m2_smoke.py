"""Run ten independent processes and inject two consecutive replica crashes."""

import argparse
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


def reserve_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "logs" / "m2-smoke")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ports = reserve_ports(7)
    config = (ROOT / "configs" / "m2.local.toml").read_text()
    for old, new in zip([7000, 5001, 6001, 5002, 6002, 5003, 6003], ports):
        config = config.replace(f"= {old}\n", f"= {new}\n")
    config = config.replace("heartbeat_freq = 1.0", "heartbeat_freq = 0.1")
    config = config.replace("read_timeout = 1.0", "read_timeout = 0.3")
    config_path = output / "deployment.toml"
    config_path.write_text(config)
    processes = {}
    handles = []
    events = []
    started = time.monotonic()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUNBUFFERED"] = "1"

    def record(text):
        event = {"elapsed_seconds": round(time.monotonic() - started, 3), "event": text}
        events.append(event)
        print(text, flush=True)

    def launch(name, component, *extra):
        stream = (output / f"{name}.log").open("w")
        handles.append(stream)
        processes[name] = subprocess.Popen(
            [sys.executable, "-m", f"ft_system.{component}.main", "--config", str(config_path), *extra],
            cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)

    def log(name):
        return (output / f"{name}.log").read_text()

    def wait_for(predicate, label, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"timeout: {label}; inspect logs in {output}")
            time.sleep(.02)

    def delivered(name):
        return log(name).count("Delivered reply")

    try:
        launch("GFD", "gfd")
        wait_for(lambda: "GFD: 0 members" in log("GFD"), "GFD startup")
        for i in range(1, 4):
            launch(f"LFD{i}", "lfd", "--lfd-id", f"LFD{i}")
        wait_for(lambda: all(f"registered LFD{i}" in log("GFD") for i in range(1, 4)), "LFD registration")
        assert "add replica" not in log("GFD")
        record("All LFDs registered; GFD still has zero members")
        for i in range(1, 4):
            launch(f"S{i}", "server", "--server-id", f"S{i}")
            members = ", ".join(f"S{j}" for j in range(1, i + 1))
            text = f"GFD: {i} {'member' if i == 1 else 'members'}: {members}"
            wait_for(lambda text=text: text in log("GFD"), text)
        record("GFD membership reached S1, S2, S3 through successful local heartbeats")
        for i in range(1, 4):
            launch(f"C{i}", "client", "--client-id", f"C{i}", "--interval", "0.08", "--count", "60")
        wait_for(lambda: all(delivered(f"C{i}") >= 8 for i in range(1, 4)), "initial replies")
        for i in range(1, 4):
            assert "Discarded duplicate reply" in log(f"C{i}")
        record("All three clients are receiving replies and discarding duplicates")
        processes["S1"].send_signal(signal.SIGINT)
        assert processes["S1"].wait(timeout=3) == 0
        wait_for(lambda: "GFD: 2 members: S2, S3" in log("GFD"), "S1 removal")
        record("Stopped S1 with SIGINT; GFD membership is S2, S3")
        wait_for(lambda: all(delivered(f"C{i}") >= 24 for i in range(1, 4)), "progress after S1 failure")
        processes["S2"].kill()
        processes["S2"].wait(timeout=3)
        wait_for(lambda: "GFD: 1 member: S3" in log("GFD"), "S2 removal")
        record("Killed S2; GFD membership is S3")
        for i in range(1, 4):
            assert processes[f"C{i}"].wait(timeout=12) == 0
            text = log(f"C{i}")
            nums = [int(n) for n in re.findall(r"request_num (\d+): Delivered reply", text)]
            assert nums == list(range(1, 61)), (i, nums)
            assert f"Sending <C{i}, S3, 60, request>" in text
            assert f"Received <C{i}, S3, 60, reply>" in text
        states = re.findall(r"my_state_S3 = (\{.*?\}) after processing", log("S3"))
        state = json.loads(states[-1])
        assert state == {"C1": 60, "C2": 60, "C3": 60}, state
        for i in (1, 2):
            assert f"S{i} has died" in log(f"LFD{i}")
            assert log("GFD").count(f"delete replica S{i}") == 1
        record("Each client completed requests 1-60 exactly once; S3 state is 60,60,60")
        result = {"result": "PASS", "process_count": 10, "transport": "TCP over loopback",
                  "injected_faults": ["S1 SIGINT", "S2 SIGKILL"],
                  "completed_requests_per_client": 60, "survivor_state": state, "events": events}
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"PASS: evidence saved to {output}")
    finally:
        for proc in processes.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in processes.values():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for stream in handles:
            stream.close()


if __name__ == "__main__":
    main()
