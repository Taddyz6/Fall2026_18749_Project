from datetime import datetime

from ft_system.blackbox import impact

CLIENT_LOG = """\
[2026-11-19 20:42:58.330] Sending <C1, S1, 1, request>
[2026-11-19 20:42:58.330] Sending <C1, S2, 1, request>
[2026-11-19 20:42:58.330] C1: view 6: members: S1, S2, S3; ready: S1, S2, S3
[2026-11-19 20:42:58.331] Received <C1, S1, 1, reply>
[2026-11-19 20:42:58.331] request_num 1: Delivered reply from S1; client_value = 1
[2026-11-19 20:42:58.356] Received <C1, S2, 1, reply>
[2026-11-19 20:42:58.356] request_num 1: Discarded duplicate reply from S2
[2026-11-19 20:42:58.539] Sending <C1, S1, 2, request>
[2026-11-19 20:42:58.540] Sending <C1, S2, 2, request>
[2026-11-19 20:42:58.541] Received <C1, S1, 2, reply>
[2026-11-19 20:43:00.541] C1 ERROR: S2 unavailable: timed out
a line without a timestamp
"""


def at(clock: str) -> float:
    return datetime.fromisoformat(f"2026-11-19 {clock}").timestamp()


def test_reply_times_pair_each_request_with_that_servers_reply():
    times, unanswered = impact.reply_times(CLIENT_LOG)

    assert times == {"S1": [1.0, 2.0], "S2": [26.0]}
    assert unanswered == {"S1": 0, "S2": 1}


def test_reply_times_select_requests_by_when_they_were_sent():
    times, unanswered = impact.reply_times(CLIENT_LOG, start=at("20:42:58.500"))
    assert (times, unanswered) == ({"S1": [2.0], "S2": []}, {"S1": 0, "S2": 1})

    # A reply that arrives after the window still counts for a request sent inside it.
    times, unanswered = impact.reply_times(CLIENT_LOG, end=at("20:42:58.340"))
    assert (times, unanswered) == ({"S1": [1.0], "S2": [26.0]}, {"S1": 0, "S2": 0})


def test_count_lines_matches_message_text_inside_the_window():
    assert impact.count_lines(CLIENT_LOG, r"ERROR") == 1
    assert impact.count_lines(CLIENT_LOG, r"ERROR", end=at("20:43:00.541")) == 0
    assert impact.count_lines(CLIENT_LOG, r"^Sending <C1, S2,") == 2
    assert impact.count_lines(CLIENT_LOG, r"^Sending", start=at("20:42:58.539"), end=at("20:42:58.540")) == 1


def test_summarize_reports_median_95th_percentile_and_maximum():
    assert impact.summarize([]) is None
    assert impact.summarize([5.0]) == {"n": 1, "median": 5.0, "p95": 5.0, "max": 5.0}
    assert impact.summarize([4.0, 1.0, 3.0, 2.0]) == {"n": 4, "median": 2.5, "p95": 4.0, "max": 4.0}
    assert impact.summarize(list(range(1, 101))) == {"n": 100, "median": 50.5, "p95": 95, "max": 100}
