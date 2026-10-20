"""Read-only, allowlisted diagnostics without configuration or personal names."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast, overload

from homeassistant.core import HomeAssistant

from . import OperatorConfigEntry

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

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


@overload
def _identifier(value: str) -> str: ...


@overload
def _identifier(value: object) -> str | None: ...


def _identifier(value: object) -> str | None:
    """Pseudonyms preserve relationships without revealing entity IDs or labels."""
    return sha256(str(value).encode()).hexdigest()[:12] if value is not None else None


def _number(value: object) -> float | int | None:
    # The exact-type check excludes bool and nonnumeric native attributes.
    return cast(int | float, value) if type(value) in (int, float) else None


def _target(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        # Reflection accepts model-like diagnostic inputs; output remains allowlisted.
        value = value.to_dict()
    if not isinstance(value, dict):
        return None
    clean: dict[str, object] = {}
    for key in ("position", "percentage", "value", "brightness", "white", "color_temp_kelvin"):
        if _number(value.get(key)) is not None:
            clean[key] = value[key]
    if isinstance(value.get("on"), bool):
        clean["on"] = value["on"]
    if value.get("direction") in {"forward", "reverse"}:
        clean["direction"] = value["direction"]
    if value.get("option") is not None:
        clean["option"] = _identifier(value["option"])
    for key in ("hs_color", "xy_color", "rgb_color", "rgbw_color", "rgbww_color"):
        color = value.get(key)
        if isinstance(color, (list, tuple)) and all(_number(item) is not None for item in color):
            clean[key] = list(color)
    if value.get("effect") is not None:
        clean["effect"] = _identifier(value["effect"])
    if value.get("profile") is not None:
        clean["profile"] = _identifier(value["profile"])
    return clean


def _status(value: object) -> str:
    return value if isinstance(value, str) and value in _STATUSES else "unknown"


def _condition_error(result: dict[str, Any]) -> bool:
    return bool(result["error"] or any(item["error"] for item in result["conditions"]))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OperatorConfigEntry
) -> dict[str, object]:
    """Export structural evidence only; never call devices or alter runtime state."""
    runtime: OperatorRuntime | None = getattr(entry, "runtime_data", None)
    if runtime is None or runtime._closed:
        issues = ir.async_get(hass)
        return {
            "version": 1,
            "runtime_state": "absent" if runtime is None else "closed",
            "faulted": bool(runtime and runtime.fault)
            or any(
                issues.async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}") is not None
                for kind in ("storage", "configuration")
            ),
            "subentries": {
                kind: len(entry.get_subentries_of_type(kind))
                for kind in ("resource", "policy", "requirement", "intent")
            },
            "resources": {},
            "intents": {},
            "policies": {},
            "requirements": {},
        }
    resources: dict[str, dict[str, object]] = {}
    for identifier, config in runtime.resources.items():
        observation = runtime.observations.get(identifier)
        decision = runtime.decisions.get(identifier)
        lease = runtime.manual(identifier)
        command = runtime.last_commands.get(identifier)
        resources[_identifier(identifier)] = {
            "kind": config["kind"]
            if config["kind"]
            in {"cover", "fan", "relay_fan", "switch", "number", "select", "light"}
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
        if "return_monitor" in config:
            monitor = runtime.return_monitor(identifier)
            resources[_identifier(identifier)]["return_monitor"] = {
                "phase": monitor.phase,
                "target_position": _number(monitor.target_position),
                "due_at": _number(monitor.due_at),
                "overdue": monitor.overdue,
            }
        if details := runtime.condition_details(identifier, published=True):
            resources[_identifier(identifier)]["command_conditions"] = {
                command: {
                    "status": result["status"],
                    "evaluation_error": _condition_error(result),
                    "evaluated_condition_count": len(result["conditions"]),
                }
                for command, result in details["command_conditions"].items()
            }
    requirements: dict[str, dict[str, object]] = {}
    for identifier in runtime.requirements:
        result = runtime.requirement_results.get(identifier)
        requirements[_identifier(identifier)] = {
            "status": _status(result.status if result else None),
            "selected_provider": _identifier(result.selected_provider) if result else None,
            "acquiring_provider": _identifier(result.acquiring_provider) if result else None,
        }
    return {
        "version": 1,
        "faulted": bool(runtime.fault),
        "shadow_locked": bool(runtime.shadow_locked),
        "resources": resources,
        "intents": {
            _identifier(key): {"desired": runtime.desired_value(key)} for key in runtime.intents
        },
        "policies": {
            _identifier(key): {
                "enabled": bool(runtime.policy_enabled(key)),
            }
            for key, config in runtime.policies.items()
        },
        "requirements": requirements,
    }
