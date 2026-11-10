"""Preflight network and consistency check for the multi-computer demo.

Run the same command on every computer at the same time, before starting the
demo processes:

    PYTHONPATH=src python -m ft_system.doctor --config configs/m4.active.distributed.toml

Each copy works out which computer it is on from the config's addresses,
listens on the ports that computer's processes will use, and keeps connecting
to every port those processes will dial. Both ends exchange the code commit
and the config fingerprint. Leave it running until every computer prints PASS,
then press Ctrl-C and start the demo.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit(f"[FAIL] Python {sys.version.split()[0]} is too old; this project needs Python 3.11 or newer.")

import argparse
import asyncio
import json
import platform
import socket
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ft_system.common.config import AppConfig, ConfigError, load_config
from ft_system.common.diagnostics import build_id


# Windows retries a refused connection for about two seconds before reporting it,
# so the connect deadline must be longer than that to tell "refused" from "no answer".
CONNECT_TIMEOUT = 4.0
PROBE_TIMEOUT = 2.0
ROUND_INTERVAL = 2.0
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class Endpoint:
    owner: str
    purpose: str
    host: str
    bind_host: str
    port: int

    def __str__(self) -> str:
        return f"{self.owner} {self.purpose} {self.host}:{self.port}"


def listening_endpoints(config: AppConfig) -> dict[str, Endpoint]:
    endpoints = {}
    for name, section in (("RM", config.rm), ("GFD", config.gfd)):
        if section is not None:
            endpoints[name] = Endpoint(name, "port", section.advertised_host, section.bind_host, section.port)
    for replica, server in config.servers.items():
        ports = [("client", server.client_port), ("heartbeat", server.heartbeat_port),
                 ("checkpoint", server.checkpoint_port)]
        for purpose, port in ports:
            if port is not None:
                endpoints[f"{replica} {purpose}"] = Endpoint(
                    replica, f"{purpose} port", server.advertised_host, server.bind_host, port)
    return endpoints


def process_hosts(config: AppConfig) -> dict[str, str]:
    """Map each process to the address of the computer it runs on."""

    hosts = {}
    if config.rm is not None:
        hosts["RM"] = config.rm.advertised_host
    if config.gfd is not None:
        hosts["GFD"] = config.gfd.advertised_host
    sacred = hosts.get("GFD", hosts.get("RM"))
    if sacred is not None:
        for client in config.clients:  # Clients share the GFD's (sacred) computer.
            hosts[client] = sacred
    for replica, server in config.servers.items():
        hosts[replica] = server.advertised_host
        for lfd in config.lfds.values():
            if lfd.server_id == replica:
                hosts[lfd.lfd_id] = server.advertised_host
    return hosts


def dialed_endpoints(config: AppConfig, process: str) -> list[str]:
    """Keys of the listening endpoints that ``process`` opens connections to."""

    if process in config.clients:
        return [f"{replica} client" for replica in config.client(process).server_ids]
    if process == "GFD":
        return ["RM"] if config.rm is not None else []
    if process in config.lfds:
        lfd = config.lfd(process)
        return (["GFD"] if config.gfd is not None else []) + [f"{lfd.server_id} heartbeat"]
    if process in config.servers:
        peers = [f"{other} checkpoint" for other in config.servers
                 if other != process and config.replication is not None]
        return (["RM"] if config.rm is not None else []) + peers
    return []


def is_this_computer(host: str) -> bool:
    """A socket can bind to an address only if one of this computer's interfaces has it."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
        return True
    except OSError:
        return False


