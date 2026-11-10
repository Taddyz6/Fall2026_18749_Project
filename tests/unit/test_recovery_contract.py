from pathlib import Path

import pytest

from ft_system.common.config import ConfigError, load_config
from ft_system.common.protocol import (
    ProtocolError, build_hello, build_launch_result, build_rm_command,
    decode_message, encode_message,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["active", "passive"])
@pytest.mark.parametrize("layout", ["local", "distributed"])
def test_m5_configs_enable_recovery_with_one_lfd_per_replica(mode, layout):
    config = load_config(ROOT / "configs" / f"m5.{mode}.{layout}.toml")
    assert config.recovery.enabled
    assert config.replication.mode == mode
    assert sorted(lfd.server_id for lfd in config.lfds.values()) == ["S1", "S2", "S3"]
    assert not load_config(ROOT / "configs" / f"m4.{mode}.{layout}.toml").recovery.enabled


@pytest.mark.parametrize("old,new,match", [
    ("enabled = true", 'enabled = "yes"', "boolean"),
    ("restart_delay = 1.0", "restart_delay = -1", "restart_delay"),
    ("restart_delay = 1.0", "restart_delay = nan", "restart_delay"),
    ("restart_retry = 1.0", "restart_retry = 0", "restart_retry"),
    ("restart_retry = 1.0", "restart_retry = true", "restart_retry"),
    ("restart_retry = 1.0", "restart_retry = inf", "restart_retry"),
    ('server_id = "S3"', 'server_id = "S2"', "exactly one LFD"),
    ("[gfd]", "[unused_gfd]", r"requires \[rm\]"),
])
def test_recovery_configuration_rejects_invalid_values(tmp_path, old, new, match):
    text = (ROOT / "configs/m5.active.local.toml").read_text().replace(old, new)
    path = tmp_path / "invalid.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=match):
        load_config(path)


@pytest.mark.parametrize("status,pid", [("started", 123), ("running", 456),
                                        ("external", None), ("failed", None)])
def test_launch_results_roundtrip(status, pid):
    result = build_launch_result("LFD2", "S2", status, pid, "example diagnostic")
    assert decode_message(encode_message(result)) == result
    hello = build_hello("LFD2", "lfd")
    assert decode_message(encode_message(hello)) == hello
    command = build_rm_command("LFD2", "ensure_replica", "S2")
    assert decode_message(encode_message(command)) == command


@pytest.mark.parametrize("field,value", [
    ("status", []), ("status", "ready"), ("status", None),
    ("replica_id", ""), ("detail", ""), ("detail", 3),
    ("pid", None), ("pid", True), ("pid", 0), ("pid", -1),
])
def test_launch_results_reject_malformed_payloads(field, value):
    result = build_launch_result("LFD2", "S2", "started", 123, "started")
    result["payload"][field] = value
    with pytest.raises(ProtocolError):
        encode_message(result)


@pytest.mark.parametrize("status", ["external", "failed"])
def test_launch_results_require_explicit_null_pid(status):
    result = build_launch_result("LFD1", "S1", status, None, "example")
    del result["payload"]["pid"]
    with pytest.raises(ProtocolError, match="requires pid"):
        encode_message(result)


def test_launch_results_and_commands_validate_direction_and_target():
    with pytest.raises(ProtocolError):
        build_rm_command("LFD1", "ensure_replica")
    with pytest.raises(ProtocolError):
        build_launch_result("LFD1", "S1", "failed", 123, "failed")
    with pytest.raises(ProtocolError):
        encode_message(build_launch_result("LFD1", "S1", "external", None, "alive")
                       | {"destination": "GFD"})
    with pytest.raises(ProtocolError):
        encode_message(build_rm_command("LFD1", "ensure_replica", "S1") | {"source": "C1"})
