"""Validate native Store data without implementing a persistence protocol."""

from copy import deepcopy

import pytest

from custom_components.ha_operator.storage import InvalidSnapshot, empty_state, validate_state


def test_empty_state_has_runtime_maps_without_commit_revision():
    state = empty_state()
    assert set(state) == {
        "manuals",
        "occurrences",
        "modes",
        "policy_enabled",
        "requests",
        "intents",
        "policy_inputs",
        "return_monitors",
    }
    assert validate_state(state) == state
    state["intents"]["sleep"] = True
    assert empty_state()["intents"] == {}


@pytest.mark.parametrize("value", [None, [], "invalid", {}, {"manuals": []}])
def test_unusable_restored_data_is_rejected(value):
    with pytest.raises(InvalidSnapshot):
        validate_state(value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("modes", {"roof": "active"}),
        ("policy_enabled", {"policy": "on"}),
        ("intents", {"sleep": 1}),
        ("policy_inputs", {"cold": {"type": "unknown", "fingerprint": "a" * 64, "state": {}}}),
        (
            "policy_inputs",
            {"cold": {"type": "qualified_numeric", "fingerprint": "invalid", "state": {}}},
        ),
        ("return_monitors", {"roof": {"fingerprint": "a" * 64, "state": {"due_at": "later"}}}),
        ("return_monitors", {"roof": {"fingerprint": "invalid", "state": {}}}),
    ],
)
def test_invalid_runtime_maps_do_not_mutate_input(key, value):
    state = empty_state()
    state[key] = value
    before = deepcopy(state)
    with pytest.raises(InvalidSnapshot):
        validate_state(state)
    assert state.keys() == before.keys()


def test_valid_typed_input_and_return_deadlines_are_preserved():
    state = empty_state()
    state["policy_inputs"]["cold"] = {
        "type": "qualified_numeric",
        "fingerprint": "a" * 64,
        "state": {
            "due_at": 200,
            "qualified": False,
            "source_quality": "numeric",
            "recovery_pending": False,
        },
    }
    state["return_monitors"]["roof"] = {
        "fingerprint": "b" * 64,
        "state": {"target_position": 7, "due_at": 300, "overdue": False},
    }
    assert validate_state(state) == state
    assert state["policy_inputs"]["cold"]["state"]["due_at"] == 200
    assert state["return_monitors"]["roof"]["state"]["due_at"] == 300


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("return_monitors", {"roof": []}),
        ("return_monitors", {"roof": {"fingerprint": "a" * 64, "state": []}}),
        ("policy_inputs", {"cold": []}),
        ("policy_inputs", {"cold": {"type": 1, "fingerprint": "a" * 64, "state": {}}}),
        (
            "policy_inputs",
            {
                "cold": {
                    "type": "qualified_numeric",
                    "fingerprint": "a" * 64,
                    "state": {"due_at": "later"},
                }
            },
        ),
    ],
)
def test_unusable_nested_restoration_is_rejected(key, value):
    state = empty_state()
    state[key] = value
    with pytest.raises(InvalidSnapshot):
        validate_state(state)
