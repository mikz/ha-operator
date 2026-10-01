"""Ordered sensor reports and timer lifecycles without HA state or wall clocks."""

import json
from dataclasses import FrozenInstanceError

import pytest

from custom_components.ha_operator.policy_inputs import (
    NumericInput,
    NumericReport,
    NumericState,
    OccurrenceChange,
    TimerEvent,
    TimerInput,
    TimerState,
    numeric_transition,
    recover_numeric,
    recover_timer,
    timer_transition,
)

COLD = NumericInput(16, 1800)
VENTILATION = TimerInput(60, 1800)


def numeric(state=None, value=15, *, now=0):
    state = NumericState() if state is None else state
    return numeric_transition(COLD, state, NumericReport(value), now=now)


def timer(state=None, kind="start", episode_id="first", *, now=0, **kwargs):
    state = TimerState() if state is None else state
    return timer_transition(VENTILATION, state, TimerEvent(kind, episode_id, **kwargs), now=now)


def tick(state, *, now):
    return timer_transition(VENTILATION, state, None, now=now)


def accepted():
    return tick(timer(finish_at=3600).state, now=60)


def test_numeric_exact_qualification_and_unchanged_cold_reports():
    started = numeric(now=0)
    assert started.state.phase == "qualifying"
    assert started.eligible is False
    assert started.next_deadline == 1800
    for time in (0, 30, 1799.999):
        repeated = numeric(started.state, now=time)
        assert repeated == started
        assert numeric(started.state, value=14, now=time) == started
    before = numeric_transition(COLD, started.state, None, now=1799.999)
    assert before.eligible is False
    due = numeric_transition(COLD, started.state, None, now=1800)
    assert due.eligible is True
    assert due.state.phase == "qualified"
    assert due.state.due_at == 1800
    assert due.next_deadline is None
    assert numeric(due.state, now=9000) == due


def test_warm_transition_resets_cold_continuity_in_one_event_loop_turn():
    started = numeric(now=0)
    warm = numeric(started.state, 16, now=1799)
    assert warm.state.due_at is None
    assert warm.eligible is False
    cold = numeric(warm.state, 15, now=1799)
    assert cold.state != started.state
    assert cold.next_deadline == 3599
    assert numeric_transition(COLD, cold.state, None, now=1800).eligible is False
    assert numeric_transition(COLD, cold.state, None, now=3599).eligible is True
    assert numeric(numeric(now=0).state, 17, now=1799).state == warm.state


def test_unknown_pending_preserves_deadline_and_waits_for_fresh_cold():
    started = numeric(now=0)
    unknown = numeric(started.state, None, now=1200)
    assert unknown.state.due_at == 1800
    assert unknown.eligible is False
    assert unknown.next_deadline is None
    assert numeric_transition(COLD, unknown.state, None, now=2100) == unknown
    assert numeric(unknown.state, None, now=2100) == unknown
    cold = numeric(unknown.state, 15, now=2100)
    assert cold.state.due_at == 1800
    assert cold.eligible is True
    assert cold.next_deadline is None


def test_unknown_qualified_latches_eligibility_until_warm():
    qualified = numeric(numeric().state, now=1800)
    unknown = numeric(qualified.state, None, now=2100)
    assert unknown.eligible is True
    assert unknown.state.phase == "qualified"
    assert unknown.state.source_quality == "unknown"
    assert unknown.state.due_at == 1800
    assert numeric_transition(COLD, unknown.state, None, now=9000) == unknown
    assert numeric(unknown.state, 16, now=9000).state == NumericState(source_quality="numeric")


@pytest.mark.parametrize("qualified", [False, True])
def test_numeric_restart_retains_deadline_but_requires_fresh_finite_evidence(qualified):
    saved = NumericState(1800, qualified, "numeric")
    restored = recover_numeric(saved)
    assert restored.phase == "recovering"
    waiting = numeric_transition(COLD, restored, None, now=2100)
    assert waiting.eligible is None
    assert waiting.next_deadline is None
    assert waiting.state.due_at == 1800
    assert numeric(restored, None, now=2100) == waiting
    cold = numeric(restored, 15, now=2100)
    assert cold.eligible is True
    assert cold.state.due_at == 1800
    warm = numeric(restored, 16, now=2100)
    assert warm.eligible is False
    assert warm.state.due_at is None


