import pytest

from ft_system.rm.policy import ReplicationPolicy


def test_active_first_replica_becomes_ready_alone_then_seeds_later_joiners():
    policy = ReplicationPolicy("active")

    assert policy.set_members(["S1"])
    assert policy.assignments() == {"S1": None}
    policy.set_members(["S1", "S2"])
    # S2 must wait for a ready donor instead of also starting from empty state.
    assert policy.assignments() == {"S1": None}

    assert policy.set_status("S1", True)
    assert policy.assignments() == {"S2": "S1"}
    policy.set_members(["S1", "S2", "S3"])
    assert policy.assignments() == {"S2": "S1", "S3": "S1"}


def test_active_relaunch_gets_state_from_oldest_ready_replica():
    policy = ReplicationPolicy("active")
    policy.set_members(["S1", "S2", "S3"])
    for replica in ("S1", "S2", "S3"):
        policy.set_status(replica, True)
    assert policy.assignments() == {}

    policy.set_members(["S1", "S3"])
    policy.set_members(["S1", "S2", "S3"])

    assert policy.view()["ready"] == ["S1", "S3"]
    assert policy.assignments() == {"S2": "S1"}
    policy.set_members(["S2", "S3"])  # The donor fails mid-transfer.
    assert policy.assignments() == {"S2": "S3"}


def test_last_resort_when_no_ready_replica_remains():
    policy = ReplicationPolicy("active")
    policy.set_members(["S1", "S2"])
    policy.set_status("S1", True)
    policy.set_members(["S2"])

    assert policy.ever_ready
    assert policy.assignments() == {"S2": None}


def test_passive_first_member_is_primary_and_backups_wait_for_checkpoints():
    policy = ReplicationPolicy("passive")
    policy.set_members(["S1"])
    policy.set_members(["S1", "S2", "S3"])

    assert policy.primary == "S1"
    assert policy.assignments() == {"S1": None}
    policy.set_status("S1", True)
    assert policy.assignments() == {}


def test_passive_primary_failure_elects_the_oldest_ready_backup():
    policy = ReplicationPolicy("passive")
    policy.set_members(["S2"])
    policy.set_members(["S1", "S2", "S3"])  # S1 joined after S2.
    policy.set_status("S2", True)
    policy.set_status("S3", True)

    assert policy.primary == "S2"
    policy.set_members(["S1", "S3"])

    # S1 joined before S3 but holds no checkpoint, so it cannot take over.
    assert policy.primary == "S3"
    policy.set_members(["S1", "S2", "S3"])
    assert policy.primary == "S3"  # The relaunched replica returns as a backup.


def test_passive_unready_primary_yields_to_ready_backup():
    policy = ReplicationPolicy("passive")
    policy.set_members(["S1", "S2"])
    policy.set_status("S1", True)
    policy.set_status("S2", True)
    policy.set_status("S1", False)

    assert policy.primary == "S2"


def test_view_number_changes_only_with_the_view():
    policy = ReplicationPolicy("active")
    policy.set_members(["S1"])
    first = policy.view_num

    assert not policy.set_members(["S1"])
    assert not policy.set_status("S9", True)
    assert policy.view_num == first
    assert policy.set_status("S1", True)
    assert policy.view() == {"view_num": first + 1, "mode": "active",
                             "members": ["S1"], "ready": ["S1"], "primary": None}


def test_joiner_that_reports_ready_keeps_its_state():
    policy = ReplicationPolicy("active")
    policy.set_members(["S1"], {"S1": True})

    assert policy.ready == {"S1"}
    assert policy.assignments() == {}


def test_rejects_unknown_mode():
    with pytest.raises(ValueError):
        ReplicationPolicy("hot")
