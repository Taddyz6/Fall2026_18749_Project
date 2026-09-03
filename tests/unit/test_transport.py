import asyncio

import pytest

from ft_system.common.protocol import ProtocolError, build_heartbeat
from ft_system.common.transport import (
    MAX_MESSAGE_BYTES,
    close_writer,
    read_message,
    read_message_with_timeout,
    write_message,
)


@pytest.mark.asyncio
async def test_reads_two_json_lines_without_merging():
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    first = build_heartbeat("LFD1", "S1", 1)
    second = build_heartbeat("LFD1", "S1", 2)
    from ft_system.common.protocol import encode_message

    reader.feed_data(encode_message(first) + encode_message(second))
    reader.feed_eof()

    assert await read_message(reader) == first
    assert await read_message(reader) == second


@pytest.mark.asyncio
async def test_clean_eof_before_message_is_reported():
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    reader.feed_eof()

    with pytest.raises(EOFError, match="closed"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_invalid_json_is_reported_as_protocol_error():
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    reader.feed_data(b"not-json\n")

    with pytest.raises(ProtocolError, match="invalid JSON"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_oversized_line_is_rejected():
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    reader.feed_data(b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n")

    with pytest.raises(ProtocolError, match="exceeds"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_timeout_does_not_wait_forever():
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)

    with pytest.raises(TimeoutError):
        await read_message_with_timeout(reader, timeout=0.01)


class RecordingWriter:
    def __init__(self, *, wait_closed_error=None):
        self.data = bytearray()
        self.drain_count = 0
        self.close_count = 0
        self.wait_closed_count = 0
        self.wait_closed_error = wait_closed_error

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        self.drain_count += 1

    def close(self):
        self.close_count += 1

    async def wait_closed(self):
        self.wait_closed_count += 1
        if self.wait_closed_error:
            raise self.wait_closed_error


@pytest.mark.asyncio
async def test_write_sends_one_message_and_drains():
    writer = RecordingWriter()
    message = build_heartbeat("LFD1", "S1", 1)

    await write_message(writer, message)

    assert writer.data.endswith(b"\n")
    assert writer.drain_count == 1


@pytest.mark.asyncio
async def test_write_rejects_message_over_size_limit_before_touching_stream():
    writer = RecordingWriter()
    message = {
        "version": 1,
        "type": "error",
        "source": "S1",
        "destination": "C1",
        "payload": {"code": "large", "detail": "x" * MAX_MESSAGE_BYTES},
    }

    with pytest.raises(ProtocolError, match="exceeds"):
        await write_message(writer, message)

    assert writer.data == b""
    assert writer.drain_count == 0


@pytest.mark.asyncio
async def test_close_writer_is_idempotent_for_none_and_suppresses_disconnect_error():
    await close_writer(None)
    writer = RecordingWriter(wait_closed_error=ConnectionResetError("peer reset"))

    await close_writer(writer)

    assert writer.close_count == 1
    assert writer.wait_closed_count == 1
