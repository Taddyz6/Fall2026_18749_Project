"""Run the Replication Manager on the clients' machine."""

import argparse
import asyncio
import signal
from dataclasses import replace

from ft_system.common.config import ConfigError, load_config
from ft_system.common.diagnostics import announce, make_logger, on_enter, print_status
from ft_system.lfd.main import positive_float
from ft_system.rm.app import RmApp


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed != 0:
        return positive_float(value)
    return parsed


def build_parser():
    parser = argparse.ArgumentParser(description="Run the Replication Manager")
    parser.add_argument("--config", required=True)
    parser.add_argument("--transfer-delay", type=non_negative_float,
                        help="Seconds to wait before a new replica receives state (demo visibility)")
    return parser


async def run(args):
    config = load_config(args.config)
    if config.rm is None or config.replication is None:
        raise ConfigError("configuration requires [rm] and [replication] sections")
    rm = config.rm
    if args.transfer_delay is not None:
        rm = replace(rm, transfer_delay=args.transfer_delay)
    logger = make_logger("RM")
    identity = announce(logger, "RM", config, args.config)
    app = RmApp(rm, config.replication.mode, config.servers, config.clients, logger, identity)
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            pass
    await app.start()
    on_enter(lambda: print_status(logger, "RM", app.status_lines()))
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
