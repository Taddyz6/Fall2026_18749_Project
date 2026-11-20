import pytest

from ft_system.blackbox.recover import RecoveryPolicy

SERVERS = ("S1", "S2", "S3")
FAULT = {"state": "fault", "culprit": "S2", "cause": "cpu_task", "since": 100.0}


def test_a_standing_curable_diagnosis_with_every_replica_ready_is_acted_on():
    assert RecoveryPolicy().decide(106.0, FAULT, SERVERS, SERVERS) == ("S2", "S2 has been diagnosed for 6 s")


@pytest.mark.parametrize("status, now, ready, reason", [
    ({"state": "healthy"}, 200.0, SERVERS, "nothing is diagnosed"),
    ({"state": "workload"}, 200.0, SERVERS, "nothing is diagnosed"),
    (FAULT | {"cause": "extra_work"}, 200.0, SERVERS, "a restart does not cure this kind of problem (extra_work)"),
    (FAULT | {"cause": "unclassified"}, 200.0, SERVERS, "a restart does not cure this kind of problem (unclassified)"),
    (FAULT, 103.0, SERVERS, "the diagnosis is 3 s old; waiting for 5 s"),
    (FAULT, 200.0, ("S1", "S2"), "not every replica is ready"),
])
def test_recovery_is_held_back(status, now, ready, reason):
    assert RecoveryPolicy().decide(now, status, ready, SERVERS) == (None, reason)


def test_restarts_are_at_least_a_minute_apart_whoever_asked_for_them():
    policy = RecoveryPolicy()
    policy.restarted(150.0)

    assert policy.decide(180.0, FAULT, SERVERS, SERVERS) == (
        None, "a replica was restarted 30 s ago; waiting 60 s between restarts")
    assert policy.decide(210.0, FAULT, SERVERS, SERVERS)[0] == "S2"


def test_memory_growth_is_also_cured_by_a_restart():
    assert RecoveryPolicy().decide(200.0, FAULT | {"cause": "memory_growth"}, SERVERS, SERVERS)[0] == "S2"
