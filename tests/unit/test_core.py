"""Behavioral tests of the I/O-free operator engine."""

from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

import pytest

from custom_components.ha_operator.core import (
    Evidence,
    ManualLease,
    Observation,
    Occurrence,
    Policy,
    Provider,
    Requirement,
    RequirementMemory,
    Resource,
    Target,
    TargetValidationError,
    evaluate,
    evaluate_evidence,
    matches,
)

OPEN = Target(position=100)
CLOSED = Target(position=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("position", 10**399), ("percentage", 10**399), ("percentage", -(10**399))],
)
def test_oversized_target_numbers_use_existing_semantic_validation(field, value):
    with pytest.raises(TargetValidationError) as raised:
        Target.from_dict({field: value})
    assert raised.value.translation_key == "finite_number"
    assert raised.value.translation_placeholders == {"field": field}


def run(*, now=10, **kwargs):
    kwargs.setdefault("resources", {"window": Resource("window", mode="live")})
    kwargs.setdefault("observations", {"window": Observation(CLOSED, True, reported_at=now)})
    return evaluate(now=now, **kwargs)


def provider(id="window", **kwargs):
    return Provider(id, resource_id=id, target=OPEN, **kwargs)


def airflow(providers, **kwargs):
    return Requirement(
        "air", True, tuple(providers), acquisition_timeout=20, retry_interval=50, **kwargs
    )


def airflow_run(providers, *, previous=None, **kwargs):
    kwargs.setdefault(
        "resources", {name: Resource(name, mode="live") for name in ("window", "inlet")}
    )
    kwargs.setdefault(
        "observations", {name: Observation(CLOSED, True) for name in ("window", "inlet")}
    )
    return run(
        requirements=(airflow(providers),),
        requirement_memory={"air": previous} if previous else {},
        **kwargs,
    )


def test_target_roundtrip_is_frozen_and_omits_none():
    target = Target(position=0, on=False, percentage=0, direction="forward", profile="off")
    assert Target.from_dict(target.to_dict()) == target
    assert Target().to_dict() == {}
    with pytest.raises(FrozenInstanceError):
        target.position = 40
    with pytest.raises(TypeError):
        Target.from_dict({"postion": 42})


@pytest.mark.parametrize(
    "field,value",
    [
        ("position", -1),
        ("percentage", 101),
        ("position", float("nan")),
        ("position", float("inf")),
        ("percentage", True),
        ("on", 1),
        ("direction", ""),
        ("profile", " "),
        ("direction", False),
        ("position", "10"),
    ],
)
def test_invalid_target(field, value):
    with pytest.raises(ValueError):
        Target(**{field: value})


@pytest.mark.parametrize(
    "make",
    [
        lambda: Resource("x", mode="invalid"),
        lambda: Resource("x", tolerance=-1),
        lambda: ManualLease("x", "wrong"),
        lambda: ManualLease("x", "target"),
        lambda: ManualLease("x", "target", Target()),
        lambda: ManualLease("x", "hands_off", OPEN),
        lambda: ManualLease("x", "hands_off", expires_at=float("nan")),
        lambda: Policy("p", "x", OPEN, kind="invalid"),
        lambda: Occurrence("p", "o", float("nan")),
        lambda: Requirement("r", True, (), acquisition_timeout=0),
        lambda: Requirement("r", True, (), retry_interval=-1),
        lambda: Requirement("r", True, (Provider("p"), Provider("p"))),
    ],
)
def test_invalid_models(make):
    with pytest.raises(ValueError):
        make()


def test_observed_matching_respects_all_fields_and_availability():
    assert matches(Target(position=50), Target(position=48))
    assert not matches(Target(position=50), Target(position=47.99))
    assert not matches(OPEN, None)
    assert not matches(OPEN, Target())
    assert not matches(OPEN, Observation(OPEN, False))
    assert not matches(Target(), OPEN)
    assert matches(Target(on=False, direction="reverse"), Target(on=False, direction="reverse"))
    assert not matches(Target(on=True), Target(on=False))


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ([], None),
        ([Evidence(15, 10, "gte")], True),
        ([Evidence(5, 10, "lte")], True),
        ([Evidence(15, 10, "lte")], False),
        ([Evidence("on", "on", kind="contact")], True),
        ([Evidence("off", "on", kind="relay")], False),
        ([Evidence(None, 10, "gte")], None),
        ([Evidence("unknown", 10, "gte")], None),
        ([Evidence("unavailable", 10, "gte")], None),
        ([Evidence(15, 10, "gte", available=False)], None),
        ([Evidence(15, 10, "gte", fresh=False)], None),
        ([Evidence(True, 10, "gte")], None),
        ([Evidence(10, False, "lte")], None),
        ([Evidence("broken", 10, "gte")], None),
        ([Evidence({}, 10, "gte")], None),
        ([Evidence(float("nan"), 10, "gte")], None),
        ([Evidence(10, float("inf"), "gte")], None),
        ([Evidence(10, 10, "gte"), Evidence(None, "on")], None),
        ([Evidence(None, "on"), Evidence(1, 10, "gte")], False),
    ],
)
def test_evidence(evidence, expected):
    assert evaluate_evidence(evidence) is expected


