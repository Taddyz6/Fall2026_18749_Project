"""Persistent registration, membership updates, and GFD heartbeat replies."""

import asyncio

from ft_system.common.protocol import (
    MessageType, ProtocolError, build_membership, build_heartbeat_ack,
)
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.transport import close_writer, read_message_with_timeout, write_message


class GfdReporter:
    def __init__(self, lfd_id, replica_id, config, logger):
        self.lfd_id = lfd_id
        self.replica_id = replica_id
        self.config = config
        self.logger = logger
        self.healthy = False
        self._changed = asyncio.Event()

    def set_health(self, healthy: bool) -> None:
        if self.healthy != healthy:
            self.healthy = healthy
            self._changed.set()
            action = "add" if healthy else "delete"
            self.logger.event(f"{self.lfd_id}: {action} replica {self.replica_id}")

    async def run(self) -> None:
        while True:
            writer = None
            sender = None
            try:
                reader, writer = await open_connection_with_timeout(
                    self.config.advertised_host, self.config.port, self.config.read_timeout)
                lock = asyncio.Lock()

                async def send(message):
                    async with lock:
                        await asyncio.wait_for(write_message(writer, message), self.config.read_timeout)

                self._changed.clear()
                await send(build_membership(self.lfd_id, self.replica_id, self.healthy, register=True))
                self.logger.event(f"{self.lfd_id}: registered with GFD")

                async def report_changes():
                    while True:
                        await self._changed.wait()
                        self._changed.clear()
                        await send(build_membership(self.lfd_id, self.replica_id, self.healthy))

                async def answer_heartbeats():
                    while True:
                        heartbeat = await read_message_with_timeout(
                            reader, self.config.heartbeat_freq + 2 * self.config.read_timeout)
                        if (heartbeat["type"] != MessageType.HEARTBEAT
                                or heartbeat["source"] != "GFD"
                                or heartbeat["destination"] != self.lfd_id):
                            raise ProtocolError("expected heartbeat from GFD")
                        count = heartbeat["heartbeat_count"]
                        self.logger.server_heartbeat_received(self.lfd_id, "GFD", count)
                        await send(build_heartbeat_ack(heartbeat))
                        self.logger.server_heartbeat_ack_sent(self.lfd_id, "GFD", count)

                sender = asyncio.create_task(report_changes())
                receiver = asyncio.create_task(answer_heartbeats())
                try:
                    done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    sender.cancel()
                    receiver.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)
            except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError) as exc:
                self.logger.error(self.lfd_id, f"GFD connection: {str(exc) or type(exc).__name__}")
            finally:
                await close_writer(writer)
            await asyncio.sleep(0.5)
