"""Durable boolean intent rules, independent of HA and observed device state."""

from __future__ import annotations

from collections.abc import Mapping


def validate_graph(intents: Mapping[str, dict]) -> None:
    """Reject missing references and cycles before any intent can be admitted."""
    visited: set[str] = set()
    pending: set[str] = set()

    def visit(key: str) -> None:
        if key not in intents:
            raise ValueError("On targets must reference configured desired controls")
        if key in pending:
            raise ValueError("Desired control on-targets must not contain a cycle")
        if key in visited:
            return
        pending.add(key)
        for target in intents[key].get("on_targets", ()):
            visit(target)
        pending.remove(key)
        visited.add(key)

    for key in intents:
        visit(key)


def apply_command(values: dict[str, bool], intents: Mapping[str, dict], key: str, on: bool) -> None:
    """Apply one explicit command and rising edges within one durable transaction.

    A first explicit ON also establishes its dependents. Initialization and
    restoration do not call this function. Repeated ON does not reattach a child
    that has since been turned off independently.
    """
    if key not in intents or type(on) is not bool:
        raise ValueError("Unknown desired control or invalid boolean value")
    previous = values.get(key)
    values[key] = on
    if on and previous is not True:
        for target in intents[key].get("on_targets", ()):
            apply_command(values, intents, target, True)
