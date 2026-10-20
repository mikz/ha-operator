"""Validate runtime data loaded through Home Assistant's native Store."""

from __future__ import annotations

from typing import cast

from .data import StoredSnapshot
from .return_monitor import ReturnMonitorState

_MAP_KEYS = (
    "manuals",
    "occurrences",
    "modes",
    "policy_enabled",
    "requests",
    "intents",
    "return_monitors",
)


class InvalidSnapshot(ValueError):
    """Restored runtime data is unusable."""


def empty_state() -> StoredSnapshot:
    return {
        "manuals": {},
        "occurrences": {},
        "modes": {},
        "policy_enabled": {},
        "requests": {},
        "intents": {},
        "return_monitors": {},
    }


def validate_state(state: object) -> StoredSnapshot:
    if not isinstance(state, dict):
        raise InvalidSnapshot("Snapshot data must be an object")
    if any(not isinstance(state.get(key), dict) for key in _MAP_KEYS):
        raise InvalidSnapshot("Snapshot is missing required intent maps")
    if any(value not in ("observe", "live") for value in state["modes"].values()):
        raise InvalidSnapshot("Resource mode must be observe or live")
    if any(type(value) is not bool for value in state["policy_enabled"].values()):
        raise InvalidSnapshot("Policy enablement must be boolean")
    if any(type(value) is not bool for value in state["intents"].values()):
        raise InvalidSnapshot("Desired control values must be boolean")
    for item in state["return_monitors"].values():
        if not isinstance(item, dict) or set(item) != {"fingerprint", "state"}:
            raise InvalidSnapshot("Invalid return monitor record")
        fingerprint = item["fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)
        ):
            raise InvalidSnapshot("Invalid return monitor fingerprint")
        if not isinstance(item["state"], dict):
            raise InvalidSnapshot("Invalid return monitor state")
        try:
            ReturnMonitorState.from_record(item["state"])
        except (ValueError, TypeError) as err:
            raise InvalidSnapshot("Invalid return monitor state") from err
    # The incompatible configuration migration already rejected live legacy inputs.
    state = {key: value for key, value in state.items() if key != "policy_inputs"}
    # Only envelope/maps/input-state validation has run. Runtime owns the remaining
    # manual, occurrence and request semantic checks before control admission.
    return cast(StoredSnapshot, state)
