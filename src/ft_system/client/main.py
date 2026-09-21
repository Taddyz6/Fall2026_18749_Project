"""Command-line entry point for an independent client."""

from __future__ import annotations

import argparse
import asyncio
import math
from collections.abc import Sequence

from ft_system.client.app import (
    ClientApp,
    ServerEndpoint,
    run_automatic,
    run_interactive,
)
from ft_system.common.config import ConfigError, load_config
from ft_system.common.logging import EventLogger


def positive_interval(value: str) -> float:
    interval = float(value)
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError(
            "interval must be a finite positive number of seconds"
        )
    return interval


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one automatically sending client")
    parser.add_argument("--config", required=True, help="Path to shared TOML config")
    parser.add_argument("--client-id", required=True, help="Client ID, for example C1")
    parser.add_argument(
        "--interval",
        type=positive_interval,
        default=1.0,
        help="Seconds to wait after each automatic request attempt (default: 1.0)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Send only on Enter or increment; ignores --interval",
    )
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
        if args.interactive:
            await run_interactive(client)
        else:
            await run_automatic(client, args.interval)
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