def outbound_address() -> str | None:
    """Best guess at this computer's network address; connecting a UDP socket sends nothing."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def port_owner_hint(port: int) -> str:
    if platform.system() == "Windows":
        return f"find it with: netstat -ano | findstr :{port}   then stop it with: taskkill /PID <pid> /F"
    return f"find it with: lsof -nP -iTCP:{port} -sTCP:LISTEN   then stop it with: kill <pid>"


class Doctor:
    def __init__(self, config_path: str | Path, out: Callable[[str], None] = print,
                 host: str | None = None) -> None:
        self.config_path = Path(config_path)
        self.config = load_config(config_path)
        self.out = out
        self.forced_host = host
        self.endpoints = listening_endpoints(self.config)
        self.hosts = process_hosts(self.config)
        self.identity = {"build": build_id(), "config": self.config.fingerprint}
        self.here: list[str] = []
        self.local_problems = 0
        self._servers: list[asyncio.Server] = []
        self._inbound: set[tuple] = set()

    def machine(self, host: str) -> str:
        processes = [process for process, address in self.hosts.items() if address == host]
        return f"the computer running {', '.join(processes)}" if processes else host

    def hello(self) -> bytes:
        return (json.dumps({"ft_doctor": 1, "from": ", ".join(self.here), **self.identity}) + "\n").encode()

    def compare(self, peer: dict) -> str | None:
        problems = []
        if peer.get("build") != self.identity["build"]:
            problems.append(f"it runs build {peer.get('build')}, this computer runs {self.identity['build']}")
        if peer.get("config") != self.identity["config"]:
            problems.append(f"its config is {peer.get('config')}, this computer's is {self.identity['config']}")
        return "; ".join(problems) or None

    def problem(self, text: str, *hints: str) -> None:
        self.local_problems += 1
        self.out(f"[FAIL] {text}")
        for hint in hints:
            self.out(f"       {hint}")

    def check_this_computer(self) -> list[Endpoint]:
        """Report local facts and problems; return the endpoints this computer must serve."""

        replication = self.config.replication
        mode = replication.mode if replication is not None else "no replication"
        self.out(f"[OK]   Python {platform.python_version()}")
        self.out(f"[INFO] build {self.identity['build']}, config {self.identity['config']} "
                 f"({self.config_path.name}, {mode}); every computer must show the same two values")
        addresses = list(dict.fromkeys(self.hosts.values()))
        if self.forced_host is not None:
            mine = [self.forced_host] if self.forced_host in addresses else []
        else:
            mine = [address for address in addresses if is_this_computer(address)]
        if not mine:
            guess = outbound_address()
            self.problem(f"none of the config's addresses ({', '.join(addresses)}) belongs to this computer",
                         f"this computer's address appears to be {guess}" if guess
                         else "this computer does not seem to be connected to a network",
                         "fix advertised_host in the config, then copy the same file to every computer")
            return []
        self.here = [process for process, address in self.hosts.items() if address in mine]
        self.out(f"[INFO] this computer is {', '.join(mine)} and runs {', '.join(self.here)}")
        serving = [endpoint for endpoint in self.endpoints.values() if self.hosts.get(endpoint.owner) in mine]
        if len(addresses) > 1:
            for endpoint in self.endpoints.values():
                if endpoint.host in LOOPBACK | {"0.0.0.0"}:
                    self.problem(f"{endpoint} uses {endpoint.host} as advertised_host in a multi-computer config",
                                 "set advertised_host to that computer's network IPv4 address")
            reported = set()
            for endpoint in serving:
                if endpoint.bind_host in LOOPBACK and endpoint.owner not in reported:
                    reported.add(endpoint.owner)
                    self.problem(f"{endpoint.owner} listens on {endpoint.bind_host}, which other computers cannot reach",
                                 'use bind_host = "0.0.0.0"')
        return serving

    async def listen(self, endpoints: list[Endpoint]) -> None:
        for endpoint in endpoints:
            try:
                server = await asyncio.start_server(
                    lambda reader, writer, endpoint=endpoint: self.answer(reader, writer, endpoint),
                    endpoint.bind_host, endpoint.port)
            except OSError as exc:
                self.problem(f"cannot listen on {endpoint.bind_host}:{endpoint.port} for {endpoint.owner} "
                             f"{endpoint.purpose}: {exc.strerror or exc}",
                             "another program, usually a demo process from an earlier run, holds this port",
                             port_owner_hint(endpoint.port))
            else:
                self._servers.append(server)
        if self._servers:
            self.out(f"[OK]   listening on {len(self._servers)} port(s) that "
                     f"{', '.join(dict.fromkeys(e.owner for e in endpoints))} will use")
            if platform.system() == "Darwin":
                self.out("       macOS: if asked whether Python may accept incoming network connections, click Allow")

    async def answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, endpoint: Endpoint) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), PROBE_TIMEOUT)
            writer.write(self.hello())
            await writer.drain()
            peer = json.loads(line)
            if not isinstance(peer, dict) or peer.get("ft_doctor") != 1 or peer.get("from") == ", ".join(self.here):
                return
            address = (writer.get_extra_info("peername") or ("?",))[0]
            key = (peer.get("from"), address, endpoint.owner, endpoint.purpose)
            if key in self._inbound:
                return
            self._inbound.add(key)
            mismatch = self.compare(peer)
            self.out(f"[IN]   {peer.get('from')} at {address} reached {endpoint.owner} {endpoint.purpose} "
                     f"{endpoint.port}" + (f" -- MISMATCH: {mismatch}" if mismatch else ""))
        except (TimeoutError, OSError, ValueError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def probe(self, endpoint: Endpoint) -> tuple[bool, str, list[str]]:
        owner = self.machine(endpoint.host)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(endpoint.host, endpoint.port), CONNECT_TIMEOUT)
        except ConnectionRefusedError:
            return False, "connection refused", [
                f"nothing is listening there: start the doctor on {owner}",
                f"if it is already running there, that computer's firewall is rejecting port {endpoint.port}"]
        except TimeoutError:
            return False, f"no answer within {CONNECT_TIMEOUT:.0f} s", [
                f"is {endpoint.host} still the address of {owner}? (macOS: ipconfig getifaddr en0; Windows: ipconfig)",
                "are both computers on the same network? Some Wi-Fi networks block traffic between devices; "
                "a phone hotspot avoids that",
                f"that computer's firewall may silently drop port {endpoint.port} "
                "(macOS: allow incoming connections for Python)"]
        except OSError as exc:
            return False, exc.strerror or str(exc), [
                f"this computer cannot reach {endpoint.host}; is it on the same network as {owner}?"]
        try:
            writer.write(self.hello())
            await writer.drain()
            peer = json.loads(await asyncio.wait_for(reader.readline(), PROBE_TIMEOUT))
            if not isinstance(peer, dict) or peer.get("ft_doctor") != 1:
                raise ValueError("not a doctor")
        except (TimeoutError, OSError, ValueError):
            return False, "something other than the doctor answered", [
                f"a demo process (or another program) still uses port {endpoint.port} on {owner}; "
                "stop it, then start the doctor there"]
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        mismatch = self.compare(peer)
        if mismatch:
            return False, f"reached {peer.get('from')}, but {mismatch}", [
                "check out the same commit everywhere (git pull) and copy one config file to every computer"]
        return True, f"reached {peer.get('from')}", []

    async def run(self, until_pass: bool = False) -> bool:
        serving = self.check_this_computer()
        if not self.here:
            return False
        await self.listen(serving)
        dialers: dict[str, list[str]] = {}
        for process in self.here:
            for key in dialed_endpoints(self.config, process):
                dialers.setdefault(key, []).append(process)
        self.out(f"[INFO] checking {len(dialers)} connection(s) that {', '.join(self.here)} will open; "
                 "keep the doctor running on every computer")
        results: dict[str, tuple[bool, str]] = {}
        passed = False
        rounds = 0
        try:
            while True:
                keys = list(dialers)
                outcomes = await asyncio.gather(*(self.probe(self.endpoints[key]) for key in keys))
                for key, (ok, detail, hints) in zip(keys, outcomes):
                    if results.get(key) == (ok, detail):
                        continue
                    results[key] = (ok, detail)
                    tag = "[OK]  " if ok else "[FAIL]"
                    self.out(f"{tag} {', '.join(dialers[key])} -> {self.endpoints[key]}: {detail}")
                    for hint in hints:
                        self.out(f"       {hint}")
                all_ok = self.local_problems == 0 and all(ok for ok, _ in results.values())
                if all_ok and not passed:
                    self.out("PASS: this computer can reach everything its processes need, with the same code and "
                             "config. Leave this running until every computer shows PASS, then press Ctrl-C.")
                    if until_pass:
                        return True
                elif not all_ok and rounds % 5 == 4:
                    failing = [str(self.endpoints[key]) for key, (ok, _) in results.items() if not ok]
                    waiting = f"still failing: {'; '.join(failing)}" if failing else "fix the [FAIL] items above"
                    self.out(f"[WAIT] {waiting}")
                passed = all_ok
                rounds += 1
                await asyncio.sleep(ROUND_INTERVAL)
        finally:
            for server in self._servers:
                server.close()
            await asyncio.gather(*(server.wait_closed() for server in self._servers), return_exceptions=True)
            self._servers.clear()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check this computer before a multi-computer demo; "
                                                 "run it on every computer at the same time")
    parser.add_argument("--config", required=True, help="The shared distributed config")
    parser.add_argument("--host", help="act as the computer with this advertised address (default: detect)")
    args = parser.parse_args(argv)
    try:
        doctor = Doctor(args.config, out=lambda text: print(text, flush=True), host=args.host)
    except ConfigError as exc:
        print(f"[FAIL] {exc}", flush=True)
        return 1
    try:
        asyncio.run(doctor.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
