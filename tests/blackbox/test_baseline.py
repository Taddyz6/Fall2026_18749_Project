import pytest

from ft_system.blackbox import baseline
from ft_system.blackbox.baseline import Signature, check, describe, level_of, spread, train
from tests.blackbox.builders import run, sample, write_run


def test_describe_gives_mean_deviation_and_percentiles():
    stats = describe([float(value) for value in range(1, 101)])

    assert (stats["n"], stats["mean"], stats["median"]) == (100, 50.5, 50.0)
    assert (stats["p01"], stats["p99"], stats["min"], stats["max"]) == (1.0, 99.0, 1.0, 100.0)
    assert stats["std"] == pytest.approx(29.0115, abs=1e-4)
    assert describe([3.0])["std"] == 0.0


@pytest.mark.parametrize("metric, mean, std, expected", [
    ("cpu", 0.5, 0.0, 2.0),                 # an idle process: two percentage points, not zero
    ("cpu_user", 40.0, 5.0, 5.0),           # a real deviation above the floor is kept
    ("threads", 4.0, 0.0, 0.5),
    ("rss_mb", 27.0, 0.0, 0.216),           # 0.8% of the mean: six deviations are about 5%
    ("io_write_count_s", 120.0, 4.0, 12.0),  # 10% of the mean
    ("io_read_count_s", 0.0, 0.0, 1.0),
    ("io_read_bytes_s", 0.0, 0.0, 1024.0),
    ("something_else", 10.0, 0.0, 0.5),
])
def test_spread_never_falls_below_the_floor_for_that_kind_of_metric(metric, mean, std, expected):
    assert spread(metric, {"mean": mean, "std": std}) == pytest.approx(expected)


def test_level_counts_deviations_on_either_side():
    stats = {"mean": 10.0, "std": 1.0}

    assert [level_of(value, "something_else", stats) for value in (10.0, 12.9, 13.0, 15.9, 16.0, 7.0, 4.0)] == [
        0, 0, 1, 1, 2, -1, -2]


def test_training_separates_warm_up_from_steady_state():
    signature = Signature(train(run(), warmup=90.0))

    own = signature.data["components"]["S2"]
    assert own["role"] == "server" and own["steady"]["threads"]["n"] == 110   # seconds 90..199
    assert (own["steady"]["threads"]["mean"], own["steady"]["threads"]["std"]) == (4.0, 0.0)
    pooled = signature.data["roles"]["server"]
    assert pooled["steady"]["cpu"]["n"] == 330 and pooled["warmup"]["cpu"]["n"] == 3 * 85
    assert pooled["warmup"]["threads"]["min"] == 4 and pooled["warmup"]["threads"]["max"] == 6
    assert signature.data["components"]["host:pc"]["steady"]["cpu"]["n"] == 195  # a host has no age
    assert signature.meta | {"created": 0} == {
        "host": "pc", "os": "TestOS", "python": "3.12", "cores": 8, "interval": 1.0, "config": "m5.toml",
        "mode": "active", "client_interval": 0.2, "seconds": 200, "samples": 195 * 5, "created": 0}


def test_training_refuses_a_run_with_an_injected_fault_or_no_samples():
    with pytest.raises(ValueError, match="not a fault-free run: a fault was injected into S2"):
        train(run() + [{"event": "fault_injected", "server": "S2"}])
    with pytest.raises(ValueError, match="no samples"):
        train([{"event": "agent_started", "host": "pc"}])


def test_a_young_process_is_judged_against_the_warm_up_phase():
    signature = Signature(train(run(), warmup=90.0))

    assert signature.stats("S1", "server", "threads", age=20.0)["max"] == 6      # pooled warm-up
    assert signature.stats("S1", "server", "threads", age=120.0)["max"] == 4     # its own steady state
    assert signature.stats("S1", "server", "threads")["max"] == 4                 # age unknown: steady
    young = sample("S1", 20.0, cpu=2.0, threads=6)
    mature = sample("S1", 120.0, cpu=2.0, threads=6)
    assert signature.level(young, "threads") == 0
    assert signature.level(mature, "threads") == 1          # 4 deviations of 0.5 above 4


