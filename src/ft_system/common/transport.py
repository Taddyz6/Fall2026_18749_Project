"""Bounded JSON Lines I/O over asyncio streams."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from ft_system.common.protocol import (
    ProtocolError,
    decode_message,
    encode_message,
)


MAX_MESSAGE_BYTES = 65_536


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read, bound, decode, and validate one JSON Lines message."""

    try:
        line = await reader.readline()
    except ValueError as exc:
        raise ProtocolError("message exceeds 65536 bytes") from exc
    if not line:
        raise EOFError("connection closed")
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds 65536 bytes")
    return decode_message(line)


async def read_message_with_timeout(
    reader: asyncio.StreamReader, timeout: float
) -> dict[str, Any]:
    """Read one message or raise TimeoutError within the configured duration."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    return await asyncio.wait_for(read_message(reader), timeout)


async def write_message(
    writer: asyncio.StreamWriter, message: Mapping[str, Any]
) -> None:
    """Write and drain one bounded JSON Lines message."""

    data = encode_message(message)
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds 65536 bytes")
    writer.write(data)
    await writer.drain()


async def close_writer(writer: asyncio.StreamWriter | None) -> None:
    """Close one stream writer, tolerating an already-disconnected peer."""

    if writer is None:
        return
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
