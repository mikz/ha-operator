"""Exercise actual config-entry lifecycle, registry links and fault visibility."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_operator import (
    async_migrate_entry,
    async_remove_config_entry_device,
    async_setup_entry,
)
from custom_components.ha_operator.const import DOMAIN
from custom_components.ha_operator.diagnostics import async_get_config_entry_diagnostics

from .helpers import (
    async_activate,
    async_add_physical_cover,
    async_setup_operator,
    managed_id,
    operator_entry,
)


async def test_real_setup_observe_registry_and_reload(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    registry = er.async_get(hass)
    cover_id = managed_id(hass, "cover")
    assert entry.state is ConfigEntryState.LOADED and entry.data["initialized"]
    assert hass.states.get(cover_id).attributes["current_position"] == 0
    assert hass.states.get(managed_id(hass, "select", key="mode")).state == "observe"
    records = [
        item for item in registry.entities.values() if item.config_entry_id == entry.entry_id
    ]
    assert len(records) == 12
    assert {item.config_subentry_id for item in records} == {"roof"}
    assert len({item.device_id for item in records}) == 1
    assert raw.commands == []
    assert registry.async_get(managed_id(hass, "sensor", key="attempts")).disabled_by is not None
    assert (
        registry.async_get(managed_id(hass, "sensor", key="next_attempt")).disabled_by is not None
    )
    assert len(entry.runtime_data._tasks) == 1 and len(entry.runtime_data._listeners) == 10
    for _ in range(3):
        previous = entry.runtime_data
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert previous._closed and not previous._listeners
        assert all(task.done() for task in previous._tasks)
        assert entry.runtime_data is not previous
        assert len(entry.runtime_data._tasks) == 1
        assert not entry.runtime_data._tasks[0].done()
        assert managed_id(hass, "cover") == cover_id
    active = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert active._closed and all(task.done() for task in active._tasks)
    assert not active._listeners
    assert hass.states.get(cover_id).state == "unavailable"
    assert raw.commands == []


async def test_live_native_request_roundtrip_survives_reload(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    await hass.services.async_call(
        "cover",
        "set_cover_position",
        {
            "entity_id": managed_id(hass, "cover"),
            "position": 67,
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert raw.commands == [("position", 67)]
    assert hass.states.get(managed_id(hass, "cover")).attributes["current_position"] == 0
    assert float(hass.states.get(managed_id(hass, "sensor", key="desired")).state) == 67
    expiry = entry.runtime_data.manual("roof").expires_at
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.mode("roof") == "live"
    assert entry.runtime_data.manual("roof").expires_at == expiry
    assert entry.runtime_data.manual("roof").target.position == 67
    assert len(raw.commands) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_real_policy_and_requirement_entities(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    hass.states.async_set("binary_sensor.extraction", "off")
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 80},
            }
        },
        requirements={
            "air": {
                "name": "Air",
                "activation_entities": ["binary_sensor.extraction"],
                "providers": [
                    {
                        "id": "window",
                        "resource_id": "roof",
                        "target": {"position": 60},
                        "evidence": [
                            {
                                "entity_id": raw.entity_id,
                                "attribute": "current_position",
                                "operator": "gte",
                                "value": 20,
                                "kind": "position",
                            }
                        ],
                    }
                ],
            }
        },
    )
    assert hass.states.get(managed_id(hass, "switch", "morning", "enabled")).state == "on"
    assert hass.states.get(managed_id(hass, "sensor", "air", "status")).state == "inactive"
    assert hass.states.get(managed_id(hass, "binary_sensor", "air", "unmet")).state == "off"
    await hass.services.async_call(
        "switch",
        "turn_off",
        {
            "entity_id": managed_id(hass, "switch", "morning", "enabled"),
        },
        blocking=True,
    )
    assert not entry.runtime_data.policy_enabled("morning")
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_corrupt_storage_loads_visible_fault_without_actuation(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    await hass.async_add_executor_job(path.write_text, "not json")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.fault == "storage_error"
    assert hass.states.get(managed_id(hass, "sensor", key="status")).state == "fault"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["faulted"] and "physical_roof" not in json.dumps(diagnostics)
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_bad_config_fails_setup_without_background_workers(hass, tmp_path):
    await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(
        resources={
            "roof": {
                "name": "Roof",
                "kind": "cover",
                "entity_id": "switch.wrong_domain",
            }
        }
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(entry, "runtime_data")


async def test_forwarding_failure_quiesces_started_runtime(hass, tmp_path):
    await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry()
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await async_setup_entry(hass, entry)
    assert entry.runtime_data._closed
    assert all(task.done() for task in entry.runtime_data._tasks)


async def test_migration_and_device_removal_contract(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    record = er.async_get(hass).async_get(managed_id(hass, "cover"))
    device = dr.async_get(hass).async_get(record.device_id)
    assert not await async_remove_config_entry_device(hass, entry, device)
    assert await async_remove_config_entry_device(
        hass, entry, SimpleNamespace(config_entries=set())
    )
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert await async_migrate_entry(hass, operator_entry(version=2))
    assert not await async_migrate_entry(hass, operator_entry(version=3))
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_input_config_migration_preserves_all_settings_and_ids(hass):
    entry = operator_entry(
        version=1,
        data={"initialized": True, "custom": "preserved"},
        policies={
            "cold": {
                "name": "Cold",
                "resource_id": "roof",
                "kind": "state",
                "target": {"position": 7},
                "eligibility_entity": "input_boolean.ready",
            }
        },
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"shadow_lock": True})
    data, options, subentries = entry.data, entry.options, entry.subentries
    identifiers = {key: item.subentry_id for key, item in subentries.items()}
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2 and entry.minor_version == 1
    assert entry.data is data and entry.options is options and entry.subentries is subentries
    assert {key: item.subentry_id for key, item in entry.subentries.items()} == identifiers
    assert "return_monitor" not in entry.subentries["roof"].data
    assert "input" not in entry.subentries["cold"].data
    assert entry.subentries["cold"].data["eligibility_entity"] == "input_boolean.ready"
    assert await async_migrate_entry(hass, entry)
    assert entry.subentries is subentries


async def test_entry_update_listener_reloads_exactly_once(hass, tmp_path):
    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    old = entry.runtime_data
    hass.config_entries.async_update_entry(entry, title="Renamed operator")
    await hass.async_block_till_done()
    assert old._closed and all(task.done() for task in old._tasks)
    assert entry.runtime_data is not old
    assert len(entry.runtime_data._tasks) == 1
    assert len(entry.update_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
