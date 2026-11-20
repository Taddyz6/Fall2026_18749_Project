import pytest

from ft_system.blackbox import diagnose
from ft_system.blackbox.baseline import Signature, train
from ft_system.blackbox.classify import classify
from ft_system.blackbox.detect import FAST, PAPER, MetricTrack, ReplicaDetector
from ft_system.blackbox.diagnose import Diagnoser, fault_intervals, replay, score
from ft_system.blackbox.fingerpoint import FAULT, HEALTHY, WORKLOAD, fingerpoint
from tests.blackbox.builders import in_time_order as ordered
from tests.blackbox.builders import run, sample, write_run
from tests.blackbox.builders import run_with_fault as recorded

CPU = {"mean": 1.0, "std": 0.0}       # the CPU floor makes one deviation 2 points: level 2 from 13


def track(values, profile=PAPER, metric="cpu", stats=CPU, phase="steady"):
    result = MetricTrack(profile)
    for value in values:
        result.observe(value, metric, stats, phase)
    return result


def test_an_excursion_is_logged_at_three_of_seven_and_problematic_after_more_than_half_of_fifteen():
    quiet = track([1.0] * 20)
    two_blips = track([1.0] * 10 + [80.0, 1.0, 1.0, 80.0, 1.0, 1.0, 1.0])
    third_hit = track([1.0] * 10 + [80.0, 80.0, 80.0])
    seven_logged = track([1.0] * 10 + [80.0] * 9)       # logged from the 3rd anomalous sample: 7 times
    eight_logged = track([1.0] * 10 + [80.0] * 10)

    assert (quiet.problematic, list(quiet.logged)) == (0, [0] * 15)
    assert (list(two_blips.levels).count(2), list(two_blips.logged)[-7:]) == (2, [0] * 7)
    assert list(third_hit.logged)[-3:] == [0, 0, 1] and third_hit.problematic == 0
    assert seven_logged.problematic == 0 and eight_logged.problematic == 1


def test_the_fast_profile_needs_fewer_samples():
    assert track([1.0] * 10 + [80.0] * 5, FAST).problematic == 0
    assert track([1.0] * 10 + [80.0] * 6, FAST).problematic == 1


def test_low_side_excursions_are_tracked_with_their_sign():
    stats = {"mean": 120.0, "std": 5.0}                # a rate: one deviation is 12, level 2 below 48
    low = track([120.0] * 5 + [10.0] * 10, metric="io_write_count_s", stats=stats)

    assert (low.problematic, low.ongoing, low.typical, low.spread) == (-1, -1, 10.0, 12.0)
    assert low.band == pytest.approx((48.0, 192.0))


def test_an_excursion_stops_being_ongoing_as_soon_as_recent_values_are_normal():
    still = track([1.0] * 10 + [80.0] * 20 + [1.0] * 3)
    over = track([1.0] * 10 + [80.0] * 20 + [1.0] * 4)

    assert (still.problematic, still.ongoing) == (1, 1)
    assert (over.problematic, over.ongoing, over.typical) == (1, 0, 1.0)     # remembered, but finished


def test_the_running_mean_follows_slow_drift_but_not_an_anomaly():
    drifting = track([1.0 + 0.2 * step for step in range(100)])       # ends near 21, never 6 deviations away
    stepped = track([1.0] * 20 + [80.0] * 100)

    assert drifting.mean == pytest.approx(21.0 - 0.2 * 19, abs=0.5) and drifting.problematic == 0
    assert all(level == 0 for level in drifting.levels)
    assert stepped.mean == pytest.approx(1.0) and stepped.problematic == 1
    assert stepped.band == pytest.approx((0.0, 13.0))


def test_a_change_of_phase_starts_again_from_the_trained_mean():
    result = track([5.0] * 50, stats={"mean": 5.0, "std": 0.0}, metric="threads", phase="warmup")
    result.observe(4.0, "threads", {"mean": 4.0, "std": 0.0}, "steady")

    assert (result.mean, result.phase, result.levels[-1]) == (4.0, "steady", 0)


@pytest.fixture
def signature():
    return Signature(train(run(), warmup=90.0))


