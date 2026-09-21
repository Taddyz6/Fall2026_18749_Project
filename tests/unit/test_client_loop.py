import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from ft_system.client.app import ClientApp, run_automatic
from ft_system.client.main import build_parser


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "abc", ""])
def test_cli_rejects_invalid_intervals(value):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(
            ["--config", "unused.toml", "--client-id", "C1", f"--interval={value}"]
        )
    assert error.value.code == 2


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
async def test_automatic_rejects_invalid_interval_before_sending(interval):
    client = Mock(spec=ClientApp)
    with pytest.raises(ValueError, match="finite positive"):
        await run_automatic(client, interval)
    client.send_increment.assert_not_called()


async def test_loop_paces_success_and_failure_and_propagates_cancellation(monkeypatch):
    client = Mock(spec=ClientApp)
    client.client_id = "C1"
    client.logger = Mock()
    client.send_increment = AsyncMock(side_effect=[TimeoutError(), {}])
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr("ft_system.client.app.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await run_automatic(client, 0.5)

    assert client.send_increment.await_count == 2
    assert sleep.await_args_list == [call(0.5), call(0.5)]
    client.logger.error.assert_called_once_with("C1", "TimeoutError")


def test_cli_defaults_to_automatic_with_one_second_interval():
    args = build_parser().parse_args(["--config", "local.toml", "--client-id", "C1"])
    assert args.interactive is False
    assert args.interval == 1.0