def test_evidence_rejects_unknown_operator():
    with pytest.raises(ValueError):
        evaluate_evidence([Evidence(1, 2, "contains")])


def test_no_request_is_idle_and_does_not_invent_target():
    result = run()
    assert result.decisions["window"].status == "idle"
    assert result.decisions["window"].target is None
    assert result.next_evaluation is None
    assert isinstance(result.decisions, MappingProxyType)
    with pytest.raises(TypeError):
        result.decisions["x"] = result.decisions["window"]


def test_fault_is_visible_even_without_admitted_intent_but_hands_off_remains_owned():
    resources = {"window": Resource("window", mode="live", fault="storage_error")}
    decision = run(resources=resources).decisions["window"]
    assert decision.status == "fault"
    assert decision.reason == "storage_error"
    assert decision.target is None
    assert (
        run(resources=resources, manuals=[ManualLease("window", "hands_off")])
        .decisions["window"]
        .status
        == "hands_off"
    )


def test_manual_beats_provider_beats_arbitrarily_high_policy():
    policy = Policy("day", "window", CLOSED, priority=100_000_000)
    requirement = airflow([provider()])
    result = run(policies=[policy], requirements=[requirement])
    assert result.decisions["window"].target == OPEN
    assert result.decisions["window"].source == "requirement:air:window"
    result = run(
        policies=[policy],
        requirements=[requirement],
        manuals=[ManualLease("window", "target", Target(position=25), expires_at=20)],
    )
    decision = result.decisions["window"]
    assert decision.target == Target(position=25)
    assert decision.source == "manual:service"
    assert decision.candidates[-1].reason == "superseded by higher precedence request"
    assert result.next_evaluation == 20


def test_stable_policy_priority_tie_breaking_ignores_input_order():
    policies = [Policy("b", "window", CLOSED, priority=5), Policy("a", "window", OPEN, priority=5)]
    assert run(policies=policies).decisions == run(policies=reversed(policies)).decisions
    assert run(policies=policies).decisions["window"].source == "policy:a"
    policies.append(Policy("c", "window", CLOSED, priority=6))
    assert run(policies=policies).decisions["window"].source == "policy:c"


def test_expiry_exactly_at_deadline_releases_ownership_without_catchup():
    manual = ManualLease("window", "target", OPEN, expires_at=15)
    before = run(now=14, manuals=[manual])
    at = run(now=15, manuals=[manual])
    assert before.decisions["window"].status == "pending"
    assert before.next_evaluation == 15
    assert at.decisions["window"].status == "idle"
    assert at.next_evaluation is None
    assert at.decisions["window"].candidates[0].reason == "manual expired"


def test_hands_off_indefinite_and_expiry():
    lease = ManualLease("window", "hands_off")
    assert lease.active(10**9)
    result = run(manuals=[lease], policies=[Policy("p", "window", OPEN)])
    assert result.decisions["window"].status == "hands_off"
    assert result.decisions["window"].target is None
    assert result.next_evaluation is None


def test_two_active_leases_rejected_but_expired_lease_does_not_conflict():
    with pytest.raises(ValueError, match="one active"):
        run(manuals=[ManualLease("window", "hands_off"), ManualLease("window", "target", OPEN)])
    run(
        manuals=[
            ManualLease("window", "hands_off", expires_at=1),
            ManualLease("window", "target", OPEN),
        ]
    )


@pytest.mark.parametrize(
    "resource,observation,status,reason",
    [
        (Resource("window", mode="observe"), Observation(CLOSED, True), "observe", "observe"),
        (
            Resource("window", mode="live", fault="storage fault"),
            Observation(CLOSED, True),
            "fault",
            "storage",
        ),
        (Resource("window", mode="live"), None, "unavailable", "fresh"),
        (Resource("window", mode="live"), Observation(CLOSED, False), "unavailable", "fresh"),
        (
            Resource("window", mode="live", observation_after=5),
            Observation(CLOSED, True, reported_at=4),
            "unavailable",
            "fresh",
        ),
        (
            Resource("window", mode="live"),
            Observation(CLOSED, True, restriction="wind"),
            "restricted",
            "wind",
        ),
        (Resource("window", mode="live"), Observation(OPEN, True), "satisfied", "matches"),
        (
            Resource("window", mode="live"),
            Observation(CLOSED, True),
            "pending",
            "target not reached",
        ),
    ],
)
def test_decision_gates_keep_desired_target(resource, observation, status, reason):
    observations = {"window": observation} if observation else {}
    decision = run(
        resources={"window": resource},
        observations=observations,
        manuals=[ManualLease("window", "target", OPEN)],
    ).decisions["window"]
    assert decision.status == status
    assert reason in decision.reason
    assert decision.target == OPEN