def test_replica_detector_ignores_a_newborn_and_forgets_on_a_new_incarnation(signature):
    detector = ReplicaDetector("S1", signature, PAPER)
    for second in range(100, 120):
        detector.observe(sample("S1", float(second), cpu=75.0, rss_mb=27.0) | {"incarnation": 1})
    assert detector.tracks["cpu"].problematic == 1 and "rss_mb" in detector.tracks

    detector.observe(sample("S1", 2.0, cpu=75.0) | {"incarnation": 2, "recv": 2000.0})
    assert detector.tracks == {} and detector.last_seen == 2000.0       # seen, but too young to judge

    detector.observe(sample("S1", 11.0, cpu=2.0, mystery=9.0) | {"incarnation": 2})
    assert list(detector.tracks) == ["cpu"] and detector.tracks["cpu"].phase == "warmup"


def tracks(profile=PAPER, **series):
    return {metric: track(values, profile, metric, {"mean": 1.0, "std": 0.0} if metric.startswith("cpu")
                          else {"mean": 120.0, "std": 5.0})
            for metric, values in series.items()}


NORMAL_CPU, HOT_CPU = [1.0] * 20, [1.0] * 5 + [80.0] * 15
NORMAL_IO, HIGH_IO = [120.0] * 20, [120.0] * 5 + [250.0] * 15


def test_the_one_odd_replica_is_blamed():
    verdict = fingerpoint({"S1": tracks(cpu=NORMAL_CPU), "S2": tracks(cpu=HOT_CPU), "S3": tracks(cpu=NORMAL_CPU)})

    assert (verdict.status, verdict.culprit, verdict.blamed, verdict.common) == (FAULT, "S2", {"cpu": 1}, {})


def test_nobody_is_blamed_when_all_replicas_move_together():
    verdict = fingerpoint({label: tracks(cpu=NORMAL_CPU, io_write_count_s=HIGH_IO) for label in ("S1", "S2", "S3")})

    assert (verdict.status, verdict.culprit, verdict.common) == (WORKLOAD, None, {"io_write_count_s": 1})


def test_two_replicas_that_moved_are_both_odd_when_the_third_stayed_behind():
    replicas = {"S1": tracks(io_write_count_s=HIGH_IO), "S2": tracks(io_write_count_s=NORMAL_IO),
                "S3": tracks(io_write_count_s=HIGH_IO)}

    verdict = fingerpoint(replicas)

    # The first by name is named; both are listed.
    assert (verdict.status, verdict.culprit, sorted(verdict.suspects)) == (FAULT, "S1", ["S1", "S3"])


def test_peers_that_moved_the_same_way_but_stopped_short_of_the_threshold_are_not_left_behind():
    """Level 2 starts at 192 here. One replica crossed it; the others sit just under, at 185.

    Their running means then follow them, so on their own they soon look normal. Next to
    the replica that crossed, they are still only 5 deviations away: the change is shared.
    """

    crossed, nearly = [120.0] * 5 + [245.0] * 60, [120.0] * 5 + [185.0] * 60
    replicas = {"S1": tracks(io_write_count_s=crossed), "S2": tracks(io_write_count_s=nearly),
                "S3": tracks(io_write_count_s=nearly)}

    verdict = fingerpoint(replicas)

    assert [replicas[label]["io_write_count_s"].problematic for label in ("S1", "S2", "S3")] == [1, 0, 0]
    assert replicas["S2"]["io_write_count_s"].levels[-1] == 0          # its mean has caught up
    assert (verdict.status, verdict.common, verdict.suspects) == (WORKLOAD, {"io_write_count_s": 1}, {})


def test_a_replica_far_above_peers_that_stopped_short_is_still_odd():
    far, nearly = [120.0] * 5 + [400.0] * 60, [120.0] * 5 + [185.0] * 60
    verdict = fingerpoint({"S1": tracks(io_write_count_s=far), "S2": tracks(io_write_count_s=nearly),
                           "S3": tracks(io_write_count_s=nearly)})

    assert (verdict.status, verdict.culprit) == (FAULT, "S1")


def test_the_replica_odd_on_most_metrics_is_blamed_and_a_tie_goes_to_the_previous_culprit():
    replicas = {"S1": tracks(cpu=HOT_CPU), "S2": tracks(cpu=HOT_CPU, cpu_user=HOT_CPU),
                "S3": tracks(cpu=NORMAL_CPU, cpu_user=NORMAL_CPU)}
    tied = {"S1": tracks(cpu=HOT_CPU), "S2": tracks(cpu=HOT_CPU), "S3": tracks(cpu=NORMAL_CPU)}

    assert fingerpoint(replicas).culprit == "S2"
    assert fingerpoint(tied).culprit == "S1" and fingerpoint(tied, previous="S2").culprit == "S2"


