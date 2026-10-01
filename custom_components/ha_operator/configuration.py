"""Validate operator configuration before admitting an actuator owner.

The structural validators deliberately accept plain dictionaries and can run without
Home Assistant. Supplying ``hass`` additionally checks native entities/capabilities.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, NoReturn, cast

from .const import DOMAIN
from .data import (
    IntentConfig,
    OperatorConfiguration,
    PolicyConfig,
    PolicyInputConfig,
    RequirementConfig,
    ResourceConfig,
    ReturnMonitorConfig,
    TargetData,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant, State
from .intents import IntentValidationError, validate_graph

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

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        translation_key: str,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.translation_key = translation_key
        self.translation_placeholders = placeholders or {}
        super().__init__(detail)


def _fail(
    detail: str,
    code: str = "invalid_configuration",
    *,
    translation_key: str,
    placeholders: dict[str, str] | None = None,
) -> NoReturn:
    raise ConfigurationError(
        code, detail, translation_key=translation_key, placeholders=placeholders
    )


def _object(value: Any, field: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(
            f"{field} must be an object",
            translation_key="config_object_expected",
            placeholders={"field": str(field)},
        )
    if any(not isinstance(key, str) for key in value):
        _fail(
            f"{field} keys must be text",
            translation_key="config_text_keys",
            placeholders={"field": str(field)},
        )
    if set(value) - allowed:
        _fail(
            f"{field} has unknown fields: {sorted(set(value) - allowed)}",
            translation_key="config_unknown_fields",
            placeholders={"field": str(field), "value": str(sorted(set(value) - allowed))},
        )
    return deepcopy(dict(value))


def _text(value: object, field: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(
            f"{field} must be nonempty text",
            translation_key="config_text_required",
            placeholders={"field": str(field)},
        )
    value = value.strip()
    if maximum is not None and len(value) > maximum:
        _fail(
            f"{field} must contain at most {maximum} characters",
            translation_key="config_text_length",
            placeholders={"field": str(field), "maximum": str(maximum)},
        )
    return value


def _number(value: object, field: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(
            f"{field} must be a number",
            translation_key="config_number_required",
            placeholders={"field": str(field)},
        )
    value = float(value)
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        detail = f"{field} must be between {minimum} and {maximum or 'a finite maximum'}"
        if maximum is None:
            _fail(
                detail,
                translation_key="config_number_minimum",
                placeholders={"field": field, "minimum": str(minimum)},
            )
        _fail(
            detail,
            translation_key="config_number_bounds",
            placeholders={"field": field, "minimum": str(minimum), "maximum": str(maximum)},
        )
    return value


def _entity(
    value: object, field: str, domain: str | None = None, hass: HomeAssistant | None = None
) -> str:
    value = _text(value, field)
    if not _ENTITY_ID.fullmatch(value) or (domain and value.split(".")[0] != domain):
        detail = f"{field} must name a {domain or 'valid'} entity"
        if domain is None:
            _fail(detail, translation_key="config_entity_expected", placeholders={"field": field})
        _fail(
            detail,
            translation_key="config_entity_domain",
            placeholders={"field": field, "domain": domain},
        )
    if hass is not None:
        from homeassistant.helpers import entity_registry as er

        registered = er.async_get(hass).async_get(value)
        if registered is not None and registered.platform == DOMAIN:
            _fail(
                f"{field} must reference a source entity, not a managed operator entity",
                "managed_entity",
                translation_key="config_must_reference_a_source_entity_not_a_managed_operator_entity",
                placeholders={"field": str(field)},
            )
        _entity_state(hass, value, field)
    return value


def _entity_state(hass: HomeAssistant, entity_id: str, field: str) -> State:
    """Return the existing source checked by native configuration admission."""
    state = hass.states.get(entity_id)
    if state is None:
        _fail(
            f"{field}: {entity_id} does not exist",
            "entity_not_found",
            translation_key="config_entity_not_found",
            placeholders={"field": field, "value": entity_id},
        )
    return state


def _entities(
    value: object, field: str, domain: str | None = None, hass: HomeAssistant | None = None
) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail(
            f"{field} must contain at least one entity",
            translation_key="config_must_contain_at_least_one_entity",
            placeholders={"field": str(field)},
        )
    result = [_entity(item, field, domain, hass) for item in value]
    if len(set(result)) != len(result):
        _fail(
            f"{field} contains duplicates",
            translation_key="config_contains_duplicates",
            placeholders={"field": str(field)},
        )
    return result


def validate_target(
    value: Any, resource: Mapping[str, Any], hass: HomeAssistant | None = None
) -> TargetData:
    """Return a normalized target that this resource can represent."""
    data = _object(value, "target", TARGET_FIELDS)
    if not data or any(item is None for item in data.values()):
        _fail(
            "target must contain non-null fields",
            translation_key="config_target_must_contain_non_null_fields",
        )
    kind = resource["kind"]
    allowed = {
        "cover": {"position"},
        "switch": {"on"},
        "fan": {"on", "percentage", "direction"},
        "relay_fan": {"on", "percentage", "direction", "profile"},
    }[kind]
    if set(data) - allowed:
        _fail(
            f"target fields are not supported by {kind}",
            translation_key="config_target_fields_are_not_supported_by",
            placeholders={"kind": str(kind)},
        )
    for field in ("position", "percentage"):
        if field in data:
            data[field] = _number(data[field], field, 0, 100)
    if "on" in data and not isinstance(data["on"], bool):
        _fail("on must be true or false", translation_key="config_on_must_be_true_or_false")
    if "direction" in data and data["direction"] not in ("forward", "reverse"):
        _fail(
            "direction must be forward or reverse",
            translation_key="config_direction_must_be_forward_or_reverse",
        )
    if "profile" in data:
        data["profile"] = _text(data["profile"], "profile", 255)
        if data["profile"] not in resource.get("profiles", {}):
            _fail(
                "target names an unknown relay profile",
                translation_key="config_target_names_an_unknown_relay_profile",
            )
    if hass is not None and kind == "fan":
        state = hass.states.get(resource["entity_id"])
        features = state.attributes.get("supported_features", 0) if state else 0
        if ("percentage" in data and not features & 1) or (
            "direction" in data and not features & 4
        ):
            _fail(
                "fan target uses unsupported capabilities",
                "unsupported_capability",
                translation_key="config_fan_target_uses_unsupported_capabilities",
            )
    return cast(TargetData, data)


def resource_outputs(resource: Mapping[str, Any]) -> set[str]:
    """Return every output owned by a resource."""
    return set(resource["outputs"]) if resource["kind"] == "relay_fan" else {resource["entity_id"]}


def _positive_duration(value: Any, field: str) -> float:
    duration = _number(value, field, 0)
    if duration == 0:
        _fail(
            f"{field} must be positive",
            translation_key="config_must_be_positive",
            placeholders={"field": str(field)},
        )
    return duration


def _policy_input(value: Any, kind: str, hass: HomeAssistant | None = None) -> PolicyInputConfig:
    """Validate the two bounded native inputs without reading helper state."""
    try:
        data = _object(
            value,
            "input",
            {
                "type",
                "entity_id",
                "comparison",
                "threshold",
                "unit",
                "qualification_seconds",
                "request_seconds",
            },
        )
        input_type = data.get("type")
        if input_type not in ("qualified_numeric", "timer_episode"):
            _fail(
                "input.type must be qualified_numeric or timer_episode",
                translation_key="config_input_type_must_be_qualified_numeric_or_timer_episode",
            )
        expected_kind = "state" if input_type == "qualified_numeric" else "occurrence"
        if kind != expected_kind:
            _fail(
                f"{input_type} input requires a {expected_kind} policy",
                translation_key="config_input_policy_kind",
                placeholders={"input_type": str(input_type), "expected_kind": str(expected_kind)},
            )
        domain = "sensor" if input_type == "qualified_numeric" else "timer"
        try:
            data["entity_id"] = _entity(data.get("entity_id"), "input.entity_id", domain, hass)
        except ConfigurationError as err:
            if err.code == "invalid_configuration":
                _fail(
                    err.detail,
                    "invalid_input_source",
                    translation_key=err.translation_key,
                    placeholders=err.translation_placeholders,
                )
            raise
        data["qualification_seconds"] = _positive_duration(
            data.get("qualification_seconds"), "input.qualification_seconds"
        )
        if input_type == "timer_episode":
            if any(key in data for key in ("comparison", "threshold", "unit")):
                _fail(
                    "timer_episode input cannot contain numeric comparison settings",
                    translation_key="config_timer_episode_input_cannot_contain_numeric_comparison_settings",
                )
            data["request_seconds"] = _positive_duration(
                data.get("request_seconds"), "input.request_seconds"
            )
        else:
            if "request_seconds" in data:
                _fail(
                    "qualified_numeric input cannot contain request_seconds",
                    translation_key="config_qualified_numeric_input_cannot_contain_request_seconds",
                )
            if data.get("comparison") != "below":
                _fail(
                    "input.comparison must be below",
                    translation_key="config_input_comparison_must_be_below",
                )
            data["threshold"] = _number(data.get("threshold"), "input.threshold", -math.inf)
            data["unit"] = _text(data.get("unit"), "input.unit")
            if hass is not None:
                state = _entity_state(hass, data["entity_id"], "input.entity_id")
                if state.attributes.get("unit_of_measurement") != data["unit"]:
                    _fail(
                        "input.unit must match the sensor unit",
                        "input_unit_mismatch",
                        translation_key="config_input_unit_must_match_the_sensor_unit",
                    )
                if state.state not in ("unknown", "unavailable"):
                    try:
                        numeric = float(state.state)
                    except TypeError, ValueError:
                        _fail(
                            "input source must report a finite number",
                            "invalid_input_value",
                            translation_key="config_input_source_must_report_a_finite_number",
                        )
                    if not math.isfinite(numeric):
                        _fail(
                            "input source must report a finite number",
                            "invalid_input_value",
                            translation_key="config_input_source_must_report_a_finite_number",
                        )
        return cast(PolicyInputConfig, data)
    except ConfigurationError as err:
        if err.code == "invalid_configuration":
            _fail(
                err.detail,
                "invalid_policy_input",
                translation_key=err.translation_key,
                placeholders=err.translation_placeholders,
            )
        raise


def _return_monitor(value: Any, kind: str) -> ReturnMonitorConfig:
    try:
        if kind != "cover":
            _fail(
                "return_monitor requires a cover resource",
                translation_key="config_return_monitor_requires_a_cover_resource",
            )
        data = _object(value, "return_monitor", {"target_at_most", "warning_after_seconds"})
        data["target_at_most"] = _number(
            data.get("target_at_most"), "return_monitor.target_at_most", 0, 100
        )
        data["warning_after_seconds"] = _positive_duration(
            data.get("warning_after_seconds"), "return_monitor.warning_after_seconds"
        )
        return cast(ReturnMonitorConfig, data)
    except ConfigurationError as err:
        _fail(
            err.detail,
            "invalid_return_monitor",
            translation_key=err.translation_key,
            placeholders=err.translation_placeholders,
        )


def validate_resource(
    value: Any,
    hass: HomeAssistant | None = None,
    *,
    resource_id: str | None = None,
    resources: Mapping[str, ResourceConfig] | None = None,
) -> ResourceConfig:
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
            "return_monitor",
            *RESOURCE_DEFAULTS,
        },
    )
    data["name"] = _text(data.get("name"), "name")
    if data.get("kind") not in KINDS:
        _fail(
            "kind must be cover, switch, fan, or relay_fan",
            translation_key="config_kind_must_be_cover_switch_fan_or_relay_fan",
        )
    kind = data["kind"]
    if "return_monitor" in data:
        data["return_monitor"] = _return_monitor(data["return_monitor"], kind)
    if "manual_control" in data and type(data["manual_control"]) is not bool:
        _fail(
            "manual_control must be true or false",
            translation_key="config_manual_control_must_be_true_or_false",
        )
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
            _fail(
                "relay_fan owns outputs; remove entity_id",
                translation_key="config_relay_fan_owns_outputs_remove_entity_id",
            )
        data["outputs"] = _entities(data.get("outputs"), "outputs", "switch", hass)
        data["reversal_dead_time"] = _number(
            data.get("reversal_dead_time"), "reversal_dead_time", 0.001, 3600
        )
        profiles = data.get("profiles")
        if not isinstance(profiles, Mapping) or len(profiles) < 2:
            _fail(
                "profiles must include an all-off profile and an on profile",
                translation_key="config_profiles_must_include_an_all_off_profile_and_an_on_profile",
            )
        normalized = {}
        signatures: set[tuple[bool, ...]] = set()
        for name, profile in profiles.items():
            name = _text(name, "profile name", 255)
            item = _object(profile, "profile", {"outputs", "percentage", "direction"})
            outputs = item.get("outputs")
            if not isinstance(outputs, Mapping) or set(outputs) != set(data["outputs"]):
                _fail(
                    "every profile must specify every owned output",
                    translation_key="config_every_profile_must_specify_every_owned_output",
                )
            if any(not isinstance(state, bool) for state in outputs.values()):
                _fail(
                    "profile output values must be true or false",
                    translation_key="config_profile_output_values_must_be_true_or_false",
                )
            signature = tuple(outputs[output] for output in data["outputs"])
            if signature in signatures:
                _fail(
                    "Relay profiles must have distinct output combinations",
                    translation_key="config_relay_profile_signatures",
                )
            signatures.add(signature)
            if "percentage" in item:
                item["percentage"] = _number(item["percentage"], "percentage", 0, 100)
                if any(outputs.values()) != (item["percentage"] > 0):
                    _fail(
                        "Relay profile speed must agree with its on/off outputs",
                        translation_key="config_relay_profile_speed",
                    )
            if "direction" in item and item["direction"] not in ("forward", "reverse"):
                _fail(
                    "profile direction must be forward or reverse",
                    translation_key="config_profile_direction_must_be_forward_or_reverse",
                )
            normalized[name] = item
        if not any(not any(profile["outputs"].values()) for profile in normalized.values()):
            _fail(
                "an all-off profile is required",
                translation_key="config_an_all_off_profile_is_required",
            )
        data["profiles"] = normalized
        relay_default = data.get("default_target")
        if isinstance(relay_default, str):
            relay_default = {"profile": relay_default}
        default_target = validate_target(relay_default, data)
        profile_name = default_target.get("profile")
        if profile_name not in normalized or not any(normalized[profile_name]["outputs"].values()):
            _fail(
                "default_target must name an on profile",
                translation_key="config_default_target_must_name_an_on_profile",
            )
        data["default_target"] = default_target
    else:
        if any(key in data for key in ("outputs", "profiles", "reversal_dead_time")):
            _fail(
                "relay settings are only valid for relay_fan",
                translation_key="config_relay_settings_are_only_valid_for_relay_fan",
            )
        data["entity_id"] = _entity(data.get("entity_id"), "entity_id", kind, hass)
        if "default_target" in data:
            data["default_target"] = validate_target(data["default_target"], data, hass)
        elif kind == "fan":
            _fail(
                "native fan requires a configured default_target",
                translation_key="config_native_fan_requires_a_configured_default_target",
            )
        if hass is not None:
            state = _entity_state(hass, data["entity_id"], "entity_id")
            features = state.attributes.get("supported_features", 0)
            if kind == "cover" and (
                not features & 4 or state.attributes.get("assumed_state", False)
            ):
                _fail(
                    "cover requires non-optimistic position control",
                    "unsupported_capability",
                    translation_key="config_cover_requires_non_optimistic_position_control",
                )
    for other_id, other in (resources or {}).items():
        if other_id != resource_id and resource_outputs(data) & resource_outputs(other):
            _fail(
                "an output is already owned by another resource",
                "duplicate_output",
                translation_key="config_an_output_is_already_owned_by_another_resource",
            )
    return cast(ResourceConfig, data)


def validate_policy(
    value: Any,
    resources: Mapping[str, ResourceConfig],
    hass: HomeAssistant | None = None,
    *,
    intents: Mapping[str, IntentConfig] | None = None,
) -> PolicyConfig:
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
            "input",
        },
    )
    data["name"] = _text(data.get("name"), "name")
    data["resource_id"] = _text(data.get("resource_id"), "resource_id")
    if data["resource_id"] not in resources:
        _fail(
            "policy must reference an existing resource",
            "resource_not_found",
            translation_key="config_policy_must_reference_an_existing_resource",
        )
    resource = resources[data["resource_id"]]
    if data.get("kind") not in ("state", "occurrence"):
        _fail(
            "policy kind must be state or occurrence",
            translation_key="config_policy_kind_must_be_state_or_occurrence",
        )
    if "input" in data:
        if "eligibility_entity" in data or "eligibility_state" in data:
            _fail(
                "input replaces eligibility_entity and eligibility_state",
                "input_eligibility_conflict",
                translation_key="config_input_replaces_eligibility_entity_and_eligibility_state",
            )
        data["input"] = _policy_input(data["input"], data["kind"], hass)
    priority = _number(data.get("priority", 0), "priority", -1000000, 1000000)
    if not priority.is_integer():
        _fail("priority must be an integer", translation_key="config_priority_must_be_an_integer")
    data["priority"] = int(priority)
    if "target" in data:
        data["target"] = validate_target(data["target"], resource, hass)
    if "eligibility_entity" in data:
        data["eligibility_entity"] = _entity(
            data["eligibility_entity"], "eligibility_entity", hass=hass
        )
        data["eligibility_state"] = _text(data.get("eligibility_state", "on"), "eligibility_state")
    elif "eligibility_state" in data:
        _fail(
            "eligibility_state requires eligibility_entity",
            translation_key="config_eligibility_state_requires_eligibility_entity",
        )
    if "intent_id" in data:
        if (
            data["kind"] != "state"
            or resource["kind"] != "switch"
            or any(
                key in data
                for key in ("target", "target_entity", "target_attribute", "target_field")
            )
        ):
            _fail(
                "A desired control supplies the complete target of a state-backed switch policy",
                translation_key="config_a_desired_control_supplies_the_complete_target_of_a_state_backed_switch_policy",
            )
        data["intent_id"] = _text(data["intent_id"], "intent_id")
        if data["intent_id"] not in (intents or {}):
            _fail(
                "intent_id must reference a configured desired control",
                translation_key="config_intent_id_must_reference_a_configured_desired_control",
            )
    elif "target_entity" in data:
        data["target_entity"] = _entity(data["target_entity"], "target_entity", hass=hass)
        field = data["target_field"] = _text(data.get("target_field", "position"), "target_field")
        probe = {"on": True, "position": 0, "percentage": 0, "direction": "forward"}
        if field not in probe:
            _fail(
                "target_field must be position, on, percentage, or direction",
                translation_key="config_target_field_must_be_position_on_percentage_or_direction",
            )
        validate_target({field: probe[field]}, resource, hass)
        if "target_attribute" in data:
            data["target_attribute"] = _text(data["target_attribute"], "target_attribute")
    elif "target" not in data or "target_attribute" in data or "target_field" in data:
        _fail(
            "provide target or target_entity; target attributes require target_entity",
            translation_key="config_provide_target_or_target_entity_target_attributes_require_target_entity",
        )
    return cast(PolicyConfig, data)


def validate_intent(value: Any) -> IntentConfig:
    """A logical desired switch with explicit initial state and optional ON edges."""
    data = _object(value, "desired control", {"name", "initial_value", "on_targets"})
    data["name"] = _text(data.get("name"), "name")
    if type(data.get("initial_value")) is not bool:
        _fail(
            "initial_value must explicitly be true or false",
            translation_key="config_initial_value_must_explicitly_be_true_or_false",
        )
    targets = data.get("on_targets", [])
    if not isinstance(targets, list):
        _fail(
            "on_targets must be a list of desired control IDs",
            translation_key="config_on_targets_must_be_a_list_of_desired_control_ids",
        )
    data["on_targets"] = [_text(key, "on target") for key in targets]
    if len(set(data["on_targets"])) != len(data["on_targets"]):
        _fail(
            "on_targets must not contain duplicates",
            translation_key="config_on_targets_must_not_contain_duplicates",
        )
    # All three fields have been normalized and validated above.
    return cast(IntentConfig, data)


def validate_requirement(
    value: Any, resources: Mapping[str, ResourceConfig], hass: HomeAssistant | None = None
) -> RequirementConfig:
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
        _fail(
            "providers must be a nonempty ordered list",
            translation_key="config_providers_must_be_a_nonempty_ordered_list",
        )
    provider_ids: set[str] = set()
    for provider in data["providers"]:
        item = _object(provider, "provider", {"id", "resource_id", "target", "evidence"})
        provider.clear()
        provider.update(item)
        provider["id"] = _text(provider.get("id"), "provider id", 255)
        if provider["id"] in provider_ids:
            _fail(
                "provider ids must be unique", translation_key="config_provider_ids_must_be_unique"
            )
        provider_ids.add(provider["id"])
        if "resource_id" in provider:
            provider["resource_id"] = _text(provider["resource_id"], "resource_id")
            if provider["resource_id"] not in resources:
                _fail(
                    "provider must reference an existing resource",
                    "resource_not_found",
                    translation_key="config_provider_must_reference_an_existing_resource",
                )
            provider["target"] = validate_target(
                provider.get("target"), resources[provider["resource_id"]], hass
            )
        elif "target" in provider:
            _fail(
                "passive providers cannot have actuator targets",
                translation_key="config_passive_providers_cannot_have_actuator_targets",
            )
        evidence = provider.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            _fail(
                "provider requires independent confirmation evidence",
                translation_key="config_provider_requires_independent_confirmation_evidence",
            )
        for index, predicate in enumerate(evidence):
            predicate = _object(
                predicate, "evidence", {"entity_id", "attribute", "operator", "value", "kind"}
            )
            evidence[index] = predicate
            predicate["entity_id"] = _entity(
                predicate.get("entity_id"), "evidence entity_id", hass=hass
            )
            if predicate.get("kind") not in ("position", "contact", "relay", "airflow"):
                _fail(
                    "evidence kind must be position, contact, relay, or airflow",
                    translation_key="config_evidence_kind_must_be_position_contact_relay_or_airflow",
                )
            if predicate.get("operator") not in ("eq", "gte", "lte"):
                _fail(
                    "evidence operator must be eq, gte, or lte",
                    translation_key="config_evidence_operator_must_be_eq_gte_or_lte",
                )
            if "attribute" in predicate:
                predicate["attribute"] = _text(predicate["attribute"], "evidence attribute")
            if "value" not in predicate or not isinstance(
                predicate["value"], (str, int, float, bool)
            ):
                _fail(
                    "evidence value must be a scalar",
                    translation_key="config_evidence_value_must_be_a_scalar",
                )
            if isinstance(predicate["value"], float) and not math.isfinite(predicate["value"]):
                _fail(
                    "evidence values must be finite",
                    translation_key="config_evidence_values_must_be_finite",
                )
            if predicate["operator"] != "eq":
                predicate["value"] = _number(
                    predicate["value"], "evidence threshold", -1e100, 1e100
                )
            if predicate["kind"] == "position" and (
                not predicate["entity_id"].startswith("cover.")
                or predicate.get("attribute") != "current_position"
            ):
                _fail(
                    "position evidence must use a raw cover current_position attribute",
                    translation_key="config_position_evidence_must_use_a_raw_cover_current_position_attribute",
                )
            if (
                hass is not None
                and predicate["kind"] == "position"
                and _entity_state(
                    hass, predicate["entity_id"], "evidence entity_id"
                ).attributes.get("assumed_state", False)
            ):
                _fail(
                    "position evidence cannot use optimistic state",
                    "unsupported_capability",
                    translation_key="config_position_evidence_cannot_use_optimistic_state",
                )
    return cast(RequirementConfig, data)


def validate_configuration(
    subentries: Mapping[str, ConfigSubentry] | Iterable[ConfigSubentry],
    hass: HomeAssistant | None = None,
) -> OperatorConfiguration:
    """Validate all subentries together; IDs always come from native subentry IDs."""
    entries = list(subentries.values() if isinstance(subentries, Mapping) else subentries)
    result: OperatorConfiguration = {
        "resources": {},
        "policies": {},
        "requirements": {},
        "intents": {},
    }
    for entry in entries:
        if entry.subentry_type not in ("resource", "policy", "requirement", "intent"):
            _fail("unsupported subentry type", translation_key="config_unsupported_subentry_type")
        if entry.subentry_type == "resource":
            result["resources"][entry.subentry_id] = validate_resource(
                entry.data, hass, resource_id=entry.subentry_id, resources=result["resources"]
            )
        elif entry.subentry_type == "intent":
            result["intents"][entry.subentry_id] = validate_intent(entry.data)
    try:
        validate_graph(result["intents"])
    except IntentValidationError as err:
        _fail(
            str(err), translation_key=err.translation_key, placeholders=err.translation_placeholders
        )
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
                    translation_key="config_a_resource_cannot_belong_to_multiple_airflow_control_groups",
                )
            claimed.update(controlled)
            result["requirements"][entry.subentry_id] = requirement
    return result
