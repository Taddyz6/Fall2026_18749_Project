import pytest

from ft_system.server.state_machine import (
    DeterministicStateMachine,
    StateMachineError,
)


def test_initial_state_has_one_zero_counter_per_client():
    machine = DeterministicStateMachine(("C1", "C2", "C3"))

    assert machine.snapshot() == {"C1": 0, "C2": 0, "C3": 0}


def test_increment_changes_only_requesting_client_slot():
    machine = DeterministicStateMachine(("C1", "C2", "C3"))

    transition = machine.apply("C1", "increment")

    assert transition.before == {"C1": 0, "C2": 0, "C3": 0}
    assert transition.after == {"C1": 1, "C2": 0, "C3": 0}
    assert transition.client_value == 1


def test_independent_client_operations_converge_regardless_of_order():
    first = DeterministicStateMachine(("C1", "C2", "C3"))
    second = DeterministicStateMachine(("C1", "C2", "C3"))

    first.apply("C1", "increment")
    first.apply("C2", "increment")
    second.apply("C2", "increment")
    second.apply("C1", "increment")

    assert first.snapshot() == second.snapshot() == {"C1": 1, "C2": 1, "C3": 0}


def test_unsupported_operation_does_not_mutate_state():
    machine = DeterministicStateMachine(("C1", "C2", "C3"))

    with pytest.raises(StateMachineError, match="unsupported operation"):
        machine.apply("C1", "decrement")

    assert machine.snapshot() == {"C1": 0, "C2": 0, "C3": 0}


def test_unknown_client_does_not_mutate_state():
    machine = DeterministicStateMachine(("C1", "C2", "C3"))

    with pytest.raises(StateMachineError, match="unknown client"):
        machine.apply("C9", "increment")

    assert machine.snapshot() == {"C1": 0, "C2": 0, "C3": 0}


def test_returned_snapshots_do_not_alias_internal_state():
    machine = DeterministicStateMachine(("C1", "C2", "C3"))
    snapshot = machine.snapshot()
    transition = machine.apply("C1", "increment")

    snapshot["C1"] = 100
    transition.after["C1"] = 200

    assert machine.snapshot() == {"C1": 1, "C2": 0, "C3": 0}


@pytest.mark.parametrize("client_ids", [(), ("",), ("C1", "C1")])
def test_rejects_invalid_client_id_collection(client_ids):
    with pytest.raises(StateMachineError):
        DeterministicStateMachine(client_ids)