def test_a_fault_is_still_blamed_while_the_workload_also_changes():
    replicas = {label: tracks(cpu=HOT_CPU if label == "S2" else NORMAL_CPU, io_write_count_s=HIGH_IO)
                for label in ("S1", "S2", "S3")}

    verdict = fingerpoint(replicas)

    assert (verdict.status, verdict.culprit, verdict.blamed) == (FAULT, "S2", {"cpu": 1})
    assert verdict.common == {"io_write_count_s": 1}


def test_a_lone_replica_is_blamed_and_no_replicas_are_healthy():
    assert fingerpoint({"S1": tracks(cpu=HOT_CPU)}).culprit == "S1"
    assert fingerpoint({}).status == HEALTHY and fingerpoint({"S1": tracks(cpu=NORMAL_CPU)}).status == HEALTHY


@pytest.mark.parametrize("blamed, cause", [
    ({"cpu": 1, "cpu_user": 1, "ctx_voluntary_s": 1}, "cpu_task"),
    ({"cpu_user": 1, "io_write_count_s": -1}, "cpu_task"),          # so busy that it serves less
    ({"cpu": 1, "io_write_count_s": 1}, "extra_work"),
    ({"rss_mb": 1}, "memory_growth"),
    ({"cpu": -1}, "unclassified"),
    ({"io_write_count_s": -1}, "unclassified"),
])
def test_classify_matches_the_evidence_to_a_cause(blamed, cause):
    assert classify(blamed) == cause


def test_diagnoser_reports_the_fault_once_and_withdraws_it(signature):
    diagnoses = replay(ordered(recorded()), signature)

    assert [(event["status"], event.get("culprit"), event.get("cause")) for event in diagnoses] == [
        (FAULT, "S2", "cpu_task"), (HEALTHY, None, None)]
    found, cleared = diagnoses
    # Third anomalous sample logs the first anomaly; the eighth logged sample makes it problematic.
    assert found["t"] == 1109.0
    assert found["summary"].startswith("S2: CPU-intensive task inside the process. ")
    assert "CPU 75% (normal 0 to " in found["summary"] and found["summary"].endswith("S1 and S3 normal")
    assert (found["headline"], found["detail"]) == tuple(found["summary"].split(". ", 1))
    assert found["evidence"] == [{"metric": "cpu", "direction": 1, "value": 75.0, "low": 0.0,
                                  "high": pytest.approx(14.0, abs=0.5)}]
    assert "kill it" in found["action"]
    # Withdrawn once the recent values are normal again (4 of the last 7) for 5 seconds in a row.
    assert cleared["t"] == 1147.0


def with_switches(records, hot=None):
    """Add context switches to every server sample: 20 a second, or 300 while ``hot(label, age)``."""

    for record in records:
        if record.get("role") == "server":
            busy = hot is not None and hot(record["label"], record["age"])
            record["m"]["ctx_voluntary_s"] = 300.0 if busy else 20.0
    return records


def test_evidence_fading_at_the_end_of_a_fault_is_not_reported_as_a_new_problem():
    """Context switches stay high five seconds longer than CPU, so for a while they are all that is odd."""

    signature = Signature(train(with_switches(run()), warmup=90.0))
    records = with_switches(recorded(), hot=lambda label, age: label == "S2" and 100 <= age < 145)

    diagnoses = replay(ordered(records), signature)

    assert [(event["status"], event.get("cause")) for event in diagnoses] == [(FAULT, "cpu_task"), (HEALTHY, None)]
    assert [item["metric"] for item in diagnoses[0]["evidence"]] == ["cpu", "ctx_voluntary_s"]