def test_hidden_rain_keeps_intent_until_device_reaches_target():
    manual = ManualLease("window", "target", OPEN, expires_at=2000)
    for now in (0, 300, 600, 900):
        decision = run(now=now, manuals=[manual]).decisions["window"]
        assert decision.status == "pending"
        assert decision.target == OPEN
        assert decision.reason == "target not reached"
    assert (
        run(now=1000, manuals=[manual], observations={"window": Observation(OPEN, True)})
        .decisions["window"]
        .status
        == "satisfied"
    )


@pytest.mark.parametrize(
    "policy,reason",
    [
        (Policy("p", "window", OPEN, enabled=False), "policy disabled"),
        (Policy("p", "window", OPEN, eligible=False), "policy ineligible"),
        (Policy("p", "window", None), "target unavailable"),
        (Policy("p", "window", Target()), "target unavailable"),
    ],
)
def test_ineligible_policies_remain_explainable(policy, reason):
    decision = run(policies=[policy]).decisions["window"]
    assert decision.status == "idle"
    assert decision.candidates[0].reason == reason


def test_occurrence_tombstone_wins_duplicates_and_never_replays():
    policy = Policy("morning", "window", OPEN, kind="occurrence")
    admitted = Occurrence("morning", "2026-10-25T06:00:00Z", 100)
    skipped = replace(admitted, skipped=True)
    assert run(policies=[policy], occurrences=[admitted]).decisions["window"].status == "pending"
    for records in ([skipped], [admitted, skipped], [skipped, admitted]):
        result = run(policies=[policy], occurrences=records)
        assert result.decisions["window"].status == "idle"
        assert result.decisions["window"].candidates[0].reason == "occurrence suppressed"
    assert (
        run(now=100, policies=[policy], occurrences=[admitted]).decisions["window"].status == "idle"
    )
    assert run(now=100, policies=[policy], occurrences=[admitted]).next_evaluation is None
    assert run(policies=[policy]).decisions["window"].status == "idle"


def test_adjacent_schedule_attribute_target_change_is_new_snapshot():
    first = run(policies=[Policy("schedule", "window", Target(position=20))])
    second = run(policies=[Policy("schedule", "window", Target(position=70))])
    assert first.decisions["window"].target.position == 20
    assert second.decisions["window"].target.position == 70


def test_occurrence_target_is_frozen_at_admission_despite_current_policy_change():
    admitted = Occurrence("morning", "block", 100, target=Target(position=40))
    policy = Policy("morning", "window", Target(position=80), kind="occurrence")
    result = run(policies=[policy], occurrences=[admitted])
    assert result.decisions["window"].target == Target(position=40)


@pytest.mark.parametrize("active,status", [(False, "inactive"), (None, "unknown")])
def test_inactive_or_unknown_requirement_releases_provider_requests(active, status):
    requirement = Requirement("air", active, (provider(),))
    result = run(
        requirements=[requirement],
        requirement_memory={"air": RequirementMemory("window", "window", 0)},
    )
    assert result.requirements["air"].status == status
    assert result.requirements["air"].memory == RequirementMemory()
    assert result.decisions["window"].status == "idle"


def test_acquisition_timeout_falls_back_and_cooldown_prevents_hot_loop():
    providers = [provider(), provider("inlet")]
    first = airflow_run(providers, now=0)
    assert first.requirements["air"].acquiring_provider == "window"
    assert first.next_evaluation == 20
    waiting = airflow_run(providers, now=19, previous=first.requirements["air"].memory)
    assert waiting.requirements["air"].memory.acquisition_started == 0
    second = airflow_run(providers, now=20, previous=waiting.requirements["air"].memory)
    assert second.requirements["air"].acquiring_provider == "inlet"
    assert second.requirements["air"].memory.failed_until == (("window", 70),)
    failed = airflow_run(providers, now=40, previous=second.requirements["air"].memory)
    assert failed.requirements["air"].status == "unmet"
    assert failed.next_evaluation == 70
    for now in (41, 50, 69):
        unchanged = airflow_run(providers, now=now, previous=failed.requirements["air"].memory)
        assert unchanged.requirements["air"].acquiring_provider is None
        assert unchanged.next_evaluation == 70
    retry = airflow_run(providers, now=70, previous=failed.requirements["air"].memory)
    assert retry.requirements["air"].acquiring_provider == "window"


