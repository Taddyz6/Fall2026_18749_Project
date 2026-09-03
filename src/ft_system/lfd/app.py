"""Configurable local fault detector heartbeat loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ft_system.common.config import LfdConfig
from ft_system.common.logging import EventLogger
from ft_system.common.protocol import (
    MessageType,
    ProtocolError,
    build_heartbeat,
)
from ft_system.common.retry import ExponentialBackoff, open_connection_with_timeout
from ft_system.common.transport import (
    close_writer,
    read_message_with_timeout,
    write_message,
)


@dataclass(frozen=True)
class HeartbeatEndpoint:
    replica_id: str
    host: str
    port: int


class LfdApp:
    """Heartbeat one local replica and report health transitions."""

    def __init__(
        self,
        config: LfdConfig,
        endpoint: HeartbeatEndpoint,
        logger: EventLogger,
    ) -> None:
        self.config = config
        self.endpoint = endpoint
        self.logger = logger
        self._heartbeat_count = 1
        self._healthy = False
        self._ever_healthy = False
        self._stop_event = asyncio.Event()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._backoff = ExponentialBackoff(
            initial=config.initial_backoff,
            maximum=config.max_backoff,
        )

    @property
    def heartbeat_count(self) -> int:
        """Return the next heartbeat number that will be transmitted."""

        return self._heartbeat_count

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def _drop_connection(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        await close_writer(writer)

    async def _connect(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        await self._drop_connection()
        self._reader, self._writer = await open_connection_with_timeout(
            self.endpoint.host,
            self.endpoint.port,
            self.config.connect_timeout,
        )

    def _mark_failed(self) -> None:
        was_healthy = self._healthy
        self._healthy = False
        if was_healthy:
            self.logger.server_failed(self.endpoint.replica_id)

    @staticmethod
    def _failure_reason(exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, EOFError):
            return "connection closed"
        return str(exc) or type(exc).__name__

    def _validate_ack(self, ack: dict, sent_count: int) -> None:
        if ack["type"] != MessageType.HEARTBEAT_ACK:
            raise ProtocolError("expected heartbeat_ack")
        if ack["source"] != self.endpoint.replica_id:
            raise ProtocolError("heartbeat ACK source does not match replica")
        if ack["destination"] != self.config.lfd_id:
            raise ProtocolError("heartbeat ACK destination does not match LFD")
        if ack["heartbeat_count"] != sent_count:
            raise ProtocolError("heartbeat ACK count does not match sent heartbeat")

    async def _heartbeat_once(self) -> None:
        await self._connect()
        if self._reader is None or self._writer is None:
            raise RuntimeError("heartbeat connection was not established")

        sent_count = self._heartbeat_count
        heartbeat = build_heartbeat(
            self.config.lfd_id,
            self.endpoint.replica_id,
            sent_count,
        )
        self.logger.heartbeat_sent(
            self.config.lfd_id, self.endpoint.replica_id, sent_count
        )
        await write_message(self._writer, heartbeat)
        self._heartbeat_count += 1

        ack = await read_message_with_timeout(self._reader, self.config.read_timeout)
        self._validate_ack(ack, sent_count)
        self.logger.heartbeat_ack(
            self.config.lfd_id, self.endpoint.replica_id, sent_count
        )

        recovering = self._ever_healthy and not self._healthy
        self._healthy = True
        self._ever_healthy = True
        self._backoff.reset()
        if recovering:
            self.logger.server_recovered(self.endpoint.replica_id)

    async def _wait_or_stop(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            return True
        except TimeoutError:
            return False

    async def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                attempted_count = self._heartbeat_count
                try:
                    await self._heartbeat_once()
                except (
                    EOFError,
                    TimeoutError,
                    ProtocolError,
                    ConnectionError,
                    OSError,
                ) as exc:
                    await self._drop_connection()
                    if self._stop_event.is_set():
                        break
                    if self._healthy:
                        self.logger.heartbeat_failed(
                            self.config.lfd_id,
                            self.endpoint.replica_id,
                            attempted_count,
                            self._failure_reason(exc),
                        )
                    self._mark_failed()
                    delay = self._backoff.next_delay()
                else:
                    delay = self.config.heartbeat_freq

                if await self._wait_or_stop(delay):
                    break
        finally:
            await self._drop_connection()

    async def stop(self) -> None:
        self._stop_event.set()
        await self._drop_connection()
