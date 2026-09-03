"""Command-line entry point for a server replica."""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Sequence

from ft_system.common.config import ConfigError, load_config
from ft_system.common.logging import EventLogger
from ft_system.server.app import ServerApp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one server replica")
    parser.add_argument("--config", required=True, help="Path to shared TOML config")
    parser.add_argument("--server-id", required=True, help="Replica ID, for example S1")
    return parser


async def run(args: argparse.Namespace) -> None:
    app_config = load_config(args.config)
    server_config = app_config.server(args.server_id)
    app = ServerApp(server_config, tuple(app_config.clients), EventLogger())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await app.start()
    try:
        await stop_event.wait()
    finally:
        await app.close()


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
