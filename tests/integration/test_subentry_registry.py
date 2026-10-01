"""Native subentry CRUD proves registry ownership and reload preservation."""

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_operator.const import DOMAIN

from .helpers import async_activate, async_add_physical_cover, async_setup_operator, managed_id


def assert_quiescent(runtime):
    assert runtime._closed
    assert all(task.done() for task in runtime._tasks)
    assert not runtime._listeners and not runtime._unsubscribers and not runtime._timers


async def test_native_crud_preserves_registry_customizations_intent_and_owner(
    hass, tmp_path, hass_ws_client
):
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    hass.states.async_set("switch.follower", "off")
    hass.states.async_set("binary_sensor.extractor", "off")
    hass.states.async_set("binary_sensor.inlet", "on")
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    await entry.runtime_data.async_request("roof", target={"position": 63}, duration=600)
    await hass.async_block_till_done()
    lease = entry.runtime_data.manual("roof")
    registry = er.async_get(hass)
    roof_id = managed_id(hass, "cover")
    registry.async_update_entity(roof_id, name="User roof", icon="mdi:star")
    attempts = managed_id(hass, "sensor", key="attempts")
    registry.async_update_entity(attempts, disabled_by=er.RegistryEntryDisabler.USER)
    roof_records = {
        item.unique_id: (item.entity_id, item.device_id, item.name, item.icon, item.disabled_by)
        for item in registry.entities.values()
        if item.config_subentry_id == "roof"
    }
    settings = (entry.version, dict(entry.data), dict(entry.options))
    identifiers = {}
    configs = {
        "intent": {"name": "User desired mode", "initial_value": False},
        "resource": {
            "name": "User follower",
            "kind": "switch",
            "entity_id": "switch.follower",
            "manual_control": False,
        },
        "policy": {
            "name": "User policy",
            "kind": "state",
            "resource_id": "roof",
            "target": {"position": 40},
        },
        "requirement": {
            "name": "User requirement",
            "activation_entities": ["binary_sensor.extractor"],
            "providers": [
                {
                    "id": "User passive inlet",
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
        },
    }
    for kind, config in configs.items():
        previous = entry.runtime_data
        existing = set(entry.subentries)
        flow = await hass.config_entries.subentries.async_init(
            (entry.entry_id, kind), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(flow["flow_id"], config)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert_quiescent(previous)
        (identifier,) = set(entry.subentries) - existing
        identifiers[kind] = identifier
        records = [
            item for item in registry.entities.values() if item.config_subentry_id == identifier
        ]
        assert records and all(item.config_entry_id == entry.entry_id for item in records)
        devices = {item.device_id for item in records}
        assert len(devices) == 1 and None not in devices
        device = dr.async_get(hass).async_get(next(iter(devices)))
        assert device.identifiers == {(DOMAIN, identifier)}
        assert device.config_entry_id == entry.entry_id
        assert device.config_subentry_id == identifier
        assert entry.runtime_data.manual("roof") == lease
        assert len(entry.runtime_data._tasks) == len(entry.runtime_data.resources)
        assert len(entry.update_listeners) == 1
        unique_ids = {item.unique_id: item.entity_id for item in records}
        edit = await entry.start_subentry_reconfigure_flow(hass, identifier)
        previous = entry.runtime_data
        result = await hass.config_entries.subentries.async_configure(
            edit["flow_id"], {**config, "name": config["name"] + " renamed"}
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert_quiescent(previous)
        assert entry.subentries[identifier].title == config["name"] + " renamed"
        assert {
            item.unique_id: item.entity_id
            for item in registry.entities.values()
            if item.config_subentry_id == identifier
        } == unique_ids
        assert entry.runtime_data.manual("roof") == lease
    assert (entry.version, dict(entry.data), dict(entry.options)) == settings
    assert {
        item.unique_id: (item.entity_id, item.device_id, item.name, item.icon, item.disabled_by)
        for item in registry.entities.values()
        if item.config_subentry_id == "roof"
    } == roof_records
    assert hass.states.get(roof_id).attributes["friendly_name"] == "User roof"
    assert all(command == ("position", 63) for command in raw.commands)
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    for count, kind in enumerate(reversed(configs), 1):
        identifier = identifiers[kind]
        previous = entry.runtime_data
        await client.send_json(
            {
                "id": count,
                "type": "config_entries/subentries/delete",
                "entry_id": entry.entry_id,
                "subentry_id": identifier,
            }
        )
        assert (await client.receive_json())["success"]
        await hass.async_block_till_done()
        assert_quiescent(previous)
        assert entry.state is ConfigEntryState.LOADED
        assert identifier not in entry.subentries
        assert not any(item.config_subentry_id == identifier for item in registry.entities.values())
        assert (
            dr.async_get(hass).async_get_device_by_identifier((DOMAIN, identifier), entry.entry_id)
            is None
        )
        assert entry.runtime_data.manual("roof") == lease
    assert len(entry.runtime_data._tasks) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "profiles,key",
    [
        (
            {
                "off": {"outputs": {"switch.a": False}},
                "first": {"outputs": {"switch.a": True}},
                "duplicate": {"outputs": {"switch.a": True}},
            },
            "config_relay_profile_signatures",
        ),
        (
            {
                "off": {"outputs": {"switch.a": False}, "percentage": 50},
                "on": {"outputs": {"switch.a": True}},
            },
            "config_relay_profile_speed",
        ),
        (
            {
                "off": {"outputs": {"switch.a": False}},
                "on": {"outputs": {"switch.a": True}, "percentage": 0},
            },
            "config_relay_profile_speed",
        ),
    ],
)
async def test_relay_flow_rejects_expected_profile_errors_before_reload(
    hass, tmp_path, profiles, key
):
    hass.states.async_set("switch.a", "off")
    entry = await async_setup_operator(hass, tmp_path, resources={})
    original = entry.runtime_data
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "resource"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {
            "name": "Relay",
            "kind": "relay_fan",
            "outputs": ["switch.a"],
            "profiles": profiles,
            "default_target": {"profile": "first" if "first" in profiles else "on"},
            "reversal_dead_time": 1,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    assert entry.runtime_data is original and entry.state is ConfigEntryState.LOADED
    assert not entry.subentries and not original._tasks
    assert await hass.config_entries.async_unload(entry.entry_id)