def test_numeric_restart_before_due_does_not_renew_deadline():
    restored = recover_numeric(numeric().state)
    fresh = numeric(restored, now=600)
    assert fresh.next_deadline == 1800
    assert fresh.eligible is False
    assert numeric_transition(COLD, fresh.state, None, now=1800).eligible is True


def test_timer_start_qualifies_once_with_absolute_expiry():
    started = timer(finish_at=3600)
    assert started.state.phase == "qualifying"
    assert started.state.expires_at == 1860
    assert started.next_deadline == 60
    assert started.occurrence_changes == ()
    assert timer(started.state, now=30, finish_at=3600) == started
    assert tick(started.state, now=59.999) == started
    admitted = tick(started.state, now=60)
    assert admitted.state.phase == "accepted"
    assert admitted.occurrence_changes == (OccurrenceChange("admit", "first", 1860),)
    assert admitted.next_deadline == 1860
    assert tick(admitted.state, now=60).occurrence_changes == ()
    assert timer(admitted.state, now=1000).occurrence_changes == ()


@pytest.mark.parametrize("expired_at", [1860, 2000])
def test_late_qualification_never_extends_request_or_admits_at_expiry(expired_at):
    started = timer().state
    late = tick(started, now=1500)
    assert late.occurrence_changes == (OccurrenceChange("admit", "first", 1860),)
    expired = tick(started, now=expired_at)
    assert expired.state.phase == "expired"
    assert expired.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    assert expired.next_deadline is None


def test_accepted_request_expires_exactly_once_and_cannot_replay():
    admitted = accepted()
    assert tick(admitted.state, now=1859.999).state == admitted.state
    expired = tick(admitted.state, now=1860)
    assert expired.state.phase == "expired"
    assert expired.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    assert tick(expired.state, now=2000).occurrence_changes == ()
    assert timer(expired.state, now=2000).state == expired.state
    assert timer(expired.state, now=2000).occurrence_changes == ()


@pytest.mark.parametrize("prior", ["pending", "accepted"])
def test_active_restart_replaces_episode_even_with_identical_finish_time(prior):
    state = timer(finish_at=3600).state if prior == "pending" else accepted().state
    restarted = timer(state, episode_id="second", now=100, finish_at=3600)
    assert restarted.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    assert restarted.state.episode_id == "second"
    assert restarted.next_deadline == 160
    assert restarted.state.expires_at == 1960
    assert timer(restarted.state, episode_id="second", now=120, finish_at=3600) == (
        timer(restarted.state, episode_id="second", now=100, finish_at=3600)
    )
    assert timer(restarted.state, "cancel", "first", now=120).state == restarted.state
    assert tick(restarted.state, now=160).occurrence_changes == (
        OccurrenceChange("admit", "second", 1960),
    )


@pytest.mark.parametrize("kind", ["pause", "cancel", "suppress"])
@pytest.mark.parametrize("prior", ["pending", "accepted"])
def test_pause_cancel_and_runtime_rejection_dead_end_episode(kind, prior):
    state = timer().state if prior == "pending" else accepted().state
    suppressed = timer(state, kind, now=60)
    assert suppressed.state.phase == "suppressed"
    assert suppressed.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    assert suppressed.next_deadline is None
    assert timer(suppressed.state, kind, now=120).occurrence_changes == ()
    assert tick(suppressed.state, now=120).state == suppressed.state
    assert timer(suppressed.state, now=120).state == suppressed.state
    assert timer(suppressed.state, "resume", now=120).state == suppressed.state
    # A subsequent fresh start, including an active restart after resume, can qualify.
    restarted = timer(suppressed.state, episode_id="second", now=130)
    assert restarted.next_deadline == 190
    assert tick(restarted.state, now=190).occurrence_changes[0].action == "admit"


def test_cancel_at_qualification_deadline_prevents_admission():
    cancelled = timer(timer().state, "cancel", now=60)
    assert [change.action for change in cancelled.occurrence_changes] == ["suppress"]


def test_finish_before_admission_suppresses_but_accepted_finish_keeps_expiry():
    pending = timer(timer().state, "finish", now=60)
    assert pending.state.phase == "suppressed"
    assert pending.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    finished = timer(accepted().state, "finish", now=120)
    assert finished.state == accepted().state
    assert finished.occurrence_changes == ()
    assert finished.next_deadline == 1860
    assert tick(finished.state, now=1860).state.phase == "expired"


