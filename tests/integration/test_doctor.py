import asyncio
import json
import socket
from pathlib import Path

from ft_system.doctor import Doctor, Endpoint
from tests.helpers import free_ports


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PORTS = [7300, 7200, 5201, 6201, 8201, 5202, 6202, 8202, 5203, 6203, 8203]


def local_config(tmp_path):
    text = (ROOT / "configs" / "m4.active.local.toml").read_text()
    ports = free_ports(len(CONFIG_PORTS))
    for old, new in zip(CONFIG_PORTS, ports):
        text = text.replace(f"= {old}\n", f"= {new}\n")
    path = tmp_path / "deployment.toml"
    path.write_text(text)
    return path, dict(zip(CONFIG_PORTS, ports))


async def test_single_computer_config_passes_every_connection(tmp_path):
    path, _ = local_config(tmp_path)
    lines = []

    assert await asyncio.wait_for(Doctor(path, lines.append).run(until_pass=True), 10)

    output = "\n".join(lines)
    assert "runs RM, GFD, C1, C2, C3, S1, LFD1, S2, LFD2, S3, LFD3" in output
    assert "[OK]   C1, C2, C3 -> S2 client port" in output
    assert "[OK]   S1, S3 -> S2 checkpoint port" in output
    assert "[OK]   GFD, S1, S2, S3 -> RM port" in output
    assert "PASS" in output


async def test_port_held_by_another_program_is_reported_with_a_fix(tmp_path):
    path, ports = local_config(tmp_path)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", ports[7300]))
    blocker.listen()
    lines = []
    doctor = Doctor(path, lines.append)
    try:
        await doctor.listen(doctor.check_this_computer())
    finally:
        blocker.close()
        for server in doctor._servers:
            server.close()

    assert doctor.local_problems == 1
    assert any(f"cannot listen on 127.0.0.1:{ports[7300]} for RM" in line for line in lines)
    assert any(str(ports[7300]) in line and ("lsof" in line or "netstat" in line) for line in lines)


async def test_probe_explains_refusals_strangers_and_mismatches(tmp_path):
    path, _ = local_config(tmp_path)
    doctor = Doctor(path, lambda text: None)
    doctor.here = ["S1", "LFD1"]

    async def stranger(reader, writer):
        writer.close()

    async def other_config(reader, writer):
        await reader.readline()
        writer.write((json.dumps({"ft_doctor": 1, "from": "RM, GFD", "build": doctor.identity["build"],
                                  "config": "00000000"}) + "\n").encode())
        await writer.drain()
        writer.close()

    closed_port = free_ports(1)[0]
    servers = [await asyncio.start_server(handler, "127.0.0.1", 0) for handler in (stranger, other_config)]
    try:
        refused = await doctor.probe(Endpoint("S2", "client port", "127.0.0.1", "127.0.0.1", closed_port))
        assert refused[:2] == (False, "connection refused")
        port = servers[0].sockets[0].getsockname()[1]
        strange = await doctor.probe(Endpoint("S2", "client port", "127.0.0.1", "127.0.0.1", port))
        assert strange[:2] == (False, "something other than the doctor answered")
        port = servers[1].sockets[0].getsockname()[1]
        ok, detail, hints = await doctor.probe(Endpoint("RM", "port", "127.0.0.1", "127.0.0.1", port))
        assert not ok and "its config is 00000000" in detail and "copy one config file" in hints[0]
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


async def test_reports_when_no_config_address_belongs_to_this_computer(tmp_path):
    path, _ = local_config(tmp_path)
    text = path.read_text().replace('advertised_host = "127.0.0.1"', 'advertised_host = "192.0.2.77"')
    path.write_text(text)
    lines = []

    assert not await Doctor(path, lines.append).run(until_pass=True)

    assert any("none of the config's addresses (192.0.2.77) belongs to this computer" in line for line in lines)