def test_confirmed_provider_retained_without_oscillation():
    result = airflow_run([provider(), provider("inlet", confirmed=True)])
    assert result.requirements["air"].selected_provider == "inlet"
    retained = airflow_run(
        [provider(confirmed=True), provider("inlet", confirmed=True)],
        previous=result.requirements["air"].memory,
    )
    assert retained.requirements["air"].selected_provider == "inlet"
    assert retained.decisions["inlet"].target == OPEN
    assert retained.decisions["window"].target is None


def test_make_before_break_keeps_old_request_until_replacement_confirmed():
    memory = RequirementMemory(selected_provider="window", failed_until=(("window", 50),))
    acquiring = airflow_run([provider(), provider("inlet")], previous=memory)
    assert acquiring.decisions["window"].target == OPEN
    assert acquiring.decisions["inlet"].target == OPEN
    complete = airflow_run(
        [provider(), provider("inlet", confirmed=True)],
        previous=acquiring.requirements["air"].memory,
    )
    assert complete.decisions["window"].status == "idle"
    assert complete.decisions["inlet"].target == OPEN
    assert complete.requirements["air"].selected_provider == "inlet"


def test_manual_closure_is_honored_even_with_last_confirmed_provider():
    lease = ManualLease("window", "target", CLOSED)
    memory = RequirementMemory(selected_provider="window")
    result = airflow_run([provider()], previous=memory, manuals=[lease])
    assert result.decisions["window"].target == CLOSED
    assert result.requirements["air"].status == "unmet"
    assert "extraction remains unchanged" in result.requirements["air"].reason
    # Requirement output only has configured inlet resources, never an extractor command.
    assert set(result.decisions) == {"window", "inlet"}


def test_hands_off_never_acquires_but_observed_route_still_satisfies():
    lease = ManualLease("window", "hands_off")
    unmet = airflow_run([provider()], manuals=[lease])
    assert unmet.requirements["air"].status == "unmet"
    satisfied = airflow_run([provider(confirmed=True)], manuals=[lease])
    assert satisfied.requirements["air"].status == "satisfied"
    assert satisfied.decisions["window"].status == "hands_off"


def test_passive_provider_can_satisfy_but_never_acquires():
    unknown = airflow_run([Provider("door", confirmed=None)])
    assert unknown.requirements["air"].status == "unknown"
    assert unknown.next_evaluation is None
    confirmed = airflow_run([Provider("door", confirmed=True)])
    assert confirmed.requirements["air"].status == "satisfied"
    assert all(item.target is None for item in confirmed.decisions.values())


@pytest.mark.parametrize(
    "p,resources,observations",
    [
        (provider(eligible=False), {}, {}),
        (Provider("window", "window", None), {}, {}),
        (provider(), {}, {}),
        (provider(), {"window": Resource("window")}, {"window": Observation(CLOSED, True)}),
        (provider(), {"window": Resource("window", mode="live", fault="fault")}, {}),
        (provider(), {"window": Resource("window", mode="live")}, {}),
        (
            provider(),
            {"window": Resource("window", mode="live")},
            {"window": Observation(CLOSED, False)},
        ),
        (
            provider(),
            {"window": Resource("window", mode="live")},
            {"window": Observation(CLOSED, True, restriction="rain")},
        ),
    ],
)
def test_uncontrollable_providers_do_not_acquire(p, resources, observations):
    result = airflow_run([p], resources=resources, observations=observations)
    assert result.requirements["air"].status == "unmet"


def test_matching_manual_target_allows_confirmation_wait_without_override():
    result = airflow_run([provider()], manuals=[ManualLease("window", "target", OPEN)])
    assert result.requirements["air"].status == "acquiring"
    assert result.decisions["window"].source == "manual:service"


def test_manual_takeover_aborts_acquisition_and_tries_alternative():
    first = airflow_run([provider(), provider("inlet")])
    later = airflow_run(
        [provider(), provider("inlet")],
        previous=first.requirements["air"].memory,
        manuals=[ManualLease("window", "hands_off")],
        now=11,
    )
    assert later.requirements["air"].acquiring_provider == "inlet"
    assert later.decisions["window"].status == "hands_off"


def test_removed_provider_or_partial_memory_is_discarded():
    result = airflow_run(
        [provider()],
        previous=RequirementMemory("gone", "gone", 0, (("gone", 100),)),
    )
    assert result.requirements["air"].selected_provider is None
    assert result.requirements["air"].acquiring_provider == "window"
    assert result.requirements["air"].memory.failed_until == ()
    incomplete = airflow_run([provider()], previous=RequirementMemory(acquiring_provider="window"))
    assert incomplete.requirements["air"].memory.acquisition_started == 10


def test_confirmation_clears_provider_cooldown():
    result = airflow_run(
        [provider(confirmed=True)],
        previous=RequirementMemory(failed_until=(("window", 100),)),
    )
    assert result.requirements["air"].memory.failed_until == ()


def test_invalid_time_rejected():
    with pytest.raises(ValueError):
        run(now=float("nan"))
