"""Native translated presentation and semantic public error boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from custom_components.ha_operator import runtime as runtime_module
from custom_components.ha_operator import sensor
from custom_components.ha_operator.const import DOMAIN

from .helpers import async_activate, async_add_physical_cover, async_setup_operator, managed_id


def test_negative_oversized_runtime_number_uses_native_semantic_validation():
    with pytest.raises(ServiceValidationError) as raised:
        runtime_module._number(-(10**399), "duration")
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "finite_number"
    assert raised.value.translation_placeholders == {"field": "duration"}


@pytest.mark.parametrize("key", ["expiry", "effective_expiry", "next_attempt", "input_deadline"])
@pytest.mark.parametrize("stamp", [None, 0, 1.8e9, 1e100, 1.8e12])
def test_timestamp_properties_preserve_numeric_deadlines_and_representable_dates(key, stamp):
    runtime = SimpleNamespace(
        resources={"roof": {"name": "Roof", "kind": "cover"}},
        policies={},
        requirements={},
        intents={},
        manual=lambda _: SimpleNamespace(expires_at=stamp),
        selections={"roof": SimpleNamespace(expires_at=stamp)},
        next_attempts={"roof": stamp},
        policy_input=lambda _: SimpleNamespace(due_at=stamp),
    )
    entity = (
        sensor.PolicyInputSensor(runtime, "roof", key)
        if key == "input_deadline"
        else sensor.ResourceSensor(runtime, "roof", key)
    )
    expected = datetime.fromtimestamp(stamp, UTC) if stamp in (0, 1.8e9) else None
    assert entity.native_value == expected
    assert runtime.manual("roof").expires_at == stamp
    assert runtime.selections["roof"].expires_at == stamp
    assert runtime.next_attempts["roof"] == stamp
    assert runtime.policy_input("roof").due_at == stamp


@pytest.mark.parametrize("error", [OverflowError, ValueError, OSError])
def test_timestamp_projection_handles_only_date_representability_errors(error):
    with patch.object(sensor, "datetime") as converter:
        converter.fromtimestamp.side_effect = error("unrepresentable date")
        assert sensor._timestamp(1) is None
        converter.fromtimestamp.assert_called_once_with(1, UTC)
    with patch.object(sensor, "datetime") as converter:
        converter.fromtimestamp.side_effect = TypeError("programming error")
        with pytest.raises(TypeError, match="programming error"):
            sensor._timestamp(1)


async def test_native_translated_names_enum_icons_and_user_override(hass, tmp_path):
    await async_add_physical_cover(hass)
    hass.states.async_set("binary_sensor.extractor", "on")
    hass.states.async_set("binary_sensor.inlet", "off")
    entry = await async_setup_operator(
        hass,
        tmp_path,
        requirements={
            "air": {
                "name": "User airflow name",
                "activation_entities": ["binary_sensor.extractor"],
                "providers": [
                    {
                        "id": "User inlet name",
                        "evidence": [
                            {
                                "entity_id": "binary_sensor.inlet",
                                "kind": "contact",
                                "operator": "eq",
                                "value": "on",
                            }
                        ],
                    }
                ],
            }
        },
    )
    registry = er.async_get(hass)
    desired = managed_id(hass, "sensor", key="desired")
    primary = managed_id(hass, "cover")
    assert registry.async_get(desired).original_name == "Desired target"
    assert registry.async_get(primary).original_name is None
    assert hass.states.get(primary).attributes["friendly_name"] == "Roof"
    assert hass.states.get(desired).attributes["friendly_name"] == "Roof Desired target"
    status = hass.states.get(managed_id(hass, "sensor", key="status"))
    assert status.attributes["device_class"] == "enum"
    assert set(status.attributes["options"]) == {
        "initializing",
        "fault",
        "hands_off",
        "idle",
        "observe",
        "unavailable",
        "restricted",
        "satisfied",
        "pending",
        "applying",
        "waiting",
    }
    unmet = hass.states.get(managed_id(hass, "binary_sensor", "air", "unmet"))
    assert unmet.attributes["device_class"] == BinarySensorDeviceClass.PROBLEM
    assert unmet.state == "on"
    provider = hass.states.get(managed_id(hass, "sensor", "air", "provider"))
    assert provider.state == "unknown"  # No provider is invented to fill a label.
    translations = await async_get_translations(hass, "en", "entity", {DOMAIN})
    assert (
        translations[f"component.{DOMAIN}.entity.sensor.control_status.state.waiting"] == "Waiting"
    )
    assert translations[f"component.{DOMAIN}.entity.select.mode.state.live"] == "Live"
    registry.async_update_entity(desired, name="User custom desired", icon="mdi:star")
    disabled = managed_id(hass, "sensor", key="attempts")
    registry.async_update_entity(disabled, disabled_by=er.RegistryEntryDisabler.USER)
    before = {
        item.unique_id: (item.entity_id, item.device_id, item.name, item.icon, item.disabled_by)
        for item in registry.entities.values()
        if item.config_entry_id == entry.entry_id
    }
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after = {
        item.unique_id: (item.entity_id, item.device_id, item.name, item.icon, item.disabled_by)
        for item in registry.entities.values()
        if item.config_entry_id == entry.entry_id
    }
    assert after == before
    assert hass.states.get(desired).attributes["friendly_name"] == "User custom desired"
    assert hass.states.get(desired).attributes["icon"] == "mdi:star"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "target,key,placeholders",
    [
        ({"position": float("nan")}, "finite_number", {"field": "position"}),
        ({"position": 101}, "target_range", {"field": "position", "maximum": "100"}),
        ({"on": "on"}, "boolean_target", {"field": "on"}),
        ({"direction": ""}, "string_target", {"field": "direction"}),
        ({"postion": 40}, "unsupported_target_fields", {"fields": "postion"}),
        ({"on": True}, "unsupported_target_fields", {"fields": "on"}),
    ],
)
async def test_native_request_errors_are_semantic_and_do_not_commit(
    hass, tmp_path, target, key, placeholders
):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    before = entry.runtime_data.store.state
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN, "request", {"resource_id": "roof", "target": target}, blocking=True
        )
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == key
    assert error.value.translation_placeholders == (placeholders or {}) or (
        placeholders is None and error.value.translation_placeholders is None
    )
    assert entry.runtime_data.store.state == before and raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unexpected_adapter_programming_error_is_not_reclassified(hass, tmp_path):
    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    with patch.object(
        entry.runtime_data.adapter("roof"),
        "normalize",
        side_effect=ValueError("programming defect"),
    ):
        with pytest.raises(ValueError, match="programming defect") as error:
            await hass.services.async_call(
                DOMAIN,
                "request",
                {"resource_id": "roof", "target": {"position": 30}},
                blocking=True,
            )
    assert not isinstance(error.value, ServiceValidationError)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_invalid_fan_percentage_step_action_is_expected_native_error(hass, tmp_path):
    hass.states.async_set("fan.raw", "off", {"supported_features": 49, "percentage_step": 0})
    entry = await async_setup_operator(
        hass,
        tmp_path,
        resources={
            "roof": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 50},
            }
        },
    )
    await async_activate(hass)
    before = entry.runtime_data.store.state
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN, "request", {"resource_id": "roof", "target": {"percentage": 50}}, blocking=True
        )
    assert error.value.translation_key == "invalid_percentage_step"
    assert entry.runtime_data.store.state == before
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_labels_states_and_semantic_icons_are_packaged_without_native_icon_overrides():
    component = Path(sensor.__file__).parent
    strings = json.loads((component / "strings.json").read_text())
    translated = json.loads((component / "translations/en.json").read_text())
    assert strings["entity"] == translated["entity"]
    icons = json.loads((component / "icons.json").read_text())["entity"]
    for platform, entries in icons.items():
        for key, icon in entries.items():
            assert key in strings["entity"][platform]
            assert icon["default"].startswith("mdi:")
    assert not {"unmet", "return_overdue"} & icons["binary_sensor"].keys()
    assert (
        not {"expiry", "effective_expiry", "next_attempt", "qualification_due"}
        & icons["sensor"].keys()
    )
    assert "cover" not in icons and "fan" not in icons


async def test_saved_relay_profile_error_is_semantic_setup_error_and_repair(hass, tmp_path):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import issue_registry as ir

    from .helpers import operator_entry

    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(
        resources={
            "relay": {
                "name": "Relay",
                "kind": "relay_fan",
                "outputs": ["switch.a"],
                "profiles": {
                    "off": {"outputs": {"switch.a": False}},
                    "on": {"outputs": {"switch.a": True}},
                    "duplicate": {"outputs": {"switch.a": True}},
                },
                "default_target": {"profile": "on"},
                "reversal_dead_time": 1,
            }
        }
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(entry, "runtime_data")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"configuration_{entry.entry_id}")
    assert issue.translation_key == "config_relay_profile_signatures"
    assert issue.translation_placeholders == {}
    assert not list(tmp_path.glob("ha_operator.*.json"))
    translations = await async_get_translations(hass, "en", "issues", {DOMAIN})
    assert (
        "distinct output combinations"
        in translations[f"component.{DOMAIN}.issues.{issue.translation_key}.description"]
    )
    assert (
        "reload" in translations[f"component.{DOMAIN}.issues.{issue.translation_key}.description"]
    )


async def test_programming_failure_in_intent_validation_is_not_a_configuration_repair(
    hass, tmp_path
):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ha_operator import async_setup_entry

    from .helpers import operator_entry

    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(resources={})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ha_operator.configuration.validate_graph",
        side_effect=ValueError("programming defect"),
    ):
        with pytest.raises(ValueError, match="programming defect"):
            await async_setup_entry(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"configuration_{entry.entry_id}") is None
