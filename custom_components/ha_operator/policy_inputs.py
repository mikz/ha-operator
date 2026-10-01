"""Two bounded policy inputs, with caller-supplied events and UTC Unix time.

Transitions describe intent to admit or suppress an occurrence. The caller must
commit the input state and occurrence changes together before using that intent.
The HA boundary supplies episode IDs and deduplicates previously replaced IDs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from .data import NumericStateRecord, TimerStateRecord


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _positive(value: float, name: str) -> None:
    _finite(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _episode(value: str | None) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("episode_id must contain 1 to 128 characters")


@dataclass(frozen=True, slots=True)
class NumericInput:
    """A strict below-threshold comparison with a continuous qualification delay."""

    threshold: float
    qualification_seconds: float

    def __post_init__(self) -> None:
        _finite(self.threshold, "threshold")
        _positive(self.qualification_seconds, "qualification_seconds")


@dataclass(frozen=True, slots=True)
class NumericReport:
    """A fresh finite report, or None for unknown/unavailable source quality."""

    value: float | None

    def __post_init__(self) -> None:
        if self.value is not None:
            _finite(self.value, "value")


@dataclass(frozen=True, slots=True)
class NumericState:
    due_at: float | None = None
    qualified: bool = False
    source_quality: Literal["numeric", "unknown"] = "unknown"
    recovery_pending: bool = False

    def __post_init__(self) -> None:
        if self.due_at is not None:
            _finite(self.due_at, "due_at")
        if type(self.qualified) is not bool or type(self.recovery_pending) is not bool:
            raise ValueError("qualification and recovery flags must be boolean")
        if self.source_quality not in ("numeric", "unknown"):
            raise ValueError("source_quality must be numeric or unknown")
        if self.qualified and self.due_at is None:
            raise ValueError("qualified input must retain its qualification deadline")

    @property
    def phase(self) -> str:
        if self.recovery_pending:
            return "recovering"
        if self.qualified:
            return "qualified"
        return "qualifying" if self.due_at is not None else "idle"

    def to_record(self) -> NumericStateRecord:
        return {
            "due_at": self.due_at,
            "qualified": self.qualified,
            "source_quality": self.source_quality,
            "recovery_pending": self.recovery_pending,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> NumericState:
        return cls(**dict(record))


@dataclass(frozen=True, slots=True)
class NumericTransition:
    state: NumericState
    eligible: bool | None
    next_deadline: float | None


def recover_numeric(state: NumericState) -> NumericState:
    """Retain continuity, but require fresh finite evidence after startup."""
    return replace(state, source_quality="unknown", recovery_pending=True)


def numeric_transition(
    config: NumericInput,
    state: NumericState,
    event: NumericReport | None,
    *,
    now: float,
) -> NumericTransition:
    """Apply one ordered report, or a deadline tick when event is None.

    Unknown readings preserve the episode and latched qualification. Pending
    qualification cannot mature while the source is unknown. A fresh cold report
    can qualify immediately against a retained, overdue deadline.
    """
    _finite(now, "now")
    if event is not None:
        if event.value is None:
            state = replace(state, source_quality="unknown")
        elif event.value >= config.threshold:
            state = NumericState(source_quality="numeric")
        else:
            state = replace(
                state,
                due_at=(
                    state.due_at if state.due_at is not None else now + config.qualification_seconds
                ),
                source_quality="numeric",
                recovery_pending=False,
            )
    if (
        state.due_at is not None
        and now >= state.due_at
        and state.source_quality == "numeric"
        and not state.recovery_pending
    ):
        state = replace(state, qualified=True)
    eligible = None if state.recovery_pending else state.qualified
    deadline = (
        state.due_at
        if not state.qualified and state.source_quality == "numeric" and not state.recovery_pending
        else None
    )
    return NumericTransition(state, eligible, deadline)


@dataclass(frozen=True, slots=True)
class TimerInput:
    qualification_seconds: float
    request_seconds: float

    def __post_init__(self) -> None:
        _positive(self.qualification_seconds, "qualification_seconds")
        _positive(self.request_seconds, "request_seconds")


@dataclass(frozen=True, slots=True)
class TimerEvent:
    """Only start creates an episode; reports/restoration must not invent starts.

    Non-start events name the episode they affect, so stale callbacks cannot
    cancel a replacement. 'suppress' also dead-ends rejected runtime admissions.
    For finish/change, accepted_at_capture preserves whether admission had been
    committed when the event arrived, even if an older save completes first.
    """

    kind: Literal["start", "pause", "resume", "cancel", "finish", "change", "suppress"]
    episode_id: str
    finish_at: float | None = None
    accepted_at_capture: bool | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("start", "pause", "resume", "cancel", "finish", "change", "suppress"):
            raise ValueError("unsupported timer event")
        _episode(self.episode_id)
        if self.finish_at is not None:
            _finite(self.finish_at, "finish_at")
        if self.accepted_at_capture is not None and type(self.accepted_at_capture) is not bool:
            raise ValueError("accepted_at_capture must be boolean or None")


@dataclass(frozen=True, slots=True)
class TimerState:
    episode_id: str | None = None
    phase: Literal["idle", "qualifying", "accepted", "suppressed", "expired"] = "idle"
    due_at: float | None = None
    expires_at: float | None = None
    finish_at: float | None = None

    def __post_init__(self) -> None:
        if self.phase not in ("idle", "qualifying", "accepted", "suppressed", "expired"):
            raise ValueError("unsupported timer phase")
        for name in ("due_at", "expires_at", "finish_at"):
            if (value := getattr(self, name)) is not None:
                _finite(value, name)
        if self.phase == "idle":
            if any(value is not None for value in (self.episode_id, self.due_at, self.expires_at)):
                raise ValueError("idle timer cannot retain an episode")
        else:
            _episode(self.episode_id)
            if self.expires_at is None:
                raise ValueError("timer episode must retain its absolute expiry")
        if (self.phase == "qualifying") != (self.due_at is not None):
            raise ValueError("only qualifying timer episodes have a deadline")
        if self.due_at is not None:
            assert self.expires_at is not None
            if self.expires_at <= self.due_at:
                raise ValueError("timer expiry must follow qualification")

    def to_record(self) -> TimerStateRecord:
        return {
            "episode_id": self.episode_id,
            "phase": self.phase,
            "due_at": self.due_at,
            "expires_at": self.expires_at,
            "finish_at": self.finish_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TimerState:
        return cls(**dict(record))


@dataclass(frozen=True, slots=True)
class OccurrenceChange:
    action: Literal["admit", "suppress"]
    episode_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class TimerTransition:
    state: TimerState
    occurrence_changes: tuple[OccurrenceChange, ...]
    next_deadline: float | None


def _suppression(state: TimerState) -> OccurrenceChange:
    assert state.episode_id is not None and state.expires_at is not None
    return OccurrenceChange("suppress", state.episode_id, state.expires_at)


def timer_transition(
    config: TimerInput,
    state: TimerState,
    event: TimerEvent | None,
    *,
    now: float,
) -> TimerTransition:
    """Apply one lifecycle event before considering the qualification/expiry tick.

    The opening expiry is start + qualification + request, even if processing is
    delayed. Accepted natural completion and finish-time edits never extend it.
    Terminal states retain their ID to prevent a repeated event opening again.
    """
    _finite(now, "now")
    changes: list[OccurrenceChange] = []
    if event is not None:
        if event.kind == "start" and event.episode_id != state.episode_id:
            if state.phase in ("qualifying", "accepted"):
                changes.append(_suppression(state))
            due_at = now + config.qualification_seconds
            state = TimerState(
                event.episode_id,
                "qualifying",
                due_at,
                due_at + config.request_seconds,
                event.finish_at,
            )
        elif event.episode_id == state.episode_id:
            suppress = (
                event.kind in ("pause", "cancel", "suppress")
                or (
                    event.kind == "finish"
                    and (state.phase == "qualifying" or event.accepted_at_capture is False)
                )
                or (
                    event.kind == "change"
                    and event.finish_at != state.finish_at
                    and (state.phase == "qualifying" or event.accepted_at_capture is False)
                )
            )
            if suppress and state.phase in ("qualifying", "accepted"):
                changes.append(_suppression(state))
                state = replace(state, phase="suppressed", due_at=None)
            if event.kind == "change":
                state = replace(state, finish_at=event.finish_at)
    if state.phase in ("qualifying", "accepted"):
        assert state.expires_at is not None
        if now >= state.expires_at:
            changes.append(_suppression(state))
            state = replace(state, phase="expired", due_at=None)
        elif state.phase == "qualifying":
            assert state.due_at is not None and state.episode_id is not None
            if now >= state.due_at:
                assert state.expires_at is not None
                changes.append(OccurrenceChange("admit", state.episode_id, state.expires_at))
                state = replace(state, phase="accepted", due_at=None)
    deadline = (
        state.due_at
        if state.phase == "qualifying"
        else state.expires_at
        if state.phase == "accepted"
        else None
    )
    return TimerTransition(state, tuple(changes), deadline)


def recover_timer(state: TimerState, *, now: float) -> TimerTransition:
    """Discard pending admission; retain only accepted, unexpired requests."""
    _finite(now, "now")
    if state.phase == "qualifying":
        return TimerTransition(
            replace(state, phase="suppressed", due_at=None), (_suppression(state),), None
        )
    if state.phase == "accepted":
        assert state.expires_at is not None
        if now >= state.expires_at:
            return TimerTransition(replace(state, phase="expired"), (_suppression(state),), None)
        return TimerTransition(state, (), state.expires_at)
    return TimerTransition(state, (), None)
