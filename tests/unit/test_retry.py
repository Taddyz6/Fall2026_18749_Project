import asyncio

import pytest

from ft_system.common.retry import ExponentialBackoff, open_connection_with_timeout


def test_backoff_doubles_and_caps_then_resets():
    backoff = ExponentialBackoff(initial=0.5, maximum=2.0)

    assert [backoff.next_delay() for _ in range(4)] == [0.5, 1.0, 2.0, 2.0]
    backoff.reset()

    assert backoff.next_delay() == 0.5


@pytest.mark.parametrize(
    ("initial", "maximum"),
    [(0, 1), (-1, 1), (1, 0), (2, 1)],
)
def test_rejects_invalid_backoff_bounds(initial, maximum):
    with pytest.raises(ValueError):
        ExponentialBackoff(initial=initial, maximum=maximum)


@pytest.mark.asyncio
async def test_connection_attempt_honors_timeout(monkeypatch):
    started = asyncio.Event()

    async def never_connect(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "open_connection", never_connect)

    with pytest.raises(TimeoutError):
        await open_connection_with_timeout("127.0.0.1", 5001, timeout=0.01)

    assert started.is_set()


@pytest.mark.asyncio
async def test_connection_helper_returns_real_loopback_streams():
    connected = asyncio.Event()

    async def handle(reader, writer):
        connected.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await open_connection_with_timeout(
            "127.0.0.1", port, timeout=0.2
        )
        await asyncio.wait_for(connected.wait(), timeout=0.2)
        assert isinstance(reader, asyncio.StreamReader)
        assert isinstance(writer, asyncio.StreamWriter)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
