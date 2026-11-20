import math

import pytest

pytest.importorskip("matplotlib")

from ft_system.blackbox import report  # noqa: E402
from ft_system.blackbox.baseline import Signature, train  # noqa: E402
from ft_system.blackbox.collector import when  # noqa: E402
from tests.blackbox.builders import run, sample, write_run  # noqa: E402


def faulty_run():
    """S2 runs hot from second 100 to 140, is killed at 150 and is back, young again, at 156."""

    records = [record for record in run(seconds=200)
               if not (record.get("label") == "S2" and record.get("age", 0) >= 150)]
    for record in records:
        if record.get("label") == "S2" and 100 <= record.get("age", 0) < 140:
            record["m"]["cpu"] = 75.0
    records += [sample("S2", float(second - 155), cpu=2.0, rss_mb=27.0, threads=6)
                | {"recv": 1000.0 + second, "incarnation": 2} for second in range(156, 200)]
    records += [
        {"t": 1100.0, "event": "fault_injected", "server": "S2"},
        {"t": 1140.0, "event": "fault_cleared", "server": "S2"},
        {"t": 1150.0, "event": "replica_killed", "server": "S2"},
        {"t": 1155.0, "event": "process_started", "label": "S2", "incarnation": 2},
        {"t": 1001.0, "event": "process_started", "label": "S1", "incarnation": 1},   # the first start: no rule
    ]
    for record in records:
        if record.get("event") == "recording_started":
            record["t"] = 1000.0
    return records


@pytest.fixture
def signature():
    return Signature(train(run(), warmup=90.0))


def test_collect_marks_what_is_outside_the_band_and_breaks_the_line_at_an_outage(signature):
    records = sorted(faulty_run(), key=when)

    series, events = report.collect(records, signature, ["cpu", "threads", "missing"], "server")

    assert sorted(series) == ["S1", "S2", "S3"] and sorted(series["S2"]) == ["cpu", "threads"]
    cpu = series["S2"]["cpu"]
    hot = [moment for moment, outside in zip(cpu["t"], cpu["outside"]) if outside is True]
    assert (hot[0], hot[-1], len(hot)) == (100.0, 139.0, 40)
    assert not any(outside is True for outside in series["S1"]["cpu"]["outside"])
    gap = cpu["t"].index(156.0) - 1
    assert cpu["t"][gap - 1] == 149.0 and math.isnan(cpu["t"][gap]) and math.isnan(cpu["value"][gap])
    # The relaunched process is young again, so its 6 threads are inside the warm-up band.
    young = series["S2"]["threads"]
    assert young["value"][-1] == 6 and young["outside"][-1] is False
    assert young["outer"][-1][1] > series["S1"]["threads"]["outer"][-1][1]
    assert events == [(100.0, "S2", "fault injected into S2"),
                      (109.0, "S2", "S2 diagnosed: CPU-intensive task inside the process"),
                      (140.0, "S2", "fault cleared in S2"), (147.0, "S2", "diagnosis withdrawn"),
                      (150.0, "S2", "S2 killed"), (155.0, "S2", "S2 relaunched")]


def test_figure_is_written_for_a_recorded_run(tmp_path, signature):
    folder = write_run(tmp_path / "faulty", faulty_run())

    path = report.plot_run(folder, signature, metrics=["cpu", "threads", "rss_mb"])

    assert path == folder / "faulty-metrics.png"
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and path.stat().st_size > 20_000


def test_command_line_draws_a_run_and_rejects_one_without_replicas(tmp_path, signature, capsys):
    signature.save(tmp_path / "baseline.json")
    folder = write_run(tmp_path / "quiet", run(seconds=120))
    empty = write_run(tmp_path / "empty", [sample("LFD1", 10.0, role="lfd", cpu=0.0)])
    target = ["--baseline", str(tmp_path / "baseline.json")]

    assert report.main(target + ["--run-dir", str(folder), "--out", str(tmp_path / "out" / "quiet.png")]) == 0
    with pytest.raises(SystemExit):
        report.main(target + ["--run-dir", str(empty)])

    assert (tmp_path / "out" / "quiet.png").stat().st_size > 20_000
    assert "figure written to" in capsys.readouterr().out