def test_stats_fall_back_to_the_role_when_a_component_has_too_little_of_its_own():
    records = run(servers=("S1", "S2")) + [sample("S3", 150.0 + i, cpu=50.0, threads=4) for i in range(5)]
    signature = Signature(train(records, warmup=90.0))

    assert signature.data["components"]["S3"]["steady"]["cpu"]["n"] == 5
    assert signature.stats("S3", "server", "cpu", age=150.0)["n"] == 225          # the pooled servers
    assert signature.stats("S9", "server", "cpu", age=150.0)["n"] == 225          # never seen in training
    assert signature.stats("S1", "server", "ctx_voluntary_s", age=150.0) is None  # a metric never recorded
    assert signature.stats("X1", "mystery", "cpu") is None
    assert signature.level(sample("X1", 150.0, role="mystery", cpu=99.0), "cpu") is None
    assert signature.level(sample("S1", 150.0, threads=4), "cpu") is None          # the sample lacks it


def test_band_is_six_deviations_wide_and_stops_at_zero():
    signature = Signature(train(run(), warmup=90.0))
    stats = signature.stats("S1", "server", "cpu", age=150.0)

    low, high = signature.band("S1", "server", "cpu", age=150.0)
    assert (low, high) == (0.0, pytest.approx(stats["mean"] + 6 * 2.0))            # the floor, clipped at 0
    assert signature.band("S1", "server", "cpu", age=150.0, sigmas=3.0)[1] == pytest.approx(stats["mean"] + 6.0)
    assert signature.band("S1", "server", "rss_mb", age=150.0) == pytest.approx((27.0 - 1.296, 27.0 + 1.296))
    assert signature.band("X1", "mystery", "cpu") is None


def test_check_counts_excursions_and_reports_only_sustained_ones():
    signature = Signature(train(run(), warmup=90.0))
    quiet = [sample("S1", 100.0 + i, cpu=2.0, rss_mb=27.0, threads=4) for i in range(20)]
    blips = [sample("S2", 100.0 + i, cpu=90.0 if i in (3, 12) else 2.0, rss_mb=27.0, threads=4) for i in range(20)]
    fault = [sample("S3", 100.0 + i, cpu=75.0 if i >= 10 else 2.0, rss_mb=27.0, threads=4) for i in range(20)]

    rows = check(signature, quiet + blips + fault + [sample("LFD1", 100.0, role="lfd", cpu=99.0)])

    assert rows[("S1", "cpu")] == {"samples": 20, "warn": 0, "anomalous": 0, "sustained": 0}
    assert rows[("S2", "cpu")] == {"samples": 20, "warn": 2, "anomalous": 2, "sustained": 0}
    # From the third anomalous sample on, three of the last seven are anomalous.
    assert rows[("S3", "cpu")] == {"samples": 20, "warn": 10, "anomalous": 10, "sustained": 8}
    assert ("LFD1", "cpu") not in rows                       # only the replicas are under diagnosis


def test_signature_survives_a_round_trip_and_names_what_differs(tmp_path):
    signature = Signature(train(run(), warmup=90.0))
    signature.save(tmp_path / "deep" / "baseline.json")
    loaded = Signature.load(tmp_path / "deep" / "baseline.json")

    assert loaded.data == signature.data
    assert loaded.mismatches(signature.meta) == []
    assert loaded.mismatches({"host": "laptop", "client_interval": 0.1, "mode": "active", "seconds": 5}) == [
        "host: trained on 'pc', this run has 'laptop'",
        "client_interval: trained on 0.2, this run has 0.1"]


def test_command_line_trains_shows_and_checks(tmp_path, capsys):
    target = ["--baseline", str(tmp_path / "baseline.json")]
    write_run(tmp_path / "train", run())
    write_run(tmp_path / "same", run(seconds=150))
    write_run(tmp_path / "busy", run(seconds=150, cpu=lambda second: 80.0 if second > 100 else 2.0))
    write_run(tmp_path / "faulty", run() + [{"event": "fault_injected", "server": "S2"}])

    assert baseline.main(target + ["train", "--run-dir", str(tmp_path / "train")]) == 0
    assert baseline.main(target + ["show"]) == 0
    assert baseline.main(target + ["check", "--run-dir", str(tmp_path / "same")]) == 0
    quiet = capsys.readouterr().out
    assert baseline.main(target + ["check", "--run-dir", str(tmp_path / "busy")]) == 1
    busy = capsys.readouterr().out
    with pytest.raises(SystemExit):
        baseline.main(target + ["train", "--run-dir", str(tmp_path / "faulty")])

    assert "a process is in warm-up for its first 90 s" in quiet
    assert "verdict: every server stays inside the signature" in quiet
    assert "warning: seconds" not in quiet                       # run length is not part of the workload
    assert "verdict: outside the signature: S1, S2, S3" in busy
