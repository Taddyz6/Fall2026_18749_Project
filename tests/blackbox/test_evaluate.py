import pytest

from ft_system.blackbox.evaluate import (
    accuracy, blamed_spans, by_intensity, fault_trials, median_of, quiet_summary, ready_times, recovery_trials,
)


def fault(server, start, end, duty=0.75):
    return [{"t": start, "event": "fault_injected", "server": server, "fault": {"kind": "cpu", "duty": duty}},
            {"t": end, "event": "fault_cleared", "server": server}]


def blame(t, culprit, cause="cpu_task"):
    return {"t": t, "status": "fault", "culprit": culprit, "cause": cause}


def healthy(t):
    return {"t": t, "status": "healthy"}


END = [{"type": "sample", "recv": 400.0, "label": "S1", "role": "server", "m": {}}]


def test_blamed_spans_run_from_a_diagnosis_to_the_next_one():
    spans = blamed_spans([blame(10.0, "S2"), healthy(50.0), {"t": 60.0, "status": "workload"}, blame(70.0, "S1")], 100.0)

    assert spans == [(10.0, 50.0, "S2"), (70.0, 100.0, "S1")]


def test_each_fault_gets_its_delay_cause_and_how_much_of_it_was_covered():
    records = fault("S2", 100.0, 140.0) + fault("S1", 200.0, 240.0, duty=0.25) + END
    diagnoses = [blame(110.0, "S2"), healthy(150.0)]

    result = fault_trials(records, diagnoses)
    first, second = result["faults"]

    assert (first["server"], first["duty"], first["delay"], first["cause"]) == ("S2", 0.75, 10.0, "cpu_task")
    assert first["covered"] == pytest.approx(0.75)                 # blamed for 30 of its 40 seconds
    assert (second["server"], second["duty"], second["covered"]) == ("S1", 0.25, 0.0) and "delay" not in second
    assert result["false_alarms"] == []
    assert accuracy(result) == {"faults": 2, "detected": 1, "missed": 1, "false_alarms": 0,
                                "precision": 1.0, "recall": 0.5, "f_score": pytest.approx(2 / 3)}


def test_a_blame_with_no_fault_behind_it_is_a_false_alarm():
    records = fault("S2", 100.0, 140.0) + END
    diagnoses = [blame(110.0, "S2"), blame(120.0, "S3"), healthy(150.0), blame(300.0, "S2")]

    result = fault_trials(records, diagnoses)

    assert [(alarm["t"], alarm["culprit"]) for alarm in result["false_alarms"]] == [(120.0, "S3"), (300.0, "S2")]
    assert accuracy(result)["precision"] == pytest.approx(1 / 3) and accuracy(result)["recall"] == 1.0


def test_a_blame_just_after_a_fault_ended_still_belongs_to_it():
    result = fault_trials(fault("S2", 100.0, 105.0) + END, [blame(112.0, "S2")])

    assert result["faults"][0]["delay"] == 12.0 and result["false_alarms"] == []


def test_accuracy_with_nothing_to_score_has_no_ratios():
    assert accuracy({"faults": [], "false_alarms": []}) == {
        "faults": 0, "detected": 0, "missed": 0, "false_alarms": 0, "precision": None, "recall": None,
        "f_score": None}


def test_faults_are_summarised_per_intensity():
    records = (fault("S1", 100.0, 135.0, 0.25) + fault("S2", 200.0, 235.0, 0.25) + fault("S3", 300.0, 335.0, 1.0)
               + END)
    diagnoses = [blame(110.0, "S1"), healthy(140.0), blame(214.0, "S2", "unclassified"), healthy(240.0)]

    rows = by_intensity(fault_trials(records, diagnoses)["faults"])

    assert [(row["duty"], row["faults"], row["detected"], row["delay_median"], row["delay_max"], row["cpu_task"])
            for row in rows] == [(0.25, 2, 2, 12.0, 14.0, 1), (1.0, 1, 0, None, None, 0)]
    assert rows[0]["covered"] == pytest.approx((25 / 35 + 21 / 35) / 2)


def test_quiet_run_counts_blames_per_hour_and_how_fast_load_changes_were_reported():
    records = [{"type": "sample", "recv": 0.0, "label": "S1", "role": "server", "m": {}},
               {"type": "sample", "recv": 1800.0, "label": "S1", "role": "server", "m": {}},
               {"t": 300.0, "event": "replica_killed", "server": "S1"},
               {"t": 600.0, "event": "load_changed", "clients": 6}, {"t": 720.0, "event": "load_changed", "clients": 3},
               {"t": 1200.0, "event": "load_changed", "clients": 6}]
    diagnoses = [{"t": 610.0, "status": "workload"}, healthy(730.0), blame(900.0, "S3")]

    summary = quiet_summary(records, diagnoses)

    assert summary == {"minutes": 30.0, "false_alarms": 1, "false_alarms_per_hour": 2.0, "kills": 1,
                       "load_raised": 2, "load_reported_after": [10.0, None], "healthy_reports": 1}


RM_LOG = """\
[2026-11-20 02:00:00.000] RM: view 6: members: S1, S2, S3; ready: S1, S2, S3
[2026-11-20 02:00:21.500] RM: view 7: members: S1, S3; ready: S1, S3
[2026-11-20 02:00:24.000] RM: view 8: members: S1, S2, S3; ready: S1, S3
[2026-11-20 02:00:26.000] RM: asking S1 to transfer its state to S2
[2026-11-20 02:00:26.200] RM: view 9: members: S1, S2, S3; ready: S1, S2, S3
"""


def test_recovery_trials_time_each_step_from_the_injection():
    from datetime import datetime

    base = datetime(2026, 11, 20, 2, 0, 0).timestamp()
    records = [{"t": base + 5.0, "event": "fault_injected", "server": "S2", "fault": {"duty": 0.75}},
               {"t": base + 20.0, "event": "replica_killed", "server": "S2", "reason": "automatic recovery"},
               {"t": base + 100.0, "event": "fault_injected", "server": "S1", "fault": {"duty": 0.75}}]
    diagnoses = [blame(base + 15.0, "S2"), healthy(base + 28.0)]

    views = ready_times(RM_LOG, ("S1", "S2", "S3"))
    first, second = recovery_trials(records, diagnoses, RM_LOG, ("S1", "S2", "S3"))

    assert [sorted(ready) for _, ready in views] == [["S1", "S2", "S3"], ["S1", "S3"], ["S1", "S3"],
                                                    ["S1", "S2", "S3"]]
    assert (first["server"], first["duty"]) == ("S2", 0.75)
    assert (first["diagnosed"], first["restarted"], first["normal"]) == pytest.approx((10.0, 15.0, 23.0))
    assert first["ready"] == pytest.approx(21.2)             # the first view with all three ready after it was lost
    assert (second["diagnosed"], second["restarted"], second["ready"], second["normal"]) == (None, None, None, None)
    assert median_of([first, second], "ready") == pytest.approx(21.2) and median_of([second], "ready") is None