def test_finish_captured_before_admission_rejects_later_request():
    finished = timer(accepted().state, "finish", now=60, accepted_at_capture=False)
    assert finished.state.phase == "suppressed"
    assert finished.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    committed = timer(accepted().state, "finish", now=60, accepted_at_capture=True)
    assert committed.state == accepted().state
    assert committed.occurrence_changes == ()


def test_changed_finish_invalidates_pending_without_renewing_accepted_request():
    started = timer(finish_at=3600)
    unchanged = timer(started.state, "change", now=30, finish_at=3600)
    assert unchanged == started
    changed = timer(started.state, "change", now=60, finish_at=7200)
    assert changed.state.phase == "suppressed"
    assert changed.state.expires_at == 1860
    assert changed.next_deadline is None
    assert changed.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    admitted_change = timer(accepted().state, "change", now=120, finish_at=7200)
    assert admitted_change.state.phase == "accepted"
    assert admitted_change.next_deadline == 1860
    assert admitted_change.occurrence_changes == ()
    assert timer(admitted_change.state, "change", now=180, finish_at=7200).state == (
        admitted_change.state
    )


def test_changed_finish_captured_during_admission_save_invalidates_published_request():
    changed = timer(accepted().state, "change", now=60, finish_at=7200, accepted_at_capture=False)
    assert changed.state.phase == "suppressed"
    assert changed.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    assert changed.next_deadline is None
    unchanged = timer(accepted().state, "change", now=60, finish_at=3600, accepted_at_capture=False)
    assert unchanged.state == accepted().state
    assert unchanged.occurrence_changes == ()


def test_restored_pending_timer_cannot_admit_but_accepted_keeps_original_expiry():
    pending = recover_timer(timer().state, now=30)
    assert pending.state.phase == "suppressed"
    assert tick(pending.state, now=60).occurrence_changes == ()
    assert timer(pending.state, now=90).state == pending.state
    restored = recover_timer(accepted().state, now=120)
    assert restored.state == accepted().state
    assert restored.occurrence_changes == ()
    assert restored.next_deadline == 1860
    expired = recover_timer(accepted().state, now=1860)
    assert expired.state.phase == "expired"
    assert expired.occurrence_changes == (OccurrenceChange("suppress", "first", 1860),)
    assert recover_timer(expired.state, now=2000).occurrence_changes == ()
    assert recover_timer(TimerState(), now=30).state == TimerState()


@pytest.mark.parametrize("state", [numeric().state, accepted().state, TimerState()])
def test_saved_states_are_frozen_json_records(state):
    record = state.to_record()
    restored = type(state).from_record(json.loads(json.dumps(record)))
    assert restored == state
    record["unexpected"] = True
    with pytest.raises(TypeError):
        type(state).from_record(record)
    with pytest.raises(FrozenInstanceError):
        state.due_at = 999


@pytest.mark.parametrize(
    "make",
    [
        lambda: NumericInput(float("nan"), 1800),
        lambda: NumericInput(16, 0),
        lambda: NumericInput(16, -1),
        lambda: NumericReport(float("inf")),
        lambda: NumericReport(True),
        lambda: NumericState(float("nan")),
        lambda: NumericState(qualified=True),
        lambda: NumericState(qualified=1),
        lambda: NumericState(recovery_pending=1),
        lambda: NumericState(source_quality="bad"),
        lambda: TimerInput(0, 1800),
        lambda: TimerInput(60, -1),
        lambda: TimerEvent("unknown", "episode"),
        lambda: TimerEvent("start", ""),
        lambda: TimerEvent("start", "episode", finish_at=float("nan")),
        lambda: TimerEvent("finish", "episode", accepted_at_capture=1),
        lambda: TimerState(phase="unknown"),
        lambda: TimerState("episode"),
        lambda: TimerState("episode", "qualifying", 60),
        lambda: TimerState("episode", "qualifying", 60, 60),
        lambda: TimerState("episode", "accepted", 60, 1860),
        lambda: TimerState(None, "accepted", expires_at=1860),
        lambda: TimerState("episode", "accepted", expires_at=float("inf")),
        lambda: numeric(now=float("nan")),
        lambda: timer(now=True),
        lambda: recover_timer(TimerState(), now=float("inf")),
    ],
)
def test_models_reject_invalid_values(make):
    with pytest.raises(ValueError):
        make()
