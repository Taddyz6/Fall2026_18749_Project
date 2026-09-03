import io

import pytest

from ft_system.common.logging import EventLogger
from ft_system.common.protocol import build_client_reply, build_client_request


FIXED_TIME = "2026-09-01 12:00:00.123"


def logger_for(stream):
    return EventLogger(
        stream=stream,
        clock=lambda: FIXED_TIME,
        use_color=False,
    )


def test_server_request_log_matches_rubric_wording():
    stream = io.StringIO()
    logger = logger_for(stream)
    request = build_client_request("C1", "S1", 7)

    logger.received(request, "request")
    logger.state("S1", {"C1": 0, "C2": 0, "C3": 0}, "before", request)
    logger.state("S1", {"C1": 1, "C2": 0, "C3": 0}, "after", request)
    logger.sending(build_client_reply(request, 1), "reply")

    assert stream.getvalue().splitlines() == [
        "[2026-09-01 12:00:00.123] Received <C1, S1, 7, request>",
        '[2026-09-01 12:00:00.123] my_state_S1 = {"C1": 0, "C2": 0, "C3": 0} before processing <C1, S1, 7, request>',
        '[2026-09-01 12:00:00.123] my_state_S1 = {"C1": 1, "C2": 0, "C3": 0} after processing <C1, S1, 7, request>',
        "[2026-09-01 12:00:00.123] Sending <C1, S1, 7, reply>",
    ]


def test_client_log_uses_same_tuple_for_sent_and_received_messages():
    stream = io.StringIO()
    logger = logger_for(stream)
    request = build_client_request("C2", "S1", 9)
    reply = build_client_reply(request, 3)

    logger.sending(request, "request")
    logger.received(reply, "reply")

    assert stream.getvalue().splitlines() == [
        "[2026-09-01 12:00:00.123] Sending <C2, S1, 9, request>",
        "[2026-09-01 12:00:00.123] Received <C2, S1, 9, reply>",
    ]


def test_lfd_logs_every_heartbeat_count_and_ack():
    stream = io.StringIO()
    logger = logger_for(stream)

    logger.heartbeat_sent("LFD1", "S1", 1)
    logger.heartbeat_ack("LFD1", "S1", 1)
    logger.heartbeat_sent("LFD1", "S1", 2)

    assert stream.getvalue().splitlines() == [
        "[2026-09-01 12:00:00.123] [1] LFD1 sending heartbeat to S1",
        "[2026-09-01 12:00:00.123] [1] LFD1 receives heartbeat from S1",
        "[2026-09-01 12:00:00.123] [2] LFD1 sending heartbeat to S1",
    ]


def test_server_logs_heartbeat_receipt_and_ack_send():
    stream = io.StringIO()
    logger = logger_for(stream)

    logger.server_heartbeat_received("S1", "LFD1", 4)
    logger.server_heartbeat_ack_sent("S1", "LFD1", 4)

    assert stream.getvalue().splitlines() == [
        "[2026-09-01 12:00:00.123] [4] S1 receives heartbeat from LFD1",
        "[2026-09-01 12:00:00.123] [4] S1 sending heartbeat ACK to LFD1",
    ]


def test_failure_and_recovery_are_clear_single_events():
    stream = io.StringIO()
    logger = logger_for(stream)

    logger.server_failed("S1")
    logger.server_recovered("S1")

    assert stream.getvalue().splitlines() == [
        "[2026-09-01 12:00:00.123] S1 has died",
        "[2026-09-01 12:00:00.123] S1 has recovered",
    ]


def test_error_names_the_component():
    stream = io.StringIO()
    logger = logger_for(stream)

    logger.error("LFD1", "connection refused")

    assert stream.getvalue() == (
        "[2026-09-01 12:00:00.123] LFD1 ERROR: connection refused\n"
    )


def test_rejects_invalid_state_phase_without_printing():
    stream = io.StringIO()
    logger = logger_for(stream)

    with pytest.raises(ValueError, match="phase"):
        logger.state(
            "S1",
            {"C1": 0},
            "during",
            build_client_request("C1", "S1", 1),
        )

    assert stream.getvalue() == ""


class FlushCountingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        super().flush()


def test_each_log_event_flushes_immediately():
    stream = FlushCountingStream()
    logger = logger_for(stream)

    logger.server_failed("S1")
    logger.server_recovered("S1")

    assert stream.flush_count == 2
