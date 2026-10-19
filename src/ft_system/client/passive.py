"""Milestone 3 client: connect to every replica but issue requests only to the primary."""

from __future__ import annotations

import asyncio

from ft_system.client.app import ClientApp, ServerEndpoint
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.transport import close_writer


class PassiveClientApp:
    """Keep the rubric's three client connections while leaving backups idle."""

    def __init__(self, client_id, endpoints, primary_id, connect_timeout, read_timeout, logger):
        self.client_id = client_id
        self.endpoints = tuple(endpoints)
        self.primary_id = primary_id
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.logger = logger
        primary = next((endpoint for endpoint in self.endpoints if endpoint.replica_id == primary_id), None)
        if primary is None:
            raise ValueError("primary endpoint is required")
        self._primary = ClientApp(client_id, primary, connect_timeout, read_timeout, logger)
        self._backup_writers = []
        self._connected = False

    @property
    def next_request_num(self):
        return self._primary.next_request_num

    async def connect(self):
        if self._connected:
            return
        self._connected = True
        await self._primary.connect()

        async def connect_backup(endpoint):
            if endpoint.replica_id == self.primary_id:
                return
            try:
                _, writer = await open_connection_with_timeout(endpoint.host, endpoint.port, self.connect_timeout)
                self._backup_writers.append(writer)
                self.logger.event(f"{self.client_id}: connected to idle backup {endpoint.replica_id}")
            except (TimeoutError, ConnectionError, OSError) as exc:
                self.logger.error(self.client_id, f"{endpoint.replica_id} unavailable: {exc}")

        await asyncio.gather(*(connect_backup(endpoint) for endpoint in self.endpoints))

    async def send_increment(self):
        await self.connect()
        return await self._primary.send_increment()

    async def close(self):
        await self._primary.close()
        await asyncio.gather(*(close_writer(writer) for writer in self._backup_writers))
        self._backup_writers.clear()
