"""Durable boolean intent rules, independent of HA and observed device state."""

from __future__ import annotations

from collections.abc import Mapping

from .data import IntentConfig


class IntentValidationError(ValueError):
    """Expected pure intent failure with presentation metadata."""

    def __init__(self, key: str, message: str, **placeholders: str) -> None:
        super().__init__(message)
        self.translation_key = key
        self.translation_placeholders = placeholders


def validate_graph(intents: Mapping[str, IntentConfig]) -> None:
    """Reject missing references and cycles before any intent can be admitted."""
    visited: set[str] = set()
    pending: set[str] = set()

    def visit(key: str) -> None:
        if key not in intents:
            raise IntentValidationError(
                "config_intent_reference", "On targets must reference configured desired controls"
            )
        if key in pending:
            raise IntentValidationError(
                "config_intent_cycle", "Desired control on-targets must not contain a cycle"
            )
        if key in visited:
            return
        pending.add(key)
        for target in intents[key].get("on_targets", ()):
            visit(target)
        pending.remove(key)
        visited.add(key)

    for key in intents:
        visit(key)


def apply_command(
    values: dict[str, bool], intents: Mapping[str, IntentConfig], key: str, on: bool
) -> None:
    """Apply one explicit command and rising edges in one state update.

    A first explicit ON also establishes its dependents. Initialization and
    restoration do not call this function. Repeated ON does not reattach a child
    that has since been turned off independently.
    """
    if key not in intents or type(on) is not bool:
        raise IntentValidationError(
            "invalid_desired_control", "Unknown desired control or invalid boolean value"
        )
    previous = values.get(key)
    values[key] = on
    if on and previous is not True:
        for target in intents[key].get("on_targets", ()):
            apply_command(values, intents, target, True)
