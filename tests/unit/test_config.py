from pathlib import Path

import pytest

from ft_system.common.config import ConfigError, load_config


DEFAULTS = {
    "bind_host": "127.0.0.1",
    "advertised_host": "127.0.0.1",
    "client_port": 5001,
    "heartbeat_port": 6001,
    "heartbeat_freq": 2.0,
    "connect_timeout": 2.0,
    "read_timeout": 2.0,
    "initial_backoff": 0.5,
    "max_backoff": 5.0,
}


def write_config(tmp_path: Path, **overrides) -> Path:
    values = DEFAULTS | overrides
    content = f"""
[server.S1]
bind_host = "{values['bind_host']}"
advertised_host = "{values['advertised_host']}"
client_port = {values['client_port']}
heartbeat_port = {values['heartbeat_port']}

[client.C1]
server_ids = ["S1"]
connect_timeout = {values['connect_timeout']}
read_timeout = {values['read_timeout']}

[lfd.LFD1]
server_id = "S1"
heartbeat_freq = {values['heartbeat_freq']}
connect_timeout = {values['connect_timeout']}
read_timeout = {values['read_timeout']}
initial_backoff = {values['initial_backoff']}
max_backoff = {values['max_backoff']}
"""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_distinct_client_and_heartbeat_ports(tmp_path):
    path = write_config(tmp_path, client_port=5001, heartbeat_port=6001)

    config = load_config(path)

    assert config.server("S1").client_port == 5001
    assert config.server("S1").heartbeat_port == 6001
    assert config.client("C1").server_ids == ("S1",)
    assert config.lfd("LFD1").server_id == "S1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_port", 0),
        ("heartbeat_port", 65536),
        ("heartbeat_freq", 0),
        ("connect_timeout", 0),
        ("read_timeout", -1),
        ("initial_backoff", 0),
        ("max_backoff", -1),
    ],
)
def test_rejects_invalid_numeric_values(tmp_path, field, value):
    path = write_config(tmp_path, **{field: value})

    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_same_port_for_client_and_heartbeat_listeners(tmp_path):
    path = write_config(tmp_path, client_port=5001, heartbeat_port=5001)

    with pytest.raises(ConfigError, match="distinct"):
        load_config(path)


def test_rejects_backoff_initial_value_above_maximum(tmp_path):
    path = write_config(tmp_path, initial_backoff=2.0, max_backoff=1.0)

    with pytest.raises(ConfigError, match="backoff"):
        load_config(path)


def test_rejects_client_that_references_missing_server(tmp_path):
    path = write_config(tmp_path)
    content = path.read_text(encoding="utf-8").replace(
        'server_ids = ["S1"]', 'server_ids = ["S9"]'
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match="S9"):
        load_config(path)


def test_rejects_lfd_that_references_missing_server(tmp_path):
    path = write_config(tmp_path)
    content = path.read_text(encoding="utf-8").replace(
        'server_id = "S1"', 'server_id = "S9"'
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match="S9"):
        load_config(path)


def test_rejects_client_without_any_server(tmp_path):
    path = write_config(tmp_path)
    content = path.read_text(encoding="utf-8").replace(
        'server_ids = ["S1"]', "server_ids = []"
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match="server_ids"):
        load_config(path)


def test_supports_wildcard_bind_with_distinct_advertised_host(tmp_path):
    path = write_config(
        tmp_path,
        bind_host="0.0.0.0",
        advertised_host="192.168.1.10",
    )

    server = load_config(path).server("S1")

    assert server.bind_host == "0.0.0.0"
    assert server.advertised_host == "192.168.1.10"


def test_unknown_component_lookup_has_clear_error(tmp_path):
    config = load_config(write_config(tmp_path))

    with pytest.raises(ConfigError, match="unknown server id: S9"):
        config.server("S9")
    with pytest.raises(ConfigError, match="unknown client id: C9"):
        config.client("C9")
    with pytest.raises(ConfigError, match="unknown LFD id: LFD9"):
        config.lfd("LFD9")


def test_rejects_malformed_toml(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[server.S1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="TOML"):
        load_config(path)
