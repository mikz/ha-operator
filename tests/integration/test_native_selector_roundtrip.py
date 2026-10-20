"""Native visual-editor configuration survives real flow validation and compilation."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import json
from copy import deepcopy

import probatio
import pytest
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator.const import DOMAIN
from tests.integration.test_config_flow import _resource, _start, native_sources  # noqa: F401

CONDITIONS = [
    {"condition": "state", "entity_id": "binary_sensor.inlet", "state": ["on"]},
    {
        "condition": "or",
        "enabled": "{{ true }}",
        "conditions": [
            {"condition": "time", "after": "06:00:00", "enabled": False},
            {"condition": "template", "value_template": "{{ true }}"},
        ],
    },
]
TRIGGERS = [
    {"trigger": "event", "event_type": "editor_admit", "id": "event"},
    {"trigger": "state", "entity_id": "binary_sensor.inlet", "to": "on", "for": {"seconds": 60}},
    {"trigger": "template", "value_template": "{{ false }}", "enabled": False},
    {"trigger": "time", "at": "06:00:00", "enabled": "{{ false }}"},
]


async def test_native_policy_editor_input_survives_save_reopen_and_runtime(
    hass, tmp_path, native_sources
):
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    fields = probatio.to_field_list(form["data_schema"], custom_serializer=cv.custom_serializer)
    for field, kind in (
        ("conditions", "condition"),
        ("triggers", "trigger"),
        ("cancel_triggers", "trigger"),
    ):
        assert next(item for item in fields if item["name"] == field)["selector"] == {kind: {}}
    data = {
        "name": "Native editor",
        "kind": "occurrence",
        "resource_id": resource.subentry_id,
        "target": {"position": 0},
        "conditions": deepcopy(CONDITIONS),
        "triggers": deepcopy(TRIGGERS),
        "cancel_triggers": [{"trigger": "event", "event_type": "editor_cancel"}],
        "duration": {"minutes": 2},
    }
    original = deepcopy(data)
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] == "create_entry"
    policy = next(iter(entry.get_subentries_of_type("policy")))
    for field in ("conditions", "triggers", "cancel_triggers", "duration"):
        assert json.loads(json.dumps(dict(policy.data)))[field] == original[field]
    assert data == original
    for _ in range(2):
        edit = await entry.start_subentry_reconfigure_flow(hass, policy.subentry_id)
        for field in ("conditions", "triggers", "cancel_triggers"):
            key = next(key for key in edit["data_schema"].schema if key == field)
            assert key.description["suggested_value"] == original[field]
        saved = await hass.config_entries.subentries.async_configure(edit["flow_id"], data)
        assert saved["reason"] == "reconfigure_successful"
    before = json.dumps(dict(entry.subentries[policy.subentry_id].data), sort_keys=True)
    hass.config.config_dir = str(tmp_path)
    assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    await runtime.async_set_mode(resource.subentry_id, "live")
    await hass.async_block_till_done()
    assert runtime._trigger_owners[policy.subentry_id].ready
    assert runtime._policy_checks[policy.subentry_id].setup_error is None
    assert json.dumps(dict(entry.subentries[policy.subentry_id].data), sort_keys=True) == before
    hass.bus.async_fire("editor_admit")
    await hass.async_block_till_done()
    assert len(runtime._state["occurrences"]) == 1
    hass.bus.async_fire("editor_cancel")
    await hass.async_block_till_done()
    assert not runtime._state["occurrences"]
    assert json.dumps(dict(entry.subentries[policy.subentry_id].data), sort_keys=True) == before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_nested_command_condition_selector_preserves_native_editor_shape(
    hass, tmp_path, native_sources
):
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    fields = {key.schema: value for key, value in form["data_schema"].schema.items()}
    command = fields["command_conditions"]
    assert (
        type(command.config["fields"]["set_cover_position"]["selector"])
        is selector.ConditionSelector
    )
    data = {
        "name": "Native guard",
        "kind": "cover",
        "entity_id": "cover.raw",
        "command_conditions": {"set_cover_position": deepcopy(CONDITIONS)},
    }
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] == "create_entry"
    resource = next(iter(entry.subentries.values()))
    edit = await entry.start_subentry_reconfigure_flow(hass, resource.subentry_id)
    key = next(key for key in edit["data_schema"].schema if key == "command_conditions")
    assert key.description["suggested_value"] == data["command_conditions"]
    before = json.dumps(dict(resource.data), sort_keys=True)
    hass.config.config_dir = str(tmp_path)
    assert await hass.config_entries.async_setup(entry.entry_id)
    checker = entry.runtime_data._command_checks[resource.subentry_id]["set_cover_position"]
    assert checker.setup_error is None
    assert json.dumps(dict(entry.subentries[resource.subentry_id].data), sort_keys=True) == before
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("field", ["conditions", "triggers", "cancel_triggers"])
async def test_native_selector_invalid_input_is_rejected_by_real_flow_manager(
    hass, native_sources, field
):
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    data = {
        "name": "Invalid",
        "kind": "occurrence",
        "resource_id": resource.subentry_id,
        "target": {"position": 0},
        field: [{}],
    }
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert not entry.get_subentries_of_type("policy")
