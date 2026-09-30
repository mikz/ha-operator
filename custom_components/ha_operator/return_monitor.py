"""Confirm effective return targets from raw feedback, with explicit UTC time."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _position(value: float, name: str) -> None:
    _finite(value, name)
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class ReturnMonitorInput:
    target_at_most: float
    warning_after_seconds: float
    tolerance: float

    def __post_init__(self) -> None:
        _position(self.target_at_most, "target_at_most")
        _finite(self.warning_after_seconds, "warning_after_seconds")
        _finite(self.tolerance, "tolerance")
        if self.warning_after_seconds <= 0 or self.tolerance < 0:
            raise ValueError("warning duration must be positive and tolerance non-negative")


@dataclass(frozen=True, slots=True)
class ReturnMonitorState:
    target_position: float | None = None
    due_at: float | None = None
    overdue: bool = False

    def __post_init__(self) -> None:
        if self.target_position is not None:
            _position(self.target_position, "target_position")
        if self.due_at is not None:
            _finite(self.due_at, "due_at")
        if type(self.overdue) is not bool:
            raise ValueError("overdue must be boolean")
        if (self.target_position is None) != (self.due_at is None):
            raise ValueError("return episode requires both target and deadline")
        if self.overdue and self.due_at is None:
            raise ValueError("overdue return requires an active episode")

    @property
    def phase(self) -> str:
        return "overdue" if self.overdue else "waiting" if self.due_at is not None else "idle"

    def to_record(self) -> dict[str, Any]:
        return {
            "target_position": self.target_position,
            "due_at": self.due_at,
            "overdue": self.overdue,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ReturnMonitorState:
        if set(record) != {"target_position", "due_at", "overdue"}:
            raise ValueError("return monitor state requires its exact fields")
        return cls(**dict(record))


@dataclass(frozen=True, slots=True)
class ReturnMonitorTransition:
    state: ReturnMonitorState
    next_deadline: float | None


def return_transition(
    config: ReturnMonitorInput,
    state: ReturnMonitorState,
    *,
    target_position: float | None,
    observed_position: float | None,
    active: bool,
    now: float,
) -> ReturnMonitorTransition:
    """Retain the absolute deadline until confirmation, end, or target change.

    Caller supplies only available, fresh raw observations; None cannot confirm.
    A successful command or desired value is never observation evidence.
    """
    _finite(now, "now")
    if type(active) is not bool:
        raise ValueError("active must be boolean")
    if target_position is not None:
        _position(target_position, "target_position")
    if observed_position is not None:
        _position(observed_position, "observed_position")
    if (
        not active
        or target_position is None
        or target_position > config.target_at_most
        or (
            observed_position is not None
            and abs(observed_position - target_position) <= config.tolerance
        )
    ):
        return ReturnMonitorTransition(ReturnMonitorState(), None)
    if state.target_position != target_position:
        state = ReturnMonitorState(target_position, now + config.warning_after_seconds)
    assert state.due_at is not None
    if now >= state.due_at:
        state = replace(state, overdue=True)
    return ReturnMonitorTransition(state, None if state.overdue else state.due_at)
