"""Randomized M4 fault injection: kill and relaunch replicas at random under client load.

Runs the eleven real processes on loopback with short timers, then repeats
random kills (sometimes two replicas at once) and manual relaunches with random
timing. A replica is only killed while another live replica is ready, so
correct state always survives and these invariants must hold:

  active   every delivered reply for request n carries client_value n; no client
           reports replica divergence; final replica states agree
  passive  each client's replies are numbered consecutively; every membership
           has a primary; after traffic stops, backups equal the primary
  both     clients keep making progress in every cycle; no process crashes

    python scripts/m4_soak.py --mode both --cycles 30 --seed 1
"""

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m4_smoke import ROOT, Run  # noqa: E402


REPLICAS = ("S1", "S2", "S3")
CLIENTS = ("C1", "C2", "C3")
DELIVERED = re.compile(r"request_num (\d+): Delivered reply from (\S+); client_value = (\d+)")
RECEIVED = re.compile(r"Received <(C\d), (S\d), (\d+), reply>")
AFTER = re.compile(r"my_state_S\d = (\{.*?\}) after processing")
CHECKPOINT_IN = re.compile(r"S\d received checkpoint <my_state = (\{.*?\}), checkpoint_count = (\d+)> from (S\d)")
VIEW = re.compile(r"RM: view (\d+): members: (.*?); ready: (.*?)(?:; primary: (\S+))?$")
READINESS = re.compile(r"S\d: i_am_ready = ([01]) \(")


