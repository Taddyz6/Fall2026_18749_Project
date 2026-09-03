"""Validated TOML configuration for local and distributed deployments."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


class ConfigError(ValueError):
    """Raised when deployment configuration is missing or invalid."""


@dataclass(frozen=True)
class ServerConfig:
    replica_id: str
    bind_host: str
    advertised_host: str
    client_port: int
    heartbeat_port: int


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    server_ids: tuple[str, ...]
    connect_timeout: float
    read_timeout: float


@dataclass(frozen=True)
class LfdConfig:
    lfd_id: str
    server_id: str
    heartbeat_freq: float
    connect_timeout: float
    read_timeout: float
    initial_backoff: float = 0.5
    max_backoff: float = 5.0


@dataclass(frozen=True)
class AppConfig:
    servers: Mapping[str, ServerConfig]
    clients: Mapping[str, ClientConfig]
    lfds: Mapping[str, LfdConfig]

    def server(self, replica_id: str) -> ServerConfig:
        try:
            return self.servers[replica_id]
        except KeyError as exc:
            raise ConfigError(f"unknown server id: {replica_id}") from exc

    def client(self, client_id: str) -> ClientConfig:
        try:
            return self.clients[client_id]
        except KeyError as exc:
            raise ConfigError(f"unknown client id: {client_id}") from exc

    def lfd(self, lfd_id: str) -> LfdConfig:
        try:
            return self.lfds[lfd_id]
        except KeyError as exc:
            raise ConfigError(f"unknown LFD id: {lfd_id}") from exc


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping) or not value:
        raise ConfigError(f"{name} section must be a non-empty table")
    return value


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _port(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(f"{field} must be an integer from 1 to 65535")
    return value


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{field} must be positive")
    return float(value)


def _required(table: Mapping[str, Any], field: str, component: str) -> Any:
    try:
        return table[field]
    except KeyError as exc:
        raise ConfigError(f"{component} is missing {field}") from exc


def _parse_servers(data: Mapping[str, Any]) -> dict[str, ServerConfig]:
    servers: dict[str, ServerConfig] = {}
    for replica_id, raw in _section(data, "server").items():
        replica_id = _non_empty_string(replica_id, "replica_id")
        if not isinstance(raw, Mapping):
            raise ConfigError(f"server {replica_id} must be a table")
        client_port = _port(
            _required(raw, "client_port", f"server {replica_id}"), "client_port"
        )
        heartbeat_port = _port(
            _required(raw, "heartbeat_port", f"server {replica_id}"),
            "heartbeat_port",
        )
        if client_port == heartbeat_port:
            raise ConfigError("client_port and heartbeat_port must be distinct")
        servers[replica_id] = ServerConfig(
            replica_id=replica_id,
            bind_host=_non_empty_string(
                _required(raw, "bind_host", f"server {replica_id}"), "bind_host"
            ),
            advertised_host=_non_empty_string(
                _required(raw, "advertised_host", f"server {replica_id}"),
                "advertised_host",
            ),
            client_port=client_port,
            heartbeat_port=heartbeat_port,
        )
    return servers


def _server_ids(value: Any, client_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"client {client_id} server_ids must be a non-empty list")
    ids = tuple(
        _non_empty_string(server_id, f"client {client_id} server_ids")
        for server_id in value
    )
    if len(ids) != len(set(ids)):
        raise ConfigError(f"client {client_id} server_ids must be unique")
    return ids


def _parse_clients(data: Mapping[str, Any]) -> dict[str, ClientConfig]:
    clients: dict[str, ClientConfig] = {}
    for client_id, raw in _section(data, "client").items():
        client_id = _non_empty_string(client_id, "client_id")
        if not isinstance(raw, Mapping):
            raise ConfigError(f"client {client_id} must be a table")
        clients[client_id] = ClientConfig(
            client_id=client_id,
            server_ids=_server_ids(
                _required(raw, "server_ids", f"client {client_id}"), client_id
            ),
            connect_timeout=_positive_number(
                _required(raw, "connect_timeout", f"client {client_id}"),
                "connect_timeout",
            ),
            read_timeout=_positive_number(
                _required(raw, "read_timeout", f"client {client_id}"),
                "read_timeout",
            ),
        )
    return clients


def _parse_lfds(data: Mapping[str, Any]) -> dict[str, LfdConfig]:
    lfds: dict[str, LfdConfig] = {}
    for lfd_id, raw in _section(data, "lfd").items():
        lfd_id = _non_empty_string(lfd_id, "lfd_id")
        if not isinstance(raw, Mapping):
            raise ConfigError(f"LFD {lfd_id} must be a table")
        initial_backoff = _positive_number(
            raw.get("initial_backoff", 0.5), "initial_backoff"
        )
        max_backoff = _positive_number(raw.get("max_backoff", 5.0), "max_backoff")
        if initial_backoff > max_backoff:
            raise ConfigError("initial_backoff must not exceed max_backoff")
        lfds[lfd_id] = LfdConfig(
            lfd_id=lfd_id,
            server_id=_non_empty_string(
                _required(raw, "server_id", f"LFD {lfd_id}"), "server_id"
            ),
            heartbeat_freq=_positive_number(
                _required(raw, "heartbeat_freq", f"LFD {lfd_id}"),
                "heartbeat_freq",
            ),
            connect_timeout=_positive_number(
                _required(raw, "connect_timeout", f"LFD {lfd_id}"),
                "connect_timeout",
            ),
            read_timeout=_positive_number(
                _required(raw, "read_timeout", f"LFD {lfd_id}"), "read_timeout"
            ),
            initial_backoff=initial_backoff,
            max_backoff=max_backoff,
        )
    return lfds


def load_config(path: str | Path) -> AppConfig:
    """Load and fully validate one shared deployment configuration."""

    config_path = Path(path)
    try:
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc

    servers = _parse_servers(data)
    clients = _parse_clients(data)
    lfds = _parse_lfds(data)

    for client in clients.values():
        for server_id in client.server_ids:
            if server_id not in servers:
                raise ConfigError(
                    f"client {client.client_id} references unknown server {server_id}"
                )
    for lfd in lfds.values():
        if lfd.server_id not in servers:
            raise ConfigError(f"LFD {lfd.lfd_id} references unknown server {lfd.server_id}")

    return AppConfig(
        servers=MappingProxyType(servers),
        clients=MappingProxyType(clients),
        lfds=MappingProxyType(lfds),
    )
