"""Validate operator configuration before admitting an actuator owner.

The structural validators deliberately accept plain dictionaries and can run without
Home Assistant. Supplying ``hass`` additionally checks native entities/capabilities.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from .const import DOMAIN
from .intents import validate_graph

RESOURCE_DEFAULTS = {
    "retry_interval": 300.0,
    "command_interval": 30.0,
    "movement_timeout": 120.0,
    "tolerance": 2.0,
    "manual_duration": 1800.0,
}
KINDS = ("cover", "switch", "fan", "relay_fan")
TARGET_FIELDS = {"position", "on", "percentage", "direction", "profile"}
_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class ConfigurationError(ValueError):
    """Configuration rejected with a translated flow error and useful detail."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _fail(detail: str, code: str = "invalid_configuration") -> None:
    raise ConfigurationError(code, detail)


def _object(value: Any, field: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        _fail(f"{field} keys must be text")
    if set(value) - allowed:
        _fail(f"{field} has unknown fields: {sorted(set(value) - allowed)}")
    return deepcopy(dict(value))


def _text(value: Any, field: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be nonempty text")
    value = value.strip()
    if maximum is not None and len(value) > maximum:
        _fail(f"{field} must contain at most {maximum} characters")
    return value


def _number(value: Any, field: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{field} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        _fail(f"{field} must be between {minimum} and {maximum or 'a finite maximum'}")
    return value


def _entity(value: Any, field: str, domain: str | None = None, hass: Any = None) -> str:
    value = _text(value, field)
    if not _ENTITY_ID.fullmatch(value) or (domain and value.split(".")[0] != domain):
        _fail(f"{field} must name a {domain or 'valid'} entity")
    if hass is not None:
        from homeassistant.helpers import entity_registry as er

        registered = er.async_get(hass).async_get(value)
        if registered is not None and registered.platform == DOMAIN:
            _fail(
                f"{field} must reference a source entity, not a managed operator entity",
                "managed_entity",
            )
        if hass.states.get(value) is None:
            _fail(f"{field}: {value} does not exist", "entity_not_found")
    return value


def _entities(value: Any, field: str, domain: str | None = None, hass: Any = None) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail(f"{field} must contain at least one entity")
    result = [_entity(item, field, domain, hass) for item in value]
    if len(set(result)) != len(result):
        _fail(f"{field} contains duplicates")
    return result


def validate_target(value: Any, resource: Mapping[str, Any], hass: Any = None) -> dict[str, Any]:
    """Return a normalized target that this resource can represent."""
    data = _object(value, "target", TARGET_FIELDS)
    if not data or any(item is None for item in data.values()):
        _fail("target must contain non-null fields")
    kind = resource["kind"]
    allowed = {
        "cover": {"position"},
        "switch": {"on"},
        "fan": {"on", "percentage", "direction"},
        "relay_fan": {"on", "percentage", "direction", "profile"},
    }[kind]
    if set(data) - allowed:
        _fail(f"target fields are not supported by {kind}")
    for field in ("position", "percentage"):
        if field in data:
            data[field] = _number(data[field], field, 0, 100)
    if "on" in data and not isinstance(data["on"], bool):
        _fail("on must be true or false")
    if "direction" in data and data["direction"] not in ("forward", "reverse"):
        _fail("direction must be forward or reverse")
    if "profile" in data:
        data["profile"] = _text(data["profile"], "profile", 255)
        if data["profile"] not in resource.get("profiles", {}):
            _fail("target names an unknown relay profile")
    if hass is not None and kind == "fan":
        state = hass.states.get(resource["entity_id"])
        features = state.attributes.get("supported_features", 0) if state else 0
        if ("percentage" in data and not features & 1) or (
            "direction" in data and not features & 4
        ):
            _fail("fan target uses unsupported capabilities", "unsupported_capability")
    return data


def resource_outputs(resource: Mapping[str, Any]) -> set[str]:
    """Return every output owned by a resource."""
    return set(resource["outputs"]) if resource["kind"] == "relay_fan" else {resource["entity_id"]}


def validate_resource(
    value: Any,
    hass: Any = None,
    *,
    resource_id: str | None = None,
    resources: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a resource, native capabilities, and exclusive output ownership."""
    data = _object(
        value,
        "resource",
        {
            "name",
            "kind",
            "entity_id",
            "outputs",
            "profiles",
            "reversal_dead_time",
            "restriction_entity",
            "fault_entity",
            "default_target",
            "manual_control",
            *RESOURCE_DEFAULTS,
        },
    )
    data["name"] = _text(data.get("name"), "name")
    if data.get("kind") not in KINDS:
        _fail("kind must be cover, switch, fan, or relay_fan")
    kind = data["kind"]
    if "manual_control" in data and type(data["manual_control"]) is not bool:
        _fail("manual_control must be true or false")
    for field, default in RESOURCE_DEFAULTS.items():
        data[field] = _number(
            data.get(field, default),
            field,
            0 if field == "tolerance" else 0.001,
            100 if field == "tolerance" else None,
        )
    for field in ("restriction_entity", "fault_entity"):
        if field in data:
            data[field] = _entity(data[field], field, hass=hass)
    if kind == "relay_fan":
        if "entity_id" in data:
            _fail("relay_fan owns outputs; remove entity_id")
        data["outputs"] = _entities(data.get("outputs"), "outputs", "switch", hass)
        data["reversal_dead_time"] = _number(
            data.get("reversal_dead_time"), "reversal_dead_time", 0.001, 3600
        )
        profiles = data.get("profiles")
        if not isinstance(profiles, Mapping) or len(profiles) < 2:
            _fail("profiles must include an all-off profile and an on profile")
        normalized = {}
        for name, profile in profiles.items():
            name = _text(name, "profile name", 255)
            item = _object(profile, "profile", {"outputs", "percentage", "direction"})
            outputs = item.get("outputs")
            if not isinstance(outputs, Mapping) or set(outputs) != set(data["outputs"]):
                _fail("every profile must specify every owned output")
            if any(not isinstance(state, bool) for state in outputs.values()):
                _fail("profile output values must be true or false")
            if "percentage" in item:
                item["percentage"] = _number(item["percentage"], "percentage", 0, 100)
            if "direction" in item and item["direction"] not in ("forward", "reverse"):
                _fail("profile direction must be forward or reverse")
            normalized[name] = item
        if not any(not any(profile["outputs"].values()) for profile in normalized.values()):
            _fail("an all-off profile is required")
        data["profiles"] = normalized
        default = data.get("default_target")
        if isinstance(default, str):
            default = {"profile": default}
        default = validate_target(default, data)
        profile_name = default.get("profile")
        if profile_name not in normalized or not any(normalized[profile_name]["outputs"].values()):
            _fail("default_target must name an on profile")
        data["default_target"] = default
    else:
        if any(key in data for key in ("outputs", "profiles", "reversal_dead_time")):
            _fail("relay settings are only valid for relay_fan")
        data["entity_id"] = _entity(data.get("entity_id"), "entity_id", kind, hass)
        if "default_target" in data:
            data["default_target"] = validate_target(data["default_target"], data, hass)
        elif kind == "fan":
            _fail("native fan requires a configured default_target")
        if hass is not None:
            state = hass.states.get(data["entity_id"])
            features = state.attributes.get("supported_features", 0)
            if kind == "cover" and (
                not features & 4 or state.attributes.get("assumed_state", False)
            ):
                _fail("cover requires non-optimistic position control", "unsupported_capability")
    for other_id, other in (resources or {}).items():
        if other_id != resource_id and resource_outputs(data) & resource_outputs(other):
            _fail("an output is already owned by another resource", "duplicate_output")
    return data


def validate_policy(
    value: Any,
    resources: Mapping[str, Mapping[str, Any]],
    hass: Any = None,
    *,
    intents: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a policy and its native state or occurrence target."""
    data = _object(
        value,
        "policy",
        {
            "name",
            "resource_id",
            "kind",
            "priority",
            "target",
            "eligibility_entity",
            "eligibility_state",
            "target_entity",
            "target_attribute",
            "target_field",
            "intent_id",
        },
    )
    data["name"] = _text(data.get("name"), "name")
    data["resource_id"] = _text(data.get("resource_id"), "resource_id")
    if data["resource_id"] not in resources:
        _fail("policy must reference an existing resource", "resource_not_found")
    resource = resources[data["resource_id"]]
    if data.get("kind") not in ("state", "occurrence"):
        _fail("policy kind must be state or occurrence")
    priority = _number(data.get("priority", 0), "priority", -1000000, 1000000)
    if not priority.is_integer():
        _fail("priority must be an integer")
    data["priority"] = int(priority)
    if "target" in data:
        data["target"] = validate_target(data["target"], resource, hass)
    if "eligibility_entity" in data:
        data["eligibility_entity"] = _entity(
            data["eligibility_entity"], "eligibility_entity", hass=hass
        )
        data["eligibility_state"] = _text(data.get("eligibility_state", "on"), "eligibility_state")
    elif "eligibility_state" in data:
        _fail("eligibility_state requires eligibility_entity")
    if "intent_id" in data:
        if (
            data["kind"] != "state"
            or resource["kind"] != "switch"
            or any(
                key in data
                for key in ("target", "target_entity", "target_attribute", "target_field")
            )
        ):
            _fail("A desired control supplies the complete target of a state-backed switch policy")
        data["intent_id"] = _text(data["intent_id"], "intent_id")
        if data["intent_id"] not in (intents or {}):
            _fail("intent_id must reference a configured desired control")
    elif "target_entity" in data:
        data["target_entity"] = _entity(data["target_entity"], "target_entity", hass=hass)
        field = data["target_field"] = _text(data.get("target_field", "position"), "target_field")
        probe = {"on": True, "position": 0, "percentage": 0, "direction": "forward"}
        if field not in probe:
            _fail("target_field must be position, on, percentage, or direction")
        validate_target({field: probe[field]}, resource, hass)
        if "target_attribute" in data:
            data["target_attribute"] = _text(data["target_attribute"], "target_attribute")
    elif "target" not in data or "target_attribute" in data or "target_field" in data:
        _fail("provide target or target_entity; target attributes require target_entity")
    return data


def validate_intent(value: Any) -> dict[str, Any]:
    """A logical desired switch with explicit initial state and optional ON edges."""
    data = _object(value, "desired control", {"name", "initial_value", "on_targets"})
    data["name"] = _text(data.get("name"), "name")
    if type(data.get("initial_value")) is not bool:
        _fail("initial_value must explicitly be true or false")
    targets = data.get("on_targets", [])
    if not isinstance(targets, list):
        _fail("on_targets must be a list of desired control IDs")
    data["on_targets"] = [_text(key, "on target") for key in targets]
    if len(set(data["on_targets"])) != len(data["on_targets"]):
        _fail("on_targets must not contain duplicates")
    return data


def validate_requirement(
    value: Any, resources: Mapping[str, Mapping[str, Any]], hass: Any = None
) -> dict[str, Any]:
    """Validate explicit activation, provider targets, and independent evidence."""
    data = _object(
        value, "requirement", {"name", "activation_entities", "providers", "acquisition_timeout"}
    )
    data["name"] = _text(data.get("name"), "name")
    data["activation_entities"] = _entities(
        data.get("activation_entities"), "activation_entities", hass=hass
    )
    data["acquisition_timeout"] = _number(
        data.get("acquisition_timeout", 120), "acquisition_timeout", 0.001
    )
    if not isinstance(data.get("providers"), list) or not data["providers"]:
        _fail("providers must be a nonempty ordered list")
    provider_ids: set[str] = set()
    for provider in data["providers"]:
        item = _object(provider, "provider", {"id", "resource_id", "target", "evidence"})
        provider.clear()
        provider.update(item)
        provider["id"] = _text(provider.get("id"), "provider id", 255)
        if provider["id"] in provider_ids:
            _fail("provider ids must be unique")
        provider_ids.add(provider["id"])
        if "resource_id" in provider:
            provider["resource_id"] = _text(provider["resource_id"], "resource_id")
            if provider["resource_id"] not in resources:
                _fail("provider must reference an existing resource", "resource_not_found")
            provider["target"] = validate_target(
                provider.get("target"), resources[provider["resource_id"]], hass
            )
        elif "target" in provider:
            _fail("passive providers cannot have actuator targets")
        evidence = provider.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            _fail("provider requires independent confirmation evidence")
        for index, predicate in enumerate(evidence):
            predicate = _object(
                predicate, "evidence", {"entity_id", "attribute", "operator", "value", "kind"}
            )
            evidence[index] = predicate
            predicate["entity_id"] = _entity(
                predicate.get("entity_id"), "evidence entity_id", hass=hass
            )
            if predicate.get("kind") not in ("position", "contact", "relay", "airflow"):
                _fail("evidence kind must be position, contact, relay, or airflow")
            if predicate.get("operator") not in ("eq", "gte", "lte"):
                _fail("evidence operator must be eq, gte, or lte")
            if "attribute" in predicate:
                predicate["attribute"] = _text(predicate["attribute"], "evidence attribute")
            if "value" not in predicate or not isinstance(
                predicate["value"], (str, int, float, bool)
            ):
                _fail("evidence value must be a scalar")
            if isinstance(predicate["value"], float) and not math.isfinite(predicate["value"]):
                _fail("evidence values must be finite")
            if predicate["operator"] != "eq":
                predicate["value"] = _number(
                    predicate["value"], "evidence threshold", -1e100, 1e100
                )
            if predicate["kind"] == "position" and (
                not predicate["entity_id"].startswith("cover.")
                or predicate.get("attribute") != "current_position"
            ):
                _fail("position evidence must use a raw cover current_position attribute")
            if (
                hass is not None
                and predicate["kind"] == "position"
                and hass.states.get(predicate["entity_id"]).attributes.get("assumed_state", False)
            ):
                _fail("position evidence cannot use optimistic state", "unsupported_capability")
    return data


def validate_configuration(
    subentries: Mapping[str, Any] | Iterable[Any], hass: Any = None
) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate all subentries together; IDs always come from native subentry IDs."""
    entries = list(subentries.values() if isinstance(subentries, Mapping) else subentries)
    result: dict[str, dict[str, dict[str, Any]]] = {
        "resources": {},
        "policies": {},
        "requirements": {},
        "intents": {},
    }
    for entry in entries:
        if entry.subentry_type not in ("resource", "policy", "requirement", "intent"):
            _fail("unsupported subentry type")
        if entry.subentry_type == "resource":
            result["resources"][entry.subentry_id] = validate_resource(
                entry.data, hass, resource_id=entry.subentry_id, resources=result["resources"]
            )
        elif entry.subentry_type == "intent":
            result["intents"][entry.subentry_id] = validate_intent(entry.data)
    try:
        validate_graph(result["intents"])
    except ValueError as err:
        _fail(str(err))
    claimed: set[str] = set()
    for entry in entries:
        if entry.subentry_type == "policy":
            result["policies"][entry.subentry_id] = validate_policy(
                entry.data, result["resources"], hass, intents=result["intents"]
            )
        elif entry.subentry_type == "requirement":
            requirement = validate_requirement(entry.data, result["resources"], hass)
            controlled = {
                provider["resource_id"]
                for provider in requirement["providers"]
                if "resource_id" in provider
            }
            if claimed & controlled:
                _fail(
                    "a resource cannot belong to multiple airflow control groups",
                    "overlapping_requirement",
                )
            claimed.update(controlled)
            result["requirements"][entry.subentry_id] = requirement
    return result
