"""Pure, deterministic arbitration and airflow planning.

There are no Home Assistant imports, clocks, tasks, storage calls, or actuator calls
here. Callers provide fresh observations and explicit UTC Unix time. Decisions are
intent; neither a decision nor a successful dispatch is physical confirmation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


@dataclass(frozen=True, slots=True)
class Target:
    """An adapter-normalized target; absent fields impose no constraint."""

    position: float | None = None
    on: bool | None = None
    percentage: float | None = None
    direction: str | None = None
    profile: str | None = None

    def __post_init__(self) -> None:
        for name in ("position", "percentage"):
            value = getattr(self, name)
            if value is not None:
                _finite(value, name)
                if not 0 <= value <= 100:
                    raise ValueError(f"{name} must be between 0 and 100")
        if self.on is not None and not isinstance(self.on, bool):
            raise ValueError("on must be a boolean")
        for name in ("direction", "profile"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty string")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Target:
        """Reject misspelled fields instead of silently admitting empty intent."""
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            name: value
            for name in ("position", "on", "percentage", "direction", "profile")
            if (value := getattr(self, name)) is not None
        }


@dataclass(frozen=True, slots=True)
class Observation:
    target: Target | None
    available: bool
    moving: bool = False
    restriction: str | None = None
    reported_at: float = 0


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    kind: str = "cover"
    mode: str = "observe"
    tolerance: float = 2
    fault: str | None = None
    observation_after: float | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"observe", "live"}:
            raise ValueError("resource mode must be observe or live")
        _finite(self.tolerance, "tolerance")
        if self.tolerance < 0:
            raise ValueError("tolerance cannot be negative")


@dataclass(frozen=True, slots=True)
class ManualLease:
    resource_id: str
    mode: str
    target: Target | None = None
    expires_at: float | None = None
    request_id: str | None = None
    source: str = "service"

    def __post_init__(self) -> None:
        if self.mode not in {"target", "hands_off"}:
            raise ValueError("manual mode must be target or hands_off")
        if self.mode == "target" and (self.target is None or not self.target.to_dict()):
            raise ValueError("target lease needs a nonempty target")
        if self.mode == "hands_off" and self.target is not None:
            raise ValueError("hands_off cannot carry a target")
        if self.expires_at is not None:
            _finite(self.expires_at, "expires_at")

    def active(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at


@dataclass(frozen=True, slots=True)
class Policy:
    id: str
    resource_id: str
    target: Target | None
    priority: int = 0
    enabled: bool = True
    eligible: bool = True
    kind: str = "state"

    def __post_init__(self) -> None:
        if self.kind not in {"state", "occurrence"}:
            raise ValueError("policy kind must be state or occurrence")


@dataclass(frozen=True, slots=True)
class Occurrence:
    policy_id: str
    occurrence_id: str
    expires_at: float
    skipped: bool = False
    target: Target | None = None

    def __post_init__(self) -> None:
        _finite(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class Evidence:
    """One independently observed predicate, never a managed desired value."""

    value: Any
    expected: Any
    operator: str = "eq"
    available: bool = True
    fresh: bool = True
    kind: str = "position"


def evaluate_evidence(evidence: Iterable[Evidence]) -> bool | None:
    """All predicates must hold. Unknown or stale evidence is never confirmed."""
    items = tuple(evidence)
    if not items:
        return None
    unknown = False
    for item in items:
        if item.operator not in {"eq", "gte", "lte"}:
            raise ValueError(f"unsupported evidence operator: {item.operator}")
        if not item.available or not item.fresh or item.value is None:
            unknown = True
            continue
        if item.value in ("unknown", "unavailable"):
            unknown = True
            continue
        if item.operator == "eq":
            result = item.value == item.expected
        else:
            # bool is numeric in Python, but cannot prove position or airflow.
            if isinstance(item.value, bool) or isinstance(item.expected, bool):
                unknown = True
                continue
            try:
                actual, expected = float(item.value), float(item.expected)
            except ValueError, TypeError:
                unknown = True
                continue
            if not math.isfinite(actual) or not math.isfinite(expected):
                unknown = True
                continue
            result = actual >= expected if item.operator == "gte" else actual <= expected
        if not result:
            return False
    return None if unknown else True


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    resource_id: str | None = None
    target: Target | None = None
    confirmed: bool | None = False
    eligible: bool = True


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    active: bool | None
    providers: tuple[Provider, ...]
    acquisition_timeout: float = 120
    retry_interval: float = 300

    def __post_init__(self) -> None:
        for name in ("acquisition_timeout", "retry_interval"):
            _finite(getattr(self, name), name)
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if len({provider.id for provider in self.providers}) != len(self.providers):
            raise ValueError("provider IDs must be unique within a requirement")


@dataclass(frozen=True, slots=True)
class RequirementMemory:
    """Transient acquisition progress, recomputed after a process restart."""

    selected_provider: str | None = None
    acquiring_provider: str | None = None
    acquisition_started: float | None = None
    failed_until: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    source: str
    target: Target | None
    priority: int = 0
    eligible: bool = True
    reason: str = "eligible"


@dataclass(frozen=True, slots=True)
class Request:
    """A current candidate carrying its precedence independently of policy priority."""

    resource_id: str
    candidate: Candidate
    tier: int
    hands_off: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    resource_id: str
    target: Target | None
    status: str
    source: str | None
    reason: str
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True, slots=True)
class RequirementResult:
    id: str
    status: str
    selected_provider: str | None
    acquiring_provider: str | None
    reason: str
    memory: RequirementMemory


@dataclass(frozen=True, slots=True)
class Evaluation:
    decisions: Mapping[str, Decision]
    requirements: Mapping[str, RequirementResult]
    next_evaluation: float | None = None


def matches(target: Target, observed: Observation | Target | None, tolerance: float = 2) -> bool:
    """Compare requested fields against feedback, not dispatched or desired state."""
    if isinstance(observed, Observation):
        if not observed.available:
            return False
        observed = observed.target
    if observed is None or not target.to_dict():
        return False
    for key, desired in target.to_dict().items():
        actual = getattr(observed, key)
        if actual is None:
            return False
        if key in {"position", "percentage"}:
            if abs(desired - actual) > tolerance:
                return False
        elif actual != desired:
            return False
    return True


def _fresh(resource: Resource, observation: Observation | None) -> bool:
    return bool(
        observation is not None
        and observation.available
        and (
            resource.observation_after is None
            or observation.reported_at >= resource.observation_after
        )
    )


def _can_acquire(
    provider: Provider,
    resources: Mapping[str, Resource],
    observations: Mapping[str, Observation],
    manuals: Mapping[str, ManualLease],
) -> bool:
    if not provider.eligible or provider.resource_id is None or provider.target is None:
        return False
    resource = resources.get(provider.resource_id)
    observation = observations.get(provider.resource_id)
    if (
        resource is None
        or resource.mode != "live"
        or resource.fault is not None
        or not _fresh(resource, observation)
        or observation.restriction is not None
    ):
        return False
    manual = manuals.get(provider.resource_id)
    return manual is None or (
        manual.mode == "target"
        and manual.target is not None
        and matches(provider.target, manual.target, resource.tolerance)
    )


def _plan_requirement(
    requirement: Requirement,
    previous: RequirementMemory,
    now: float,
    resources: Mapping[str, Resource],
    observations: Mapping[str, Observation],
    manuals: Mapping[str, ManualLease],
) -> tuple[RequirementResult, tuple[Request, ...], tuple[float, ...]]:
    if requirement.active is not True:
        status = "inactive" if requirement.active is False else "unknown"
        result = RequirementResult(
            requirement.id, status, None, None, "activation is " + status, RequirementMemory()
        )
        return result, (), ()

    providers = {provider.id: provider for provider in requirement.providers}
    selected = previous.selected_provider if previous.selected_provider in providers else None
    failed = {
        provider_id: until
        for provider_id, until in previous.failed_until
        if provider_id in providers and until > now
    }
    acquiring = previous.acquiring_provider
    started = previous.acquisition_started
    deadlines: list[float] = []

    # A previously confirmed route wins ties, avoiding oscillation on every update.
    confirmed = [provider for provider in requirement.providers if provider.confirmed is True]
    adequate = next((provider for provider in confirmed if provider.id == selected), None)
    if adequate is None and confirmed:
        adequate = confirmed[0]
    if adequate is not None:
        selected = adequate.id
        acquiring, started = None, None
        failed.pop(selected, None)
        status, reason = "satisfied", "provider independently confirmed"
    else:
        current = providers.get(acquiring) if acquiring is not None else None
        if current is not None and started is not None:
            if not _can_acquire(current, resources, observations, manuals):
                acquiring, started = None, None
            elif now >= started + requirement.acquisition_timeout:
                failed[current.id] = now + requirement.retry_interval
                acquiring, started = None, None
        else:
            acquiring, started = None, None
        if acquiring is None:
            candidate = next(
                (
                    provider
                    for provider in requirement.providers
                    if provider.id not in failed
                    and _can_acquire(provider, resources, observations, manuals)
                ),
                None,
            )
            if candidate is not None:
                acquiring, started = candidate.id, now
        if acquiring is not None and started is not None:
            status, reason = "acquiring", "waiting for independent provider confirmation"
            deadlines.append(started + requirement.acquisition_timeout)
        else:
            status = "unknown" if any(p.confirmed is None for p in providers.values()) else "unmet"
            reason = "no eligible confirmed provider; extraction remains unchanged"

    # Keep the old provider requested until its replacement is independently confirmed.
    # Manual ownership still arbitrates above both of these requests.
    requests: list[Request] = []
    for provider_id in dict.fromkeys((selected, acquiring)):
        if provider_id is None:
            continue
        provider = providers[provider_id]
        if provider.resource_id is not None and provider.target is not None:
            requests.append(
                Request(
                    provider.resource_id,
                    Candidate(f"requirement:{requirement.id}:{provider.id}", provider.target),
                    tier=1,
                )
            )
    deadlines.extend(failed.values())
    memory = RequirementMemory(selected, acquiring, started, tuple(sorted(failed.items())))
    return (
        RequirementResult(requirement.id, status, selected, acquiring, reason, memory),
        tuple(requests),
        tuple(deadlines),
    )


def _policy_requests(
    policies: Iterable[Policy], occurrences: Iterable[Occurrence], now: float
) -> tuple[tuple[Request, ...], tuple[float, ...]]:
    records: dict[tuple[str, str], Occurrence] = {}
    for occurrence in occurrences:
        key = (occurrence.policy_id, occurrence.occurrence_id)
        previous = records.get(key)
        # A tombstone must dominate an accidentally duplicated admitted record.
        if previous is None or occurrence.skipped:
            records[key] = occurrence
    requests: list[Request] = []
    deadlines: list[float] = []
    for policy in policies:
        relevant: list[Occurrence | None] = (
            [item for item in records.values() if item.policy_id == policy.id]
            if policy.kind == "occurrence"
            else [None]
        )
        for occurrence in relevant:
            source = f"policy:{policy.id}"
            reason = "eligible"
            target = policy.target
            if occurrence is not None:
                source += f":{occurrence.occurrence_id}"
                if occurrence.target is not None:
                    target = occurrence.target
                if occurrence.expires_at > now:
                    deadlines.append(occurrence.expires_at)
                if occurrence.skipped:
                    reason = "occurrence suppressed"
                elif occurrence.expires_at <= now:
                    reason = "occurrence expired"
            if not policy.enabled:
                reason = "policy disabled"
            elif not policy.eligible:
                reason = "policy ineligible"
            elif target is None or not target.to_dict():
                reason = "target unavailable"
            requests.append(
                Request(
                    policy.resource_id,
                    Candidate(source, target, policy.priority, reason == "eligible", reason),
                    tier=0,
                )
            )
    return tuple(requests), tuple(deadlines)


def evaluate(
    *,
    now: float,
    resources: Mapping[str, Resource],
    observations: Mapping[str, Observation],
    manuals: Iterable[ManualLease] = (),
    policies: Iterable[Policy] = (),
    occurrences: Iterable[Occurrence] = (),
    requirements: Iterable[Requirement] = (),
    requirement_memory: Mapping[str, RequirementMemory] | None = None,
) -> Evaluation:
    """Resolve the current snapshot without mutating it or retaining command queues."""
    _finite(now, "now")
    requests, policy_deadlines = _policy_requests(policies, occurrences, now)
    all_requests = list(requests)
    deadlines = list(policy_deadlines)
    valid_manuals: dict[str, ManualLease] = {}
    for manual in manuals:
        if manual.expires_at is not None and manual.expires_at > now:
            deadlines.append(manual.expires_at)
        active = manual.active(now)
        if active:
            if manual.resource_id in valid_manuals:
                raise ValueError("only one active manual lease is permitted per resource")
            valid_manuals[manual.resource_id] = manual
        all_requests.append(
            Request(
                manual.resource_id,
                Candidate(
                    f"manual:{manual.source}",
                    manual.target,
                    eligible=active,
                    reason="eligible" if active else "manual expired",
                ),
                tier=2,
                hands_off=manual.mode == "hands_off",
            )
        )

    results: dict[str, RequirementResult] = {}
    memories = requirement_memory or {}
    for requirement in requirements:
        result, provider_requests, requirement_deadlines = _plan_requirement(
            requirement,
            memories.get(requirement.id, RequirementMemory()),
            now,
            resources,
            observations,
            valid_manuals,
        )
        results[requirement.id] = result
        all_requests.extend(provider_requests)
        deadlines.extend(requirement_deadlines)

    decisions: dict[str, Decision] = {}
    for resource_id, resource in resources.items():
        ranked = sorted(
            (request for request in all_requests if request.resource_id == resource_id),
            key=lambda request: (
                -request.tier,
                -request.candidate.priority,
                request.candidate.source,
            ),
        )
        winner = next((request for request in ranked if request.candidate.eligible), None)
        candidates = tuple(
            request.candidate
            if not request.candidate.eligible or request is winner
            else Candidate(
                request.candidate.source,
                request.candidate.target,
                request.candidate.priority,
                False,
                "superseded by higher precedence request",
            )
            for request in ranked
        )
        target = winner.candidate.target if winner else None
        source = winner.candidate.source if winner else None
        observation = observations.get(resource_id)
        if winner is not None and winner.hands_off:
            status, reason = "hands_off", "manual hands-off lease is active"
        elif resource.fault is not None:
            status, reason = "fault", resource.fault
        elif winner is None:
            status, reason = "idle", "no eligible request"
        elif resource.mode != "live":
            status, reason = "observe", "observe mode prohibits actuation"
        elif not _fresh(resource, observation):
            status, reason = "unavailable", "waiting for fresh physical observations"
        elif observation.restriction is not None:
            status, reason = "restricted", observation.restriction
        elif target is not None and matches(target, observation, resource.tolerance):
            status, reason = "satisfied", "observed state matches target"
        else:
            status, reason = "pending", "target not reached"
        decisions[resource_id] = Decision(resource_id, target, status, source, reason, candidates)
    future = [deadline for deadline in deadlines if deadline > now]
    return Evaluation(
        MappingProxyType(decisions), MappingProxyType(results), min(future, default=None)
    )
