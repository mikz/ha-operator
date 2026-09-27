"""Temporal and permutation properties independent of Home Assistant."""

from hypothesis import given
from hypothesis import strategies as st

from custom_components.ha_operator.core import (
    ManualLease,
    Observation,
    Occurrence,
    Policy,
    Resource,
    Target,
    evaluate,
)

finite_time = st.floats(min_value=0, max_value=1e9, allow_nan=False, allow_infinity=False)
position = st.integers(min_value=0, max_value=100)


def decide(now, **kwargs):
    return evaluate(
        now=now,
        resources={"r": Resource("r", mode="live")},
        observations={"r": Observation(Target(position=0), True, reported_at=now)},
        **kwargs,
    )


@given(expiry=finite_time, elapsed=st.floats(min_value=0, max_value=1e7), target=position)
def test_no_expired_manual_target_dispatches(expiry, elapsed, target):
    result = decide(
        expiry + elapsed,
        manuals=[ManualLease("r", "target", Target(position=target), expires_at=expiry)],
    )
    assert result.decisions["r"].target is None
    assert result.decisions["r"].status == "idle"
    assert result.next_evaluation is None


@given(positions=st.lists(position, min_size=1, max_size=30), expiry=st.integers(31, 10000))
def test_telemetry_cannot_renew_lease(positions, expiry):
    lease = ManualLease("r", "target", Target(position=100), expires_at=expiry)
    for now, observed in enumerate(positions):
        result = evaluate(
            now=now,
            resources={"r": Resource("r", mode="live")},
            observations={"r": Observation(Target(position=observed), True, reported_at=now)},
            manuals=[lease],
        )
        assert result.next_evaluation == expiry
        assert lease.expires_at == expiry
    assert decide(expiry, manuals=[lease]).decisions["r"].target is None


@given(
    priorities=st.lists(st.integers(-10000, 10000), min_size=1, max_size=30),
    target=position,
)
def test_policy_order_cannot_change_result(priorities, target):
    policies = [
        Policy(f"p{i:02d}", "r", Target(position=target), priority=p)
        for i, p in enumerate(priorities)
    ]
    assert (
        decide(0, policies=policies).decisions == decide(0, policies=reversed(policies)).decisions
    )


@given(now=finite_time, target=position)
def test_tombstone_prevents_replay_regardless_admission_order(now, target):
    policy = Policy("p", "r", Target(position=target), kind="occurrence")
    admitted = Occurrence("p", "stable-utc-id", now + 100)
    tombstone = Occurrence("p", "stable-utc-id", now + 100, skipped=True)
    for records in ([admitted, tombstone], [tombstone, admitted]):
        result = decide(now, policies=[policy], occurrences=records)
        assert result.decisions["r"].status == "idle"


@given(old=position, new=position, times=st.lists(finite_time, min_size=1, max_size=20))
def test_replacing_intent_never_resurrects_superseded_target(old, new, times):
    old_lease = ManualLease("r", "target", Target(position=old), request_id="old")
    new_lease = ManualLease("r", "target", Target(position=new), request_id="new")
    decide(0, manuals=[old_lease])
    for now in sorted(times):
        decision = decide(now, manuals=[new_lease]).decisions["r"]
        assert decision.target == Target(position=new)
