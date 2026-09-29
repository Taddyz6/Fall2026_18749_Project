"""Command-line entry point for an independent client."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from ft_system.client.app import ClientApp, ServerEndpoint, run_automatic, run_interactive
from ft_system.common.config import ConfigError, load_config
from ft_system.common.logging import EventLogger
from ft_system.client.active import ActiveClientApp
from ft_system.common.protocol import ProtocolError
from ft_system.lfd.main import positive_float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one independent client")
    parser.add_argument("--config", required=True, help="Path to shared TOML config")
    parser.add_argument("--client-id", required=True, help="Client ID, for example C1")
    parser.add_argument("--interactive", action="store_true", help="Send requests manually")
    parser.add_argument("--interval", type=positive_float, default=1.0, help="Seconds between automatic requests")
    parser.add_argument("--count", type=int, default=0, help="Stop after this many replies; 0 runs forever")
    return parser


async def run(args: argparse.Namespace) -> None:
    app_config = load_config(args.config)
    client_config = app_config.client(args.client_id)
    endpoints = []
    for replica_id in client_config.server_ids:
        server = app_config.server(replica_id)
        endpoints.append(ServerEndpoint(
            server.replica_id, server.advertised_host, server.client_port
        ))

    if len(endpoints) > 1:
        client = ActiveClientApp(
            client_config.client_id,
            endpoints,
            client_config.connect_timeout,
            client_config.read_timeout,
            EventLogger(),
        )
    else:
        client = ClientApp(
            client_config.client_id,
            endpoints[0],
            client_config.connect_timeout,
            client_config.read_timeout,
            EventLogger(),
        )
    if args.count < 0:
        raise ConfigError("count must be non-negative")
    try:
        if args.interactive:
            await run_interactive(client)
        elif args.count == 0:
            await run_automatic(client, args.interval)
        else:
            completed = 0
            while completed < args.count:
                try:
                    await client.send_increment()
                    completed += 1
                except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError) as exc:
                    client.logger.error(client.client_id, str(exc) or type(exc).__name__)
                if completed < args.count:
                    await asyncio.sleep(args.interval)
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
