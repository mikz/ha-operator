"""Structural and admission safety checks for user-configured device owners."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.ha_operator.configuration import (
    ConfigurationError,
    resource_outputs,
    validate_configuration,
    validate_policy,
    validate_requirement,
    validate_resource,
    validate_target,
)

COVER = {"name": "Window", "kind": "cover", "entity_id": "cover.raw"}
SWITCH = {"name": "Outlet", "kind": "switch", "entity_id": "switch.raw"}
FAN = {
    "name": "Native fan",
    "kind": "fan",
    "entity_id": "fan.raw",
    "default_target": {"on": True, "percentage": 50, "direction": "forward"},
}
RELAY = {
    "name": "Relay fan",
    "kind": "relay_fan",
    "outputs": ["switch.low", "switch.high"],
    "profiles": {
        "off": {"outputs": {"switch.low": False, "switch.high": False}},
        "low": {
            "outputs": {"switch.low": True, "switch.high": False},
            "percentage": 50,
            "direction": "forward",
        },
        "high": {
            "outputs": {"switch.low": False, "switch.high": True},
            "percentage": 100,
            "direction": "forward",
        },
    },
    "reversal_dead_time": 2,
    "default_target": "low",
}
POLICY = {"name": "Morning", "resource_id": "r", "kind": "state", "target": {"position": 50}}
NUMERIC_INPUT = {
    "type": "qualified_numeric",
    "entity_id": "sensor.temperature",
    "comparison": "below",
    "threshold": 16,
    "unit": "°C",
    "qualification_seconds": 1800,
}
TIMER_INPUT = {
    "type": "timer_episode",
    "entity_id": "timer.ventilation",
    "qualification_seconds": 60,
    "request_seconds": 1800,
}
RETURN_MONITOR = {"target_at_most": 7, "warning_after_seconds": 300}
REQUIREMENT = {
    "name": "Airflow",
    "activation_entities": ["binary_sensor.extract"],
    "providers": [
        {
            "id": "window",
            "resource_id": "r",
            "target": {"position": 30},
            "evidence": [
                {
                    "entity_id": "cover.raw",
                    "attribute": "current_position",
                    "kind": "position",
                    "operator": "gte",
                    "value": 25,
                }
            ],
        }
    ],
}


def entry(kind, key, data):
    return SimpleNamespace(subentry_type=kind, subentry_id=key, data=data)


def test_defaults_are_normalized_without_changing_input():
    data = validate_resource(COVER)
    assert data["retry_interval"] == 300
    assert data["command_interval"] == 30
    assert data["movement_timeout"] == 120
    assert data["tolerance"] == 2
    assert data["manual_duration"] == 1800
    assert "retry_interval" not in COVER
    relay = validate_resource(RELAY)
    assert relay["default_target"] == {"profile": "low"}
    assert relay["profiles"]["off"]["outputs"]["switch.low"] is False
    assert resource_outputs(relay) == {"switch.low", "switch.high"}
    assert validate_resource(SWITCH)["kind"] == "switch"
    assert validate_resource(FAN)["default_target"]["percentage"] == 50
    assert validate_target({"on": False}, SWITCH) == {"on": False}


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": 1},
        {"name": " "},
        {"kind": "light"},
        {"entity_id": "fan.wrong"},
        {"entity_id": "not an id"},
        {"retry_interval": 0},
        {"command_interval": True},
        {"movement_timeout": float("inf")},
        {"manual_duration": "30"},
        {"tolerance": 101},
        {"outputs": ["switch.one"]},
        {"default_target": {"position": -1}},
        {"fault_entity": "missing"},
    ],
)
def test_invalid_resources(patch):
    with pytest.raises(ConfigurationError):
        validate_resource(COVER | patch)


@pytest.mark.parametrize("value", [None, [], "x", 0])
def test_resources_must_be_objects(value):
    with pytest.raises(ConfigurationError):
        validate_resource(value)


@pytest.mark.parametrize(
    "target", [{}, {"position": None}, {"position": 101}, {"on": True}, {"other": 0}]
)
def test_cover_rejects_unrepresentable_targets(target):
    with pytest.raises(ConfigurationError):
        validate_target(target, COVER)


@pytest.mark.parametrize("target", [{"on": "off"}, {"direction": "inward"}, {"percentage": False}])
def test_fan_rejects_unrepresentable_targets(target):
    with pytest.raises(ConfigurationError):
        validate_target(target, FAN)


def test_unknown_relay_profile_and_missing_native_default():
    with pytest.raises(ConfigurationError):
        validate_target({"profile": "missing"}, RELAY)
    with pytest.raises(ConfigurationError):
        validate_resource({key: value for key, value in FAN.items() if key != "default_target"})


@pytest.mark.parametrize(
    "patch",
    [
        {"entity_id": "fan.invalid"},
        {"outputs": []},
        {"outputs": ["switch.low", "switch.low"]},
        {"profiles": {}},
        {"profiles": []},
        {"reversal_dead_time": 0},
        {"default_target": "off"},
        {"default_target": {"on": True}},
        {"profiles": {"a": {}, "b": {}}},
        {
            "profiles": {
                "off": {"outputs": {"switch.low": False, "switch.high": False}},
                "on": {"outputs": {"switch.low": "on", "switch.high": False}},
            }
        },
        {
            "profiles": {
                "a": {"outputs": {"switch.low": True, "switch.high": False}},
                "b": {"outputs": {"switch.low": False, "switch.high": True}},
            }
        },
    ],
)
def test_invalid_relay_profiles(patch):
    with pytest.raises(ConfigurationError):
        validate_resource(RELAY | patch)


def test_relay_profile_validation_and_output_ownership():
    relay = deepcopy(RELAY)
    relay["profiles"]["low"]["direction"] = "inward"
    with pytest.raises(ConfigurationError):
        validate_resource(relay)
    relay["profiles"]["low"]["direction"] = "forward"
    relay["profiles"]["low"]["percentage"] = 110
    with pytest.raises(ConfigurationError):
        validate_resource(relay)
    with pytest.raises(ConfigurationError, match="already owned"):
        validate_resource(RELAY, resources={"switch": SWITCH | {"entity_id": "switch.low"}})
    assert validate_resource(COVER, resource_id="r", resources={"r": COVER})["name"] == "Window"


async def test_native_capability_and_managed_source_rejection(hass):
    hass.states.async_set("cover.raw", "closed", {"supported_features": 4})
    hass.states.async_set("binary_sensor.rain", "off")
    data = validate_resource(COVER | {"restriction_entity": "binary_sensor.rain"}, hass)
    assert data["restriction_entity"] == "binary_sensor.rain"
    hass.states.async_set("cover.raw", "closed", {"supported_features": 0})
    with pytest.raises(ConfigurationError, match="position control"):
        validate_resource(COVER, hass)
    hass.states.async_set("cover.raw", "closed", {"supported_features": 4, "assumed_state": True})
    with pytest.raises(ConfigurationError, match="position control"):
        validate_resource(COVER, hass)
    hass.states.async_set("fan.raw", "off", {"supported_features": 5})
    assert validate_resource(FAN, hass)["kind"] == "fan"
    hass.states.async_set("fan.raw", "off", {"supported_features": 0})
    with pytest.raises(ConfigurationError, match="unsupported capabilities"):
        validate_resource(FAN, hass)
    managed = er.async_get(hass).async_get_or_create("cover", "ha_operator", "owned")
    hass.states.async_set(managed.entity_id, "open", {"supported_features": 4})
    with pytest.raises(ConfigurationError, match="managed operator"):
        validate_resource(COVER | {"entity_id": managed.entity_id}, hass)


def test_policy_defaults_dynamic_target_and_occurrences():
    assert validate_policy(POLICY, {"r": COVER})["priority"] == 0
    dynamic = {
        "name": "Dynamic",
        "resource_id": "r",
        "kind": "state",
        "target_entity": "schedule.morning",
        "target_attribute": "position",
        "eligibility_entity": "schedule.morning",
    }
    assert validate_policy(dynamic, {"r": COVER})["eligibility_state"] == "on"
    assert validate_policy(dynamic, {"r": COVER})["target_field"] == "position"
    assert (
        validate_policy(POLICY | {"kind": "occurrence", "priority": -5}, {"r": COVER})["priority"]
        == -5
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"resource_id": "missing"},
        {"kind": "unknown"},
        {"priority": 0.5},
        {"eligibility_state": "on"},
        {"target_entity": "sensor.target", "target_field": "profile"},
        {"target_entity": "sensor.target", "target_field": "on"},
        {"target_attribute": "position"},
        {"target": None},
    ],
)
def test_invalid_policies(patch):
    with pytest.raises(ConfigurationError):
        validate_policy(POLICY | patch, {"r": COVER})


def test_policy_requires_target():
    with pytest.raises(ConfigurationError):
        validate_policy(
            {key: value for key, value in POLICY.items() if key != "target"}, {"r": COVER}
        )


def test_requirement_evidence_and_passive_provider():
    normalized = validate_requirement(REQUIREMENT, {"r": COVER})
    assert normalized["acquisition_timeout"] == 120
    assert normalized["providers"][0]["evidence"][0]["value"] == 25.0
    passive = deepcopy(REQUIREMENT)
    passive["providers"][0] = {
        "id": "passive",
        "evidence": [
            {
                "entity_id": "binary_sensor.contact",
                "kind": "contact",
                "operator": "eq",
                "value": "on",
            }
        ],
    }
    assert validate_requirement(passive, {})["providers"][0]["id"] == "passive"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(providers=[]),
        lambda d: d["providers"].append(deepcopy(d["providers"][0])),
        lambda d: d["providers"][0].update(resource_id="missing"),
        lambda d: d["providers"][0].pop("resource_id"),
        lambda d: d["providers"][0].update(evidence=[]),
        lambda d: d["providers"][0]["evidence"][0].update(kind="desired"),
        lambda d: d["providers"][0]["evidence"][0].update(operator="ne"),
        lambda d: d["providers"][0]["evidence"][0].update(value=None),
        lambda d: d["providers"][0]["evidence"][0].update(attribute="desired_position"),
        lambda d: d["providers"][0]["evidence"][0].update(entity_id="sensor.target"),
    ],
)
def test_invalid_requirements(mutate):
    data = deepcopy(REQUIREMENT)
    mutate(data)
    with pytest.raises(ConfigurationError):
        validate_requirement(data, {"r": COVER})


def test_full_configuration_order_ids_and_overlap():
    entries = [
        entry("policy", "p", POLICY),
        entry("requirement", "air", REQUIREMENT),
        entry("resource", "r", COVER),
    ]
    normalized = validate_configuration({item.subentry_id: item for item in entries})
    assert normalized["resources"]["r"]["name"] == "Window"
    assert normalized["policies"]["p"]["resource_id"] == "r"
    with pytest.raises(ConfigurationError, match="multiple airflow"):
        validate_configuration([*entries, entry("requirement", "other", REQUIREMENT)])
    with pytest.raises(ConfigurationError, match="unsupported subentry"):
        validate_configuration([entry("unknown", "bad", {})])


@pytest.mark.parametrize("data", [{0: True}, COVER | {"name": ""}])
def test_nontext_keys_and_empty_names(data):
    with pytest.raises(ConfigurationError):
        validate_resource(data)


def test_native_state_lengths_and_threshold_finiteness():
    relay = deepcopy(RELAY)
    relay["profiles"]["x" * 256] = relay["profiles"].pop("low")
    with pytest.raises(ConfigurationError, match="255"):
        validate_resource(relay)
    requirement = deepcopy(REQUIREMENT)
    requirement["providers"][0]["id"] = "x" * 256
    with pytest.raises(ConfigurationError, match="255"):
        validate_requirement(requirement, {"r": COVER})
    requirement["providers"][0]["id"] = "window"
    requirement["providers"][0]["evidence"][0].update(operator="eq", value=float("nan"))
    with pytest.raises(ConfigurationError, match="finite"):
        validate_requirement(requirement, {"r": COVER})


async def test_admission_rejects_optimistic_evidence_and_unsupported_policy(hass):
    hass.states.async_set("binary_sensor.extract", "on")
    hass.states.async_set("cover.raw", "open", {"current_position": 100, "assumed_state": True})
    with pytest.raises(ConfigurationError, match="optimistic"):
        validate_requirement(REQUIREMENT, {"r": COVER}, hass)
    hass.states.async_set("fan.raw", "off", {"supported_features": 0})
    with pytest.raises(ConfigurationError, match="unsupported capabilities"):
        validate_policy(POLICY | {"target": {"percentage": 50}}, {"r": FAN}, hass)
    with pytest.raises(ConfigurationError, match="unsupported capabilities"):
        validate_target({"direction": "reverse"}, FAN, hass)


def test_dynamic_target_can_read_the_entity_state():
    policy = {
        "name": "Position",
        "resource_id": "r",
        "kind": "state",
        "target_entity": "sensor.position",
    }
    assert validate_policy(policy, {"r": COVER})["target_field"] == "position"


def test_optional_native_settings_preserve_existing_configuration():
    old = POLICY | {"eligibility_entity": "input_boolean.ready"}
    original = deepcopy(old)
    assert validate_policy(old, {"r": COVER}) == original | {
        "priority": 0,
        "eligibility_state": "on",
    }
    assert old == original
    assert "input" not in validate_policy(POLICY, {"r": COVER})
    assert "return_monitor" not in validate_resource(COVER)


@pytest.mark.parametrize("kind,source", [("state", NUMERIC_INPUT), ("occurrence", TIMER_INPUT)])
def test_native_input_round_trip_and_independent_copy(kind, source):
    policy = POLICY | {"kind": kind, "input": deepcopy(source)}
    original = deepcopy(policy)
    result = validate_configuration(
        [
            entry("resource", "r", COVER | {"return_monitor": RETURN_MONITOR}),
            entry("policy", "p", policy),
        ]
    )
    assert result["policies"]["p"]["input"] == source
    assert result["resources"]["r"]["return_monitor"] == RETURN_MONITOR
    result["policies"]["p"]["input"]["qualification_seconds"] = 1
    assert policy == original


@pytest.mark.parametrize("value", [None, [], "sensor.temperature", {}, {"type": "other"}])
def test_invalid_input_shape_has_specific_error(value):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"input": value}, {"r": COVER})
    assert error.value.code == "invalid_policy_input"


@pytest.mark.parametrize(
    "patch",
    [
        {"comparison": "above"},
        {"comparison": None},
        {"threshold": None},
        {"threshold": True},
        {"threshold": "16"},
        {"threshold": float("nan")},
        {"threshold": float("inf")},
        {"unit": None},
        {"unit": " "},
        {"request_seconds": 30},
        {"extra": 1},
    ],
)
def test_invalid_numeric_input_fields(patch):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"input": NUMERIC_INPUT | patch}, {"r": COVER})
    assert error.value.code == "invalid_policy_input"


@pytest.mark.parametrize(
    "kind,source,field",
    [
        ("state", NUMERIC_INPUT, "qualification_seconds"),
        ("occurrence", TIMER_INPUT, "qualification_seconds"),
        ("occurrence", TIMER_INPUT, "request_seconds"),
    ],
)
@pytest.mark.parametrize("value", [None, 0, -1, True, "60", float("nan"), float("inf")])
def test_input_durations_must_be_finite_positive_numbers(kind, source, field, value):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"kind": kind, "input": source | {field: value}}, {"r": COVER})
    assert error.value.code == "invalid_policy_input"
    assert field in error.value.detail


@pytest.mark.parametrize("kind,source", [("occurrence", NUMERIC_INPUT), ("state", TIMER_INPUT)])
def test_input_requires_matching_policy_kind(kind, source):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"kind": kind, "input": source}, {"r": COVER})
    assert error.value.code == "invalid_policy_input"


@pytest.mark.parametrize(
    "field,value", [("comparison", "below"), ("threshold", 16), ("unit", "°C")]
)
def test_timer_rejects_numeric_settings(field, value):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(
            POLICY | {"kind": "occurrence", "input": TIMER_INPUT | {field: value}}, {"r": COVER}
        )
    assert error.value.code == "invalid_policy_input"


@pytest.mark.parametrize("kind,source", [("state", NUMERIC_INPUT), ("occurrence", TIMER_INPUT)])
@pytest.mark.parametrize("entity_id", ["input_number.source", "binary_sensor.source", "bad", None])
def test_native_input_requires_correct_source_domain(kind, source, entity_id):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(
            POLICY | {"kind": kind, "input": source | {"entity_id": entity_id}}, {"r": COVER}
        )
    assert error.value.code == "invalid_input_source"


@pytest.mark.parametrize("kind,source", [("state", NUMERIC_INPUT), ("occurrence", TIMER_INPUT)])
@pytest.mark.parametrize(
    "legacy", [{"eligibility_entity": "input_boolean.ready"}, {"eligibility_state": "on"}]
)
def test_native_input_replaces_helper_eligibility(kind, source, legacy):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"kind": kind, "input": source} | legacy, {"r": COVER})
    assert error.value.code == "input_eligibility_conflict"


@pytest.mark.parametrize("source_state", ["15.5", "-10", "unknown", "unavailable"])
async def test_numeric_input_accepts_matching_native_units_and_outages(hass, source_state):
    hass.states.async_set("sensor.temperature", source_state, {"unit_of_measurement": "°C"})
    policy = validate_policy(POLICY | {"input": NUMERIC_INPUT}, {"r": COVER}, hass)
    assert policy["input"] == NUMERIC_INPUT


@pytest.mark.parametrize("unit", [None, "°F", "K", "C"])
async def test_numeric_input_rejects_missing_or_different_native_unit(hass, unit):
    hass.states.async_set("sensor.temperature", "unknown", {"unit_of_measurement": unit})
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"input": NUMERIC_INPUT}, {"r": COVER}, hass)
    assert error.value.code == "input_unit_mismatch"


@pytest.mark.parametrize("source_state", ["cold", "nan", "inf", "-inf"])
async def test_numeric_input_rejects_usable_nonnumeric_or_nonfinite_sources(hass, source_state):
    hass.states.async_set("sensor.temperature", source_state, {"unit_of_measurement": "°C"})
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"input": NUMERIC_INPUT}, {"r": COVER}, hass)
    assert error.value.code == "invalid_input_value"


@pytest.mark.parametrize("kind,source", [("state", NUMERIC_INPUT), ("occurrence", TIMER_INPUT)])
async def test_native_input_rejects_missing_and_managed_sources(hass, kind, source):
    with pytest.raises(ConfigurationError) as error:
        validate_policy(POLICY | {"kind": kind, "input": source}, {"r": COVER}, hass)
    assert error.value.code == "entity_not_found"
    domain = source["entity_id"].split(".")[0]
    registered = er.async_get(hass).async_get_or_create(domain, "ha_operator", "owned_input")
    hass.states.async_set(registered.entity_id, "unknown", {"unit_of_measurement": "°C"})
    with pytest.raises(ConfigurationError) as error:
        validate_policy(
            POLICY | {"kind": kind, "input": source | {"entity_id": registered.entity_id}},
            {"r": COVER},
            hass,
        )
    assert error.value.code == "managed_entity"


async def test_native_inputs_keep_target_capability_validation(hass):
    hass.states.async_set("sensor.temperature", "15", {"unit_of_measurement": "°C"})
    hass.states.async_set("timer.ventilation", "idle")
    hass.states.async_set("fan.raw", "off", {"supported_features": 0})
    for kind, source in (("state", NUMERIC_INPUT), ("occurrence", TIMER_INPUT)):
        with pytest.raises(ConfigurationError) as error:
            validate_policy(
                POLICY | {"kind": kind, "target": {"percentage": 50}, "input": source},
                {"r": FAN},
                hass,
            )
        assert error.value.code == "unsupported_capability"
    assert (
        validate_policy(POLICY | {"kind": "occurrence", "input": TIMER_INPUT}, {"r": COVER}, hass)[
            "input"
        ]
        == TIMER_INPUT
    )


def test_native_input_retains_dynamic_targets_and_strict_positive_durations():
    policy = {key: value for key, value in POLICY.items() if key != "target"}
    policy.update(target_entity="sensor.position", input=NUMERIC_INPUT | {"threshold": -10})
    assert validate_policy(policy, {"r": COVER})["target_field"] == "position"
    assert (
        validate_policy(
            POLICY | {"input": NUMERIC_INPUT | {"qualification_seconds": 0.0001}}, {"r": COVER}
        )["input"]["qualification_seconds"]
        == 0.0001
    )


@pytest.mark.parametrize("resource", [SWITCH, FAN, RELAY])
def test_return_monitor_requires_cover(resource):
    with pytest.raises(ConfigurationError) as error:
        validate_resource(resource | {"return_monitor": RETURN_MONITOR})
    assert error.value.code == "invalid_return_monitor"


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"target_at_most": 7},
        {"warning_after_seconds": 300},
        RETURN_MONITOR | {"target_at_most": -1},
        RETURN_MONITOR | {"target_at_most": 101},
        RETURN_MONITOR | {"target_at_most": True},
        RETURN_MONITOR | {"target_at_most": float("nan")},
        RETURN_MONITOR | {"warning_after_seconds": 0},
        RETURN_MONITOR | {"warning_after_seconds": -1},
        RETURN_MONITOR | {"warning_after_seconds": True},
        RETURN_MONITOR | {"warning_after_seconds": "300"},
        RETURN_MONITOR | {"warning_after_seconds": float("inf")},
        RETURN_MONITOR | {"extra": 1},
    ],
)
def test_invalid_return_monitor_has_specific_error(value):
    with pytest.raises(ConfigurationError) as error:
        validate_resource(COVER | {"return_monitor": value})
    assert error.value.code == "invalid_return_monitor"


@pytest.mark.parametrize("position", [0, 100])
def test_return_monitor_accepts_position_boundaries(position):
    assert (
        validate_resource(
            COVER | {"return_monitor": RETURN_MONITOR | {"target_at_most": position}}
        )["return_monitor"]["target_at_most"]
        == position
    )


async def test_return_monitor_requires_native_position_control(hass):
    hass.states.async_set("cover.raw", "open", {"supported_features": 0})
    with pytest.raises(ConfigurationError) as error:
        validate_resource(COVER | {"return_monitor": RETURN_MONITOR}, hass)
    assert error.value.code == "unsupported_capability"
