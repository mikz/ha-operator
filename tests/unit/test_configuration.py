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
