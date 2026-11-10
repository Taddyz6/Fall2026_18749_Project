"""Passive-replication client: connect to every replica, talk only to the primary."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from ft_system.client.app import ClientApp
from ft_system.common.protocol import MessageType, ProtocolError, build_client_request
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.transport import close_writer, read_message_with_timeout, write_message


class PassiveClientApp:
    """Keep the rubric's idle backup connections and follow the RM's choice of primary.

    The request number advances only after a reply, so a request that fails
    because the primary crashed is retried with the same number at the next
    primary; replica-side duplicate detection keeps a retry from applying twice.
    """

    def __init__(self, client_id, endpoints, primary_id, connect_timeout, read_timeout, logger):
        self.client_id = client_id
        self._endpoints = {endpoint.replica_id: endpoint for endpoint in endpoints}
        if primary_id is not None and primary_id not in self._endpoints:
            raise ValueError("primary endpoint is required")
        self.primary_id = primary_id
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.logger = logger
        self._next_request_num = 1
        self._members = set(self._endpoints)
        self._primary = None  # (replica_id, reader, writer)
        self._idle = {}  # replica_id -> (reader, writer)
        self._sync_needed = True
        self._primary_known = asyncio.Event()
        if primary_id is not None:
            self._primary_known.set()
        self._send_lock = asyncio.Lock()

    @property
    def next_request_num(self):
        return self._next_request_num

    def status_lines(self):
        connected = self._primary is not None and not self._primary[2].is_closing()
        return [f"next request_num {self._next_request_num}; primary {self.primary_id or 'unknown'} "
                f"({'connected' if connected else 'not connected'})",
                f"idle backup connections: {', '.join(sorted(self._idle)) or 'none'}; "
                f"members per latest view: {', '.join(sorted(self._members)) or 'none'}"]

    def apply_view(self, view):
        members = set(view["members"]) & set(self._endpoints)
        if members != self._members:
            self._members = members
            self._sync_needed = True
        primary = view["primary"]
        if primary is not None and primary != self.primary_id and primary in self._endpoints:
            if self.primary_id is None:
                self.logger.event(f"{self.client_id}: primary is {primary}", "recovered")
            else:
                self.logger.event(f"{self.client_id}: primary changed {self.primary_id} -> {primary}", "recovered")
            self.primary_id = primary
            self._primary_known.set()

    async def _open(self, replica):
        endpoint = self._endpoints[replica]
        return await open_connection_with_timeout(endpoint.host, endpoint.port, self.connect_timeout)

    async def _sync_idle(self):
        """Hold one idle connection to each backup in the current membership."""
        if not self._sync_needed:
            return
        self._sync_needed = False
        primary = self._primary[0] if self._primary is not None else self.primary_id
        for replica in sorted(set(self._idle) - self._members):
            await close_writer(self._idle.pop(replica)[1])
        for replica in sorted(self._members - set(self._idle) - {primary}):
            try:
                self._idle[replica] = await self._open(replica)
            except (TimeoutError, ConnectionError, OSError) as exc:
                self.logger.error(self.client_id, f"{replica} unavailable: {exc}")
                continue
            self.logger.event(f"{self.client_id}: connected to idle backup {replica}")

    async def _primary_connection(self, primary):
        """Return a connection to ``primary``; the caller chose it before any await."""
        if self._primary is not None:
            replica, reader, writer = self._primary
            if replica == primary and not writer.is_closing():
                return reader, writer
            self._primary = None
            if replica in self._members and not writer.is_closing():
                self._idle[replica] = (reader, writer)  # A demoted primary becomes an idle backup.
            else:
                await close_writer(writer)
        connection = self._idle.pop(primary, None)
        if connection is None or connection[1].is_closing():
            connection = await self._open(primary)
        self._primary = (primary, *connection)
        return connection

    async def _drop_primary(self):
        if self._primary is not None:
            await close_writer(self._primary[2])
            self._primary = None

    async def connect(self):
        async with self._send_lock:
            await self._sync_idle()

    async def send_increment(self):
        async with self._send_lock:
            await self._sync_idle()
            if self.primary_id is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._primary_known.wait(), self.connect_timeout)
                if self.primary_id is None:
                    raise ConnectionError("no primary known yet; waiting for the RM view")
            # The view may change during the awaits below; this request goes to one primary.
            primary = self.primary_id
            number = self._next_request_num
            request = build_client_request(self.client_id, primary, number)
            try:
                reader, writer = await self._primary_connection(primary)
                self.logger.sending(request, "request")
                await write_message(writer, request)
                reply = await read_message_with_timeout(reader, self.read_timeout)
                if reply["type"] == MessageType.ERROR and reply["payload"]["code"] == "not_primary":
                    raise ProtocolError(f"{primary} is not the primary (it reports {reply['payload']['detail']})")
                ClientApp._validate_reply(reply, request)
            except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError) as exc:
                await self._drop_primary()
                self.logger.event(f"{self.client_id}: no reply from primary {primary} for request_num {number} "
                                  f"({str(exc) or type(exc).__name__}); will retry with the RM's current primary",
                                  "failed")
                raise
            self.logger.received(reply, "reply")
            self._next_request_num += 1
            return reply

    async def close(self):
        await self._drop_primary()
        idle, self._idle = list(self._idle.values()), {}
        await asyncio.gather(*(close_writer(writer) for _, writer in idle))
