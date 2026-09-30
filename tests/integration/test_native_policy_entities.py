"""Native read-only views do not own qualification or return monitoring."""

from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

from custom_components.ha_operator.const import DOMAIN
from tests.integration.helpers import (
    async_activate,
    async_add_physical_cover,
    async_setup_operator,
    managed_id,
)
from tests.integration.test_policy_inputs_runtime import numeric_policy, temperature


async def advance(hass, freezer, seconds):
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=seconds))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_disabled_diagnostics_execute_and_public_enable_reload_then_disable(
    hass, tmp_path, freezer
):
    await async_add_physical_cover(hass)
    temperature(hass, 15)
    entry = await async_setup_operator(hass, tmp_path, policies=numeric_policy())
    await async_activate(hass)
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    ids = [
        registry.async_get_entity_id(platform, DOMAIN, f"cold_{key}")
        for platform, key in (
            ("sensor", "input_phase"),
            ("sensor", "qualification_due"),
            ("binary_sensor", "qualified"),
        )
    ]
    for entity_id in ids:
        record = registry.async_get(entity_id)
        assert record.config_subentry_id == "cold"
        assert record.entity_category == "diagnostic"
        assert record.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
    temperature(hass, 14)
    await hass.async_block_till_done()
    due = runtime.policy_input("cold").due_at
    await advance(hass, freezer, 60)
    assert runtime.policy_input("cold").qualified
    assert runtime.decisions["roof"].target.position == 7
    assert all(hass.states.get(entity_id) is None for entity_id in ids)
    for entity_id in ids:
        registry.async_update_entity(entity_id, disabled_by=None)
    await hass.async_block_till_done()
    await advance(hass, freezer, 31)
    assert runtime._closed and entry.runtime_data is not runtime
    runtime = entry.runtime_data
    assert runtime.policy_input("cold").due_at == due
    temperature(hass, 13)
    await hass.async_block_till_done()
    assert hass.states.get(ids[0]).state == "qualified"
    assert hass.states.get(ids[1]).attributes["device_class"] == "timestamp"
    assert hass.states.get(ids[2]).state == "on"
    for entity_id in ids:
        registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert entry.runtime_data is runtime and not runtime._closed
    temperature(hass, 17)
    await hass.async_block_till_done()
    assert not runtime.policy_input("cold").qualified
    temperature(hass, 14)
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    assert runtime.policy_input("cold").qualified
    assert entry.runtime_data is runtime
    await hass.config_entries.async_unload(entry.entry_id)


async def test_return_problem_primary_expiry_and_input_source_links(hass, tmp_path, freezer):
    raw = await async_add_physical_cover(hass)
    raw._attr_current_cover_position = 100
    raw.refuse = True
    raw.async_write_ha_state()
    temperature(hass, 15)
    entry = await async_setup_operator(
        hass,
        tmp_path,
        resources={
            "roof": {
                "name": "Roof",
                "kind": "cover",
                "entity_id": raw.entity_id,
                "return_monitor": {"target_at_most": 7, "warning_after_seconds": 300},
            }
        },
        policies=numeric_policy()
        | {
            "vent": {
                "name": "Opening",
                "kind": "occurrence",
                "resource_id": "roof",
                "priority": 100,
                "target": {"position": 100},
            }
        },
    )
    await async_activate(hass)
    temperature(hass, 14)
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    desired = hass.states.get(managed_id(hass, "sensor", key="desired"))
    assert desired.attributes["related_entities"] == ("sensor.temperature",)
    expiry_id = managed_id(hass, "sensor", key="effective_expiry")
    assert desired.attributes["effective_expiry_entity"] == expiry_id
    registry = er.async_get(hass)
    overdue_id = registry.async_get_entity_id("binary_sensor", DOMAIN, "roof_return_overdue")
    assert registry.async_get(overdue_id).entity_category is None
    assert hass.states.get(overdue_id).attributes["device_class"] == "problem"
    assert hass.states.get(overdue_id).state == "off"
    await advance(hass, freezer, 300)
    assert hass.states.get(overdue_id).state == "on"
    raw._attr_current_cover_position = 7
    raw.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(overdue_id).state == "off"
    expiry = dt_util.utcnow().timestamp() + 120
    await entry.runtime_data.async_submit_occurrence("vent", "synthetic-episode", expiry)
    await hass.async_block_till_done()
    assert dt_util.parse_datetime(hass.states.get(expiry_id).state).timestamp() == int(expiry)
    assert hass.states.get(managed_id(hass, "sensor", key="expiry")).state == "unknown"
    assert registry.async_get(expiry_id).entity_category is None
    registry.async_update_entity(expiry_id, new_entity_id="sensor.renamed_effective_expiry")
    await hass.async_block_till_done()
    assert (
        hass.states.get(managed_id(hass, "sensor", key="reason")).attributes[
            "effective_expiry_entity"
        ]
        == "sensor.renamed_effective_expiry"
    )
    await hass.config_entries.async_unload(entry.entry_id)
