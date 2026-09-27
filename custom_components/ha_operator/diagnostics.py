"""Read-only, allowlisted diagnostics without configuration or personal names."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

_STATUSES = {
    "pending",
    "applying",
    "waiting",
    "satisfied",
    "idle",
    "observe",
    "hands_off",
    "unavailable",
    "restricted",
    "fault",
    "inactive",
    "unknown",
    "acquiring",
    "unmet",
    "initializing",
}


def _identifier(value: Any) -> str | None:
    """Pseudonyms preserve relationships without revealing entity IDs or labels."""
    return sha256(str(value).encode()).hexdigest()[:12] if value is not None else None


def _number(value: Any) -> float | int | None:
    return value if type(value) in (int, float) else None


def _target(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        return None
    clean: dict[str, Any] = {}
    for key in ("position", "percentage"):
        if _number(value.get(key)) is not None:
            clean[key] = value[key]
    if isinstance(value.get("on"), bool):
        clean["on"] = value["on"]
    if value.get("direction") in {"forward", "reverse"}:
        clean["direction"] = value["direction"]
    if value.get("profile") is not None:
        clean["profile"] = _identifier(value["profile"])
    return clean


def _status(value: Any) -> str:
    return value if isinstance(value, str) and value in _STATUSES else "unknown"


def _trace_health(value: dict[str, Any]) -> dict[str, Any]:
    """Export health counters only; raw records and paths belong outside diagnostics."""
    return {
        **{key: value.get(key) is True for key in ("enabled", "healthy", "complete")},
        **{
            key: _number(value.get(key))
            for key in (
                "last_sequence",
                "durable_sequence",
                "queued_records",
                "dropped_records",
                "write_errors",
                "rotations",
                "last_heartbeat",
                "last_write_at",
            )
        },
        "session": _identifier(value.get("session_id")),
    }


async def async_get_config_entry_diagnostics(hass: Any, entry: Any) -> dict[str, Any]:
    """Export structural evidence only; never call devices or alter runtime state."""
    runtime = entry.runtime_data
    resources: dict[str, Any] = {}
    for identifier, config in runtime.resources.items():
        observation = runtime.observations.get(identifier)
        decision = runtime.decisions.get(identifier)
        lease = runtime.manual(identifier)
        command = runtime.last_commands.get(identifier)
        resources[_identifier(identifier)] = {
            "kind": config["kind"]
            if config["kind"] in {"cover", "fan", "relay_fan", "switch"}
            else "unknown",
            "mode": "live" if runtime.mode(identifier) == "live" else "observe",
            "status": _status(decision.status if decision else None),
            "source": _identifier(decision.source) if decision else None,
            "desired": _target(decision.target) if decision else None,
            "observed": _target(observation.target) if observation else None,
            "available": bool(observation and observation.available),
            "moving": bool(observation and observation.moving),
            "restricted": bool(observation and observation.restriction),
            "reported_at": _number(observation.reported_at) if observation else None,
            "manual": None
            if lease is None
            else {
                "mode": "hands_off" if lease.mode == "hands_off" else "target",
                "expires_at": _number(lease.expires_at),
                "target": _target(lease.target),
            },
            "last_command": None
            if not isinstance(command, dict)
            else {
                "target": _target(command.get("target")),
                "at": _number(command.get("at")),
            },
            "attempts": _number(runtime.attempts.get(identifier, 0)),
            "next_attempt": _number(runtime.next_attempts.get(identifier)),
        }
    requirements: dict[str, Any] = {}
    for identifier in runtime.requirements:
        result = runtime.requirement_results.get(identifier)
        requirements[_identifier(identifier)] = {
            "status": _status(result.status if result else None),
            "selected_provider": _identifier(result.selected_provider) if result else None,
            "acquiring_provider": _identifier(result.acquiring_provider) if result else None,
        }
    history = []
    for event in list(runtime.history)[-100:]:
        history.append(
            {
                "resource": _identifier(event.get("resource_id")),
                "status": _status(event.get("status")),
                "source": _identifier(event.get("source")),
                "target": _target(event.get("target")),
                "at": _number(event.get("at")),
            }
        )
    return {
        "version": 1,
        "faulted": bool(runtime.fault),
        "shadow_locked": bool(runtime.shadow_locked),
        "trace": _trace_health(runtime.trace_health()),
        "resources": resources,
        "policies": {
            _identifier(key): {"enabled": bool(runtime.policy_enabled(key))}
            for key in runtime.policies
        },
        "requirements": requirements,
        "history": history,
    }
