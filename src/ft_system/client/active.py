"""Fan out each request and deliver the first valid replica reply."""

import asyncio
from dataclasses import dataclass

from ft_system.client.app import ClientApp, ServerEndpoint
from ft_system.common.protocol import ProtocolError, build_client_request
from ft_system.common.retry import open_connection_with_timeout
from ft_system.common.transport import close_writer, read_message, write_message


@dataclass
class ReplicaConnection:
    endpoint: ServerEndpoint
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    last_sent: int = 0
    last_received: int = 0
    task: asyncio.Task | None = None


class ActiveClientApp:
    def __init__(self, client_id, endpoints, connect_timeout, read_timeout, logger):
        self.client_id = client_id
        self.endpoints = tuple(endpoints)
        if not self.endpoints or len({e.replica_id for e in self.endpoints}) != len(self.endpoints):
            raise ValueError("unique replica endpoints are required")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.logger = logger
        self._connections = {}
        self._started = False
        self._next_request_num = 1
        self._delivered = 0
        self._last_reply = None
        self._pending = None
        self._send_lock = asyncio.Lock()
        self._tasks = set()
        self._all_connections = []
        self._delivered_values = {}
        self._closing = False

    @property
    def next_request_num(self):
        return self._next_request_num

    async def connect(self):
        if self._started:
            return
        self._started = True

        async def open_replica(endpoint):
            try:
                reader, writer = await open_connection_with_timeout(
                    endpoint.host, endpoint.port, self.connect_timeout)
            except (TimeoutError, ConnectionError, OSError) as exc:
                self.logger.error(self.client_id, f"{endpoint.replica_id} unavailable: {exc}")
                return
            connection = ReplicaConnection(endpoint, reader, writer)
            self._connections[endpoint.replica_id] = connection
            self._all_connections.append(connection)
            connection.task = asyncio.create_task(self._receive(connection))
            self._tasks.add(connection.task)
            connection.task.add_done_callback(self._tasks.discard)
            connection.task.add_done_callback(lambda _: self._prune_delivered_values())

        await asyncio.gather(*(open_replica(endpoint) for endpoint in self.endpoints))

    async def _retire(self, connection, reason):
        replica = connection.endpoint.replica_id
        if self._connections.get(replica) is connection:
            del self._connections[replica]
            if not self._closing:
                self.logger.error(self.client_id, f"{replica} unavailable: {reason}")
        await close_writer(connection.writer)

    def _prune_delivered_values(self):
        """Keep a reply value only while another replica can still answer it."""
        for number in tuple(self._delivered_values):
            if all(c.last_received >= number or (c.task is not None and c.task.done())
                   for c in self._all_connections):
                del self._delivered_values[number]

    async def _receive(self, connection):
        replica = connection.endpoint.replica_id
        try:
            while True:
                reply = await read_message(connection.reader)
                number = reply.get("request_num", 0)
                if not connection.last_received <= number <= connection.last_sent or number < 1:
                    raise ProtocolError("reply is outside the sent request sequence")
                request = build_client_request(self.client_id, replica, number)
                ClientApp._validate_reply(reply, request)
                connection.last_received = number
                self.logger.received(reply, "reply")
                if number <= self._delivered:
                    expected = self._delivered_values.get(number)
                    if expected is not None and reply["payload"]["client_value"] != expected:
                        self.logger.error(self.client_id,
                                          f"replica state divergence on request_num {number}: {replica}")
                    self.logger.duplicate(number, replica)
                elif number == self._next_request_num:
                    self._delivered = number
                    self._delivered_values[number] = reply["payload"]["client_value"]
                    self._last_reply = reply
                    self.logger.event(f"request_num {number}: Delivered reply from {replica}; client_value = {reply['payload']['client_value']}")
                    if self._pending is not None and not self._pending.done():
                        self._pending.set_result(reply)
                else:
                    raise ProtocolError("unexpected logical request number")
                self._prune_delivered_values()
        except (EOFError, TimeoutError, ProtocolError, ConnectionError, OSError) as exc:
            await self._retire(connection, str(exc) or type(exc).__name__)
        finally:
            await close_writer(connection.writer)

    async def send_increment(self):
        async with self._send_lock:
            await self.connect()
            # A reply can arrive after the caller's timeout, before its retry.
            if self._delivered == self._next_request_num:
                self._next_request_num += 1
                return self._last_reply
            if not self._connections:
                raise ConnectionError("no replicas remain; restart the complete demo to reset state")
            number = self._next_request_num
            self._pending = asyncio.get_running_loop().create_future()

            async def send(connection):
                request = build_client_request(self.client_id, connection.endpoint.replica_id, number)
                connection.last_sent = number
                self.logger.sending(request, "request")
                try:
                    await asyncio.wait_for(write_message(connection.writer, request), self.connect_timeout)
                except (TimeoutError, ConnectionError, OSError) as exc:
                    await self._retire(connection, str(exc) or type(exc).__name__)

            try:
                # Complete the broadcast before admitting the next logical request.
                # Independent readers keep draining slow and late replies meanwhile.
                await asyncio.gather(*(send(c) for c in tuple(self._connections.values())))
                reply = await asyncio.wait_for(asyncio.shield(self._pending), self.read_timeout)
                self._next_request_num += 1
                return reply
            finally:
                if self._pending is not None and not self._pending.done():
                    self._pending.cancel()
                self._pending = None

    async def close(self):
        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(close_writer(c.writer) for c in self._connections.values()))
        self._connections.clear()