def test_new_evidence_for_the_same_culprit_updates_the_cause(signature):
    """From 120 s the hot replica also writes far more than its peers: it is doing more work."""

    records = recorded(fault=(100, 180))
    for record in records:
        if record.get("role") == "server":
            hot = record["label"] == "S2" and record["age"] >= 120
            record["m"]["io_write_count_s"] = 400.0 if hot else 120.0
    trained = Signature(train([dict(record, m=dict(record["m"], io_write_count_s=120.0))
                               if record.get("role") == "server" else record for record in run()], warmup=90.0))

    diagnoses = replay(ordered(records), trained)

    assert [(event["culprit"], event["cause"]) for event in diagnoses] == [("S2", "cpu_task"), ("S2", "extra_work")]


def test_the_fast_profile_reports_sooner(signature):
    assert replay(ordered(recorded()), signature, FAST)[0]["t"] == 1105.0


def test_fault_free_and_relaunch_runs_lead_to_no_diagnosis(signature):
    relaunch = [record for record in run() if not (record.get("label") == "S2" and record.get("age", 0) >= 120)]
    relaunch += [sample("S2", float(second - 124), cpu=2.0, rss_mb=27.0, threads=6)
                 | {"recv": 1000.0 + second, "incarnation": 2} for second in range(125, 200)]

    assert replay(ordered(run()), signature) == []
    assert replay(ordered(relaunch), signature) == []


def test_a_change_seen_by_every_replica_is_reported_as_workload_not_as_a_fault(signature):
    busy = run(cpu=lambda second: 60.0 if second >= 120 else 2.0)

    diagnoses = replay(ordered(busy), signature)

    assert [event["status"] for event in diagnoses] == [WORKLOAD]
    assert diagnoses[0]["summary"].startswith("all 3 replicas moved together on CPU: ")
    assert diagnoses[0]["headline"] == "Workload change: no replica is blamed"
    assert score(busy, diagnoses)["false_alarms"] == []


def test_a_silent_replica_is_left_out_of_the_comparison(signature):
    """S3 stops reporting at 100 s; from 120 s the other two burn CPU together."""

    records = [record for record in run(cpu=lambda second: 60.0 if second >= 120 else 2.0)
               if not (record.get("label") == "S3" and record.get("age", 0) >= 100)]

    diagnoses = replay(ordered(records), signature)

    assert [event["status"] for event in diagnoses] == [WORKLOAD]
    assert "all 2 replicas" in diagnoses[0]["summary"]


def test_score_measures_delay_and_counts_blame_without_a_fault(signature):
    records = ordered(recorded())
    result = score(records, replay(records, signature))
    wrong = score(records, [{"status": FAULT, "culprit": "S1", "cause": "cpu_task", "t": 1110.0},
                            {"status": FAULT, "culprit": "S2", "cause": "cpu_task", "t": 1190.0}])

    assert [fault["server"] for fault in fault_intervals(records)] == ["S2"]
    assert [(fault["server"], fault["start"], fault["end"], fault["delay"], fault["cause"])
            for fault in result["detected"]] == [("S2", 1100.0, 1140.0, 9.0, "cpu_task")]
    assert result["missed"] == [] and result["false_alarms"] == []
    assert len(wrong["false_alarms"]) == 2 and [fault["server"] for fault in wrong["missed"]] == ["S2"]


def test_a_fault_ended_by_a_kill_or_by_the_end_of_the_run_still_has_an_interval():
    records = [{"t": 10.0, "event": "fault_injected", "server": "S1"},
               {"t": 30.0, "event": "replica_killed", "server": "S1"},
               {"t": 40.0, "event": "fault_injected", "server": "S2"},
               {"type": "sample", "recv": 90.0, "label": "S2", "role": "server", "m": {}}]

    assert [(fault["server"], fault["start"], fault["end"]) for fault in fault_intervals(records)] == [
        ("S1", 10.0, 30.0), ("S2", 40.0, 90.0)]


def test_command_line_replays_a_run_and_scores_it(tmp_path, signature, capsys):
    signature.save(tmp_path / "baseline.json")
    folder = write_run(tmp_path / "faulty", recorded())

    assert diagnose.main(["--baseline", str(tmp_path / "baseline.json"), "--run-dir", str(folder)]) == 0
    printed = capsys.readouterr().out.splitlines()

    assert printed[0] == "+ 101.0 s  done  fault injected into S2"
    assert printed[1].startswith("+ 110.0 s  DIAG  S2: CPU-intensive task inside the process. ")
    assert printed[-2:] == ["detected: the fault in S2 after 9.0 s, as cpu_task", "false alarms: 0"]
