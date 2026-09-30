"""Semantic return episodes remain independent of clocks, HA and commands."""

import json
from dataclasses import FrozenInstanceError

import pytest

from custom_components.ha_operator.return_monitor import (
    ReturnMonitorInput,
    ReturnMonitorState,
    return_transition,
)

CONFIG = ReturnMonitorInput(7, 300, 2)


def run(state=None, *, target=7, observed=100, active=True, now=0):
    return return_transition(
        CONFIG,
        state or ReturnMonitorState(),
        target_position=target,
        observed_position=observed,
        active=active,
        now=now,
    )


def test_refusal_and_repeated_telemetry_keep_original_exact_deadline():
    started = run()
    assert started.state.phase == "waiting"
    assert started.next_deadline == 300
    for now in (0, 100, 299.999):
        assert run(started.state, now=now) == started
        assert run(started.state, observed=None, now=now) == started
    due = run(started.state, now=300)
    assert due.state.overdue
    assert due.state.phase == "overdue"
    assert due.state.due_at == 300
    assert due.next_deadline is None
    assert run(due.state, now=900) == due
    assert run(due.state, observed=None, now=900) == due


@pytest.mark.parametrize("observed", [5, 7, 9])
def test_raw_feedback_within_existing_tolerance_confirms(observed):
    assert run(observed=observed).state == ReturnMonitorState()
    cleared = run(run().state, observed=observed, now=300)
    assert cleared.state.phase == "idle"
    assert cleared.next_deadline is None
    assert run(run(run().state, now=300).state, observed=observed, now=600).state == (
        ReturnMonitorState()
    )


@pytest.mark.parametrize("observed", [4.99, 9.01, None])
def test_mismatch_or_unknown_cannot_confirm(observed):
    assert run(run().state, observed=observed, now=300).state.overdue


def test_relevant_target_change_starts_new_period_without_source_identity():
    prior = run(run().state, now=300).state
    changed = run(prior, target=0, now=400)
    assert changed.state.target_position == 0
    assert changed.next_deadline == 700
    assert not changed.state.overdue
    assert run(changed.state, target=0, now=600) == changed
    assert run(changed.state, target=0, now=700).state.overdue


@pytest.mark.parametrize("target,active", [(None, True), (8, True), (7, False)])
def test_idle_outside_return_range_or_inactive_ends_monitor(target, active):
    for prior in (run().state, run(run().state, now=300).state):
        assert run(prior, target=target, active=active, now=400).state == ReturnMonitorState()


def test_restart_record_retains_deadline_and_overdue_transition():
    prior = run().state
    recovered = ReturnMonitorState.from_record(json.loads(json.dumps(prior.to_record())))
    assert run(recovered, now=200).next_deadline == 300
    assert run(recovered, observed=None, now=500).state.overdue
    warning = run(recovered, now=500).state
    assert ReturnMonitorState.from_record(warning.to_record()) == warning
    assert ReturnMonitorState.from_record(ReturnMonitorState().to_record()).phase == "idle"
    with pytest.raises(FrozenInstanceError):
        recovered.due_at = 900


@pytest.mark.parametrize(
    "make",
    [
        lambda: ReturnMonitorInput(-1, 300, 2),
        lambda: ReturnMonitorInput(101, 300, 2),
        lambda: ReturnMonitorInput(True, 300, 2),
        lambda: ReturnMonitorInput(7, 0, 2),
        lambda: ReturnMonitorInput(7, -1, 2),
        lambda: ReturnMonitorInput(7, 300, -1),
        lambda: ReturnMonitorInput(7, float("inf"), 2),
        lambda: ReturnMonitorState(7),
        lambda: ReturnMonitorState(due_at=300),
        lambda: ReturnMonitorState(overdue=True),
        lambda: ReturnMonitorState(overdue=1),
        lambda: ReturnMonitorState(7, float("nan")),
        lambda: ReturnMonitorState.from_record({}),
        lambda: ReturnMonitorState.from_record(
            {"target_position": None, "due_at": None, "overdue": False, "extra": True}
        ),
        lambda: run(active=1),
        lambda: run(target=float("nan")),
        lambda: run(observed=True),
        lambda: run(now=float("inf")),
    ],
)
def test_invalid_values_are_rejected(make):
    with pytest.raises(ValueError):
        make()
