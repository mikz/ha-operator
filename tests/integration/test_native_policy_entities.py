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


async def advance(hass, freezer, seconds):
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=seconds))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_return_problem_primary_expiry_and_input_source_links(hass, tmp_path, freezer):
    raw = await async_add_physical_cover(hass)
    raw._attr_current_cover_position = 100
    raw.refuse = True
    raw.async_write_ha_state()
    hass.states.async_set("sensor.temperature", "15")
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
        policies={
            "cold": {
                "name": "Cold",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 7},
                "conditions": [
                    {"condition": "numeric_state", "entity_id": "sensor.temperature", "below": 16}
                ],
            }
        }
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
    hass.states.async_set("sensor.temperature", "14")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    desired = hass.states.get(managed_id(hass, "sensor", key="desired"))
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
