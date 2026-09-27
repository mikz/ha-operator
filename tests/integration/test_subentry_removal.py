"""Native subentry deletion must quiesce control and explain broken references."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.ha_operator.const import DOMAIN

from .helpers import async_add_physical_cover, async_setup_operator


def _dependencies(kind):
    if kind == "policy":
        return {
            "policies": {
                "morning": {
                    "name": "Morning",
                    "kind": "state",
                    "resource_id": "roof",
                    "target": {"position": 60},
                }
            }
        }
    return {
        "requirements": {
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
                                "entity_id": "cover.physical_roof",
                                "kind": "position",
                                "attribute": "current_position",
                                "operator": "gte",
                                "value": 30,
                            }
                        ],
                    }
                ],
            }
        }
    }


@pytest.mark.parametrize("kind,dependent", [("policy", "morning"), ("requirement", "air")])
async def test_native_delete_referenced_resource_inhibits_until_repaired_and_reloaded(
    hass,
    tmp_path,
    hass_ws_client,
    kind,
    dependent,
):
    raw = await async_add_physical_cover(hass)
    hass.states.async_set("binary_sensor.extraction", "off")
    entry = await async_setup_operator(hass, tmp_path, **_dependencies(kind))
    original = entry.runtime_data
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "config_entries/subentries/delete",
            "entry_id": entry.entry_id,
            "subentry_id": "roof",
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert original._closed
    assert all(task.done() for task in original._tasks)
    assert not original._listeners and not original._unsubscribers
    assert not original._timers
    assert "roof" not in entry.subentries
    assert dependent in entry.subentries  # Never silently remove a dependent policy.
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not entry.update_listeners
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"configuration_{entry.entry_id}")
    assert issue is not None and issue.translation_key == "configuration_error"
    assert "existing resource" in issue.translation_placeholders["detail"]
    assert raw.commands == []

    # HA removes update listeners after failed setup. The Repair explicitly tells
    # the user to remove/reconfigure the remaining dependency, then use Reload.
    await client.send_json(
        {
            "id": 2,
            "type": "config_entries/subentries/delete",
            "entry_id": entry.entry_id,
            "subentry_id": dependent,
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data._tasks
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"configuration_{entry.entry_id}") is None
    assert len(entry.update_listeners) == 1
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unreferenced_resource_delete_reloads_to_empty_configuration(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    original = entry.runtime_data
    assert hass.config_entries.async_remove_subentry(entry, "roof")
    await hass.async_block_till_done()
    assert original._closed and all(task.done() for task in original._tasks)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not original
    assert not entry.runtime_data.resources and not entry.runtime_data._tasks
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"configuration_{entry.entry_id}") is None
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_restored_valid_snapshot_clears_storage_repair_only_after_good_reload(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    snapshot = await hass.async_add_executor_job(path.read_text)
    await hass.async_add_executor_job(path.write_text, "invalid json")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.fault == "storage_error"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.fault == "storage_error"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
    await hass.async_add_executor_job(path.write_text, snapshot)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.fault is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is None
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)
