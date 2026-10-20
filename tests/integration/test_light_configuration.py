"""Native light capabilities validate saved targets without inventing defaults."""

import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator.configuration import (
    ConfigurationError,
    validate_resource,
    validate_target,
)
from custom_components.ha_operator.const import DOMAIN
from tests.integration.test_config_flow import _start
from tests.integration.test_light import add_physical, light_config


@pytest.mark.parametrize(
    "target",
    [
        {"hs_color": [10, 101]},
        {"hs_color": [10]},
        {"color_temp_kelvin": 1000},
        {"effect": "missing"},
        {"hs_color": [10, 20], "color_temp_kelvin": 3000},
    ],
)
async def test_light_saved_target_rejects_malformed_or_unsupported_native_value(hass, target):
    await add_physical(hass)
    resource = validate_resource(light_config(), hass)
    with pytest.raises(ConfigurationError):
        validate_target(target, resource, hass)


async def test_native_light_form_saves_editable_target_and_dynamic_temperature(hass):
    raw = await add_physical(hass)
    raw._attr_min_color_temp_kelvin = 4000
    raw._attr_max_color_temp_kelvin = 5000
    raw.async_write_ha_state()
    hass.states.async_set("input_number.temperature", "4500", {"unit_of_measurement": "K"})
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    data = light_config(default_target={"brightness": 64, "hs_color": [10, 20]})
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] == "create_entry"
    resource = next(iter(entry.get_subentries_of_type("resource")))
    assert json.loads(json.dumps(dict(resource.data)))["default_target"] == {
        "on": True,
        "brightness": 64,
        "hs_color": [10, 20],
    }
    form = await _start(hass, entry, "policy")
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "name": "Temperature",
            "kind": "state",
            "resource_id": resource.subentry_id,
            "target_entity": "input_number.temperature",
            "target_field": "color_temp_kelvin",
        },
    )
    assert result["type"] == "create_entry"
