"""Run the GFD on the clients' machine."""

import argparse
import asyncio
import signal
from dataclasses import replace

from ft_system.common.config import ConfigError, load_config
from ft_system.common.logging import EventLogger
from ft_system.gfd.app import GfdApp
from ft_system.lfd.main import positive_float


def build_parser():
    parser = argparse.ArgumentParser(description="Run the global fault detector")
    parser.add_argument("--config", required=True)
    parser.add_argument("--heartbeat-freq", type=positive_float)
    return parser


async def run(args):
    config = load_config(args.config)
    if config.gfd is None:
        raise ConfigError("configuration requires a gfd section")
    gfd = config.gfd
    if args.heartbeat_freq is not None:
        gfd = replace(gfd, heartbeat_freq=args.heartbeat_freq)
    app = GfdApp(gfd, {lfd.lfd_id: lfd.server_id for lfd in config.lfds.values()}, EventLogger())
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            pass
    await app.start()
    try:
        await stopped.wait()
    finally:
        await app.close()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except ConfigError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
