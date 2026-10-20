"""Validate native Store data while pruning only obsolete detector records."""

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
    "key,value",
    [
        ("modes", {"roof": "active"}),
        ("policy_enabled", {"policy": "on"}),
        ("intents", {"sleep": 1}),
        ("return_monitors", {"roof": []}),
        ("return_monitors", {"roof": {"fingerprint": "invalid", "state": {}}}),
        ("return_monitors", {"roof": {"fingerprint": "a" * 64, "state": []}}),
        ("return_monitors", {"roof": {"fingerprint": "a" * 64, "state": {"due_at": "later"}}}),
    ],
)
def test_invalid_runtime_maps_do_not_mutate_input(key, value):
    state = empty_state()
    state[key] = value
    before = deepcopy(state)
    with pytest.raises(InvalidSnapshot):
        validate_state(state)
    assert state == before


def test_prunes_only_obsolete_detector_records_and_keeps_unrelated_intent():
    state = empty_state()
    state["policy_inputs"] = {"obsolete": {"state": {"due_at": 123}}}
    state["manuals"]["roof"] = {"mode": "hands_off", "expires_at": 600}
    state["return_monitors"]["roof"] = {
        "fingerprint": "b" * 64,
        "state": {"target_position": 7, "due_at": 300, "overdue": False},
    }
    before = deepcopy(state)
    restored = validate_state(state)
    assert "policy_inputs" not in restored
    assert restored["manuals"] == before["manuals"]
    assert restored["return_monitors"] == before["return_monitors"]
    assert state == before
