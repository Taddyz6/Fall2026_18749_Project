"""Command-line entry point for a local fault detector."""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Sequence
from dataclasses import replace

from ft_system.common.config import ConfigError, load_config
from ft_system.common.logging import EventLogger
from ft_system.lfd.app import HeartbeatEndpoint, LfdApp


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one local fault detector")
    parser.add_argument("--config", required=True, help="Path to shared TOML config")
    parser.add_argument("--lfd-id", required=True, help="LFD ID, for example LFD1")
    parser.add_argument(
        "--heartbeat-freq",
        type=positive_float,
        help="Override heartbeat period in seconds",
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    app_config = load_config(args.config)
    lfd_config = app_config.lfd(args.lfd_id)
    if args.heartbeat_freq is not None:
        lfd_config = replace(lfd_config, heartbeat_freq=args.heartbeat_freq)
    server = app_config.server(lfd_config.server_id)
    endpoint = HeartbeatEndpoint(
        server.replica_id,
        server.advertised_host,
        server.heartbeat_port,
    )
    app = LfdApp(lfd_config, endpoint, EventLogger())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    task = asyncio.create_task(app.run())
    try:
        await stop_event.wait()
    finally:
        await app.stop()
        await task


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
