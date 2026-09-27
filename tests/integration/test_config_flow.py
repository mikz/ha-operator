"""Exercise native HA flow managers, subentry identity, and reconfiguration."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator.config_flow import OperatorSubentryFlow
from custom_components.ha_operator.const import DOMAIN


@pytest.fixture
def native_sources(hass):
    hass.states.async_set("cover.raw", "closed", {"supported_features": 15, "current_position": 0})
    hass.states.async_set("binary_sensor.extraction", "on")
    hass.states.async_set("binary_sensor.inlet", "on")


async def _start(hass, entry, kind):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": "user"}
    )


async def _resource(hass, entry):
    flow = await _start(hass, entry, "resource")
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"name": "Skylight", "kind": "cover", "entity_id": "cover.raw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return next(item for item in entry.subentries.values() if item.subentry_type == "resource")


async def test_singleton_setup_and_abort(hass):
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_setup", new=AsyncMock(return_value=True)
    ):
        form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert form["type"] is FlowResultType.FORM
        created = await hass.config_entries.flow.async_configure(form["flow_id"], {})
        assert created["type"] is FlowResultType.CREATE_ENTRY
        assert created["title"] == "HA Operator"
        assert created["data"] == {}
        duplicate = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert duplicate["reason"] == "single_instance_allowed"


async def test_resource_errors_then_creation_and_reconfigure(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    invalid = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {"name": "Bad", "kind": "cover", "entity_id": "cover.missing"}
    )
    assert invalid["errors"] == {"base": "entity_not_found"}
    assert "cover.missing" in invalid["description_placeholders"]["detail"]
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "name": "Window",
            "kind": "cover",
            "entity_id": "cover.raw",
            "restriction_entity": "binary_sensor.extraction",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    original = next(iter(entry.subentries.values()))
    assert original.data["retry_interval"] == 300
    assert original.data["manual_duration"] == 1800
    # A listener is allowed; reload is owned by the parent, not the flow.
    with patch.object(entry, "update_listeners", [AsyncMock()]):
        edit = await entry.start_subentry_reconfigure_flow(hass, original.subentry_id)
        assert edit["step_id"] == "reconfigure"
        error = await hass.config_entries.subentries.async_configure(
            edit["flow_id"], {"name": "Window", "kind": "cover", "entity_id": "cover.missing"}
        )
        assert error["errors"] == {"base": "entity_not_found"}
        changed = await hass.config_entries.subentries.async_configure(
            edit["flow_id"], {"name": "Renamed", "kind": "cover", "entity_id": "cover.raw"}
        )
        assert changed["reason"] == "reconfigure_successful"
    updated = entry.subentries[original.subentry_id]
    assert updated.title == "Renamed"
    assert "restriction_entity" not in updated.data
    duplicate = await _start(hass, entry, "resource")
    duplicate = await hass.config_entries.subentries.async_configure(
        duplicate["flow_id"], {"name": "Duplicate", "kind": "cover", "entity_id": "cover.raw"}
    )
    assert duplicate["errors"] == {"base": "duplicate_output"}


async def test_policy_needs_resource_then_creates(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert (await _start(hass, entry, "policy"))["reason"] == "no_resources"
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "name": "Morning",
            "resource_id": resource.subentry_id,
            "kind": "state",
            "target": {"position": 50},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["resource_id"] == resource.subentry_id
    assert result["data"]["priority"] == 0


async def test_requirement_passive_and_overlap(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    data = {
        "name": "Ventilation",
        "activation_entities": ["binary_sensor.extraction"],
        "providers": [
            {
                "id": "passive",
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
    form = await _start(hass, entry, "requirement")
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["acquisition_timeout"] == 120
    resource = await _resource(hass, entry)
    data["providers"][0].update(resource_id=resource.subentry_id, target={"position": 50})
    form = await _start(hass, entry, "requirement")
    assert (await hass.config_entries.subentries.async_configure(form["flow_id"], data))[
        "type"
    ] is FlowResultType.CREATE_ENTRY
    form = await _start(hass, entry, "requirement")
    assert (await hass.config_entries.subentries.async_configure(form["flow_id"], data))[
        "errors"
    ] == {"base": "overlapping_requirement"}


def test_base_flow_requires_a_schema():
    with pytest.raises(NotImplementedError):
        OperatorSubentryFlow()._schema()


async def test_parent_flow_checks_existing_entries_at_submit(hass):
    """An entry added while the form was open cannot create another owner."""
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert result["reason"] == "single_instance_allowed"
