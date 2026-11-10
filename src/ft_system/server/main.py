"""Command-line entry point for a server replica."""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Sequence
from dataclasses import replace

from ft_system.common.config import ConfigError, load_config
from ft_system.common.diagnostics import announce, make_logger, on_enter, print_status
from ft_system.lfd.main import positive_float
from ft_system.server.app import ServerApp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one server replica")
    parser.add_argument("--config", required=True, help="Path to shared TOML config")
    parser.add_argument("--server-id", required=True, help="Replica ID, for example S1")
    parser.add_argument("--checkpoint-freq", type=positive_float,
                        help="Override the primary's checkpoint period in seconds")
    return parser


async def run(args: argparse.Namespace) -> None:
    app_config = load_config(args.config)
    server_config = app_config.server(args.server_id)
    replication = app_config.replication
    if args.checkpoint_freq is not None:
        if replication is None:
            raise ConfigError("--checkpoint-freq requires a replication configuration")
        replication = replace(replication, checkpoint_freq=args.checkpoint_freq)
    logger = make_logger(args.server_id)
    identity = announce(logger, args.server_id, app_config, args.config)
    app = ServerApp(server_config, tuple(app_config.clients), logger,
                    replication, tuple(app_config.servers.values()), app_config.rm, identity)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await app.start()
    on_enter(lambda: print_status(logger, args.server_id, app.status_lines()))
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