class Soak(Run):
    def __init__(self, mode, output, rng, interval="0.05"):
        # Fails here, before touching the shared config, if another run is using this folder.
        for stale in output.glob("*.log"):
            stale.unlink()
        super().__init__(mode, output)
        self.interval = interval
        self.rng = rng
        self.instance = {}  # replica -> log name of its current process
        self.launches = {replica: 0 for replica in REPLICAS}
        self.down = set()
        self.events = []

    def note(self, text):
        self.events.append(f"{time.strftime('%H:%M:%S')} {text}")
        print(f"[{self.mode}] {text}", flush=True)

    def launch(self, name, component, *extra, log=None):
        if component == "server":
            self.launches[name] += 1
            log = name if self.launches[name] == 1 else f"{name}-{self.launches[name]}"
            self.instance[name] = log
            self.down.discard(name)
        super().launch(name, component, *extra, log=log)

    def instance_ready(self, replica):
        states = READINESS.findall(self.log(self.instance[replica]))
        return bool(states) and states[-1] == "1"

    def killable(self):
        alive = [replica for replica in REPLICAS if replica not in self.down]
        return [victim for victim in alive
                if any(other != victim and self.instance_ready(other) for other in alive)]

    def kill(self, replica):
        self.note(f"kill {replica} (ready={self.instance_ready(replica)})")
        self.crash(replica)
        self.down.add(replica)

    def relaunch(self, replica):
        self.note(f"relaunch {replica}")
        self.launch(replica, "server", "--server-id", replica)

    def check_processes(self):
        for name, process in self.processes.items():
            if name not in self.down and process.poll() is not None:
                raise AssertionError(f"[{self.mode}] {name} exited with code {process.returncode}")
        for file in self.output.glob("*.log"):
            if "Traceback" in file.read_text(errors="replace"):
                raise AssertionError(f"[{self.mode}] exception in {file}")

    def check_clients(self):
        for client in CLIENTS:
            text = self.log(client)
            assert "divergence" not in text, f"[{self.mode}] {client} saw diverging replicas"
            if self.mode == "active":
                for number, replica, value in DELIVERED.findall(text):
                    assert number == value, (f"[active] {client} request_num {number} got client_value "
                                             f"{value} from {replica}: an update was lost or repeated")
            else:
                numbers = [int(number) for who, _, number in RECEIVED.findall(text) if who == client]
                assert numbers == list(range(1, len(numbers) + 1)), f"[passive] {client} replies out of sequence"

    def check_views(self):
        for line in self.log("RM").splitlines():
            match = VIEW.search(line)
            if match and self.mode == "passive" and match.group(2) != "-":
                assert match.group(4) not in (None, "-"), f"[passive] view without a primary: {line}"

    def all_ready(self):
        views = [VIEW.search(line) for line in self.log("RM").splitlines()]
        views = [view for view in views if view]
        return bool(views) and views[-1].group(2) == views[-1].group(3) == "S1, S2, S3"

    def run_cycles(self, cycles):
        for cycle in range(1, cycles + 1):
            if self.down and (len(self.down) == 2 or self.rng.random() < 0.75):
                time.sleep(self.rng.uniform(0.2, 1.5))
                self.relaunch(self.rng.choice(sorted(self.down)))
            else:
                candidates = self.killable()
                if candidates:
                    self.kill(self.rng.choice(candidates))
                    if self.rng.random() < 0.25 and self.killable():
                        self.kill(self.rng.choice(self.killable()))
            # Sometimes move on before detection or recovery completes.
            time.sleep(self.rng.choice([0.0, 0.1, 0.3, 0.6, 1.0, 1.6]))
            self.wait_progress(f"client progress in cycle {cycle}", amount=3)
            self.check_processes()
            self.check_clients()

    def settle_and_verify(self):
        for replica in sorted(self.down):
            self.relaunch(replica)
        self.wait_for(self.all_ready, "all three replicas ready after the last cycle", timeout=30)
        self.wait_progress("traffic after final recovery", amount=10)
        self.check_clients()
        for client in CLIENTS:
            self.processes[client].kill()
            self.processes[client].wait(timeout=5)
            self.down.add(client)
        time.sleep(1.5)  # In-flight requests finish; passive backups receive final checkpoints.
        self.check_processes()
        self.check_views()
        finals = {}
        for replica in REPLICAS:
            text = self.log(self.instance[replica])
            applied = [json.loads(state) for state in AFTER.findall(text)]
            received = [json.loads(state) for state, _, _ in CHECKPOINT_IN.findall(text)]
            finals[replica] = (applied[-1] if applied else None, received[-1] if received else None)
        if self.mode == "passive":
            primary = [replica for replica in REPLICAS if "role = primary" in self.log(self.instance[replica])]
            assert len(primary) == 1, f"[passive] expected one final primary, found {primary}"
            expected = finals[primary[0]][0]
            for replica in REPLICAS:
                if replica != primary[0]:
                    assert finals[replica][1] == expected, (
                        f"[passive] backup {replica} holds {finals[replica][1]}, primary {primary[0]} holds {expected}")
            self.note(f"final primary {primary[0]}, state {expected}")
        else:
            states = {replica: finals[replica][0] for replica in REPLICAS}
            for client in CLIENTS:
                values = [state[client] for state in states.values()]
                # A client killed mid-broadcast may have reached only some replicas.
                assert max(values) - min(values) <= 1, f"[active] final states disagree for {client}: {states}"
            self.note(f"final states {states}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["active", "passive", "both"], default="both")
    parser.add_argument("--cycles", type=int, default=30)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--interval", default="0.05", help="seconds between each client's requests")
    parser.add_argument("--output", type=Path, default=ROOT / "logs" / "m4-soak")
    args = parser.parse_args()
    seed = args.seed if args.seed is not None else random.randrange(10**6)
    for mode in (["active", "passive"] if args.mode == "both" else [args.mode]):
        run = Soak(mode, args.output.resolve() / mode, random.Random(seed), args.interval)
        print(f"[{mode}] seed {seed}, {args.cycles} cycles", flush=True)
        try:
            run.bootstrap()
            run.run_cycles(args.cycles)
            run.settle_and_verify()
        except AssertionError:
            (run.output / "events.txt").write_text("\n".join(run.events))
            print(f"[{mode}] FAILED with seed {seed}; logs and events.txt in {run.output}", flush=True)
            raise
        finally:
            run.close()
        print(f"PASS: M4 {mode} soak, seed {seed}, {args.cycles} cycles", flush=True)


if __name__ == "__main__":
    main()
