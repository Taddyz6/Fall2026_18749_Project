"""Command-line entry point for an independent client."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from ft_system.client.app import ClientApp, ServerEndpoint, run_interactive
from ft_system.common.config import ConfigError, load_config
from ft_system.common.logging import EventLogger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one interactive client")
    parser.add_argument("--config", required=True, help="Path to shared TOML config")
    parser.add_argument("--client-id", required=True, help="Client ID, for example C1")
    return parser


async def run(args: argparse.Namespace) -> None:
    app_config = load_config(args.config)
    client_config = app_config.client(args.client_id)
    replica_id = client_config.server_ids[0]
    server = app_config.server(replica_id)
    endpoint = ServerEndpoint(replica_id, server.advertised_host, server.client_port)
    client = ClientApp(
        client_config.client_id,
        endpoint,
        client_config.connect_timeout,
        client_config.read_timeout,
        EventLogger(),
    )
    try:
        await run_interactive(client)
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except ConfigError as exc:
        build_parser().error(str(exc))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
