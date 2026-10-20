"""Typed native diagnostics retain useful facts without leaking configuration."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import json

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_operator.const import DOMAIN
from custom_components.ha_operator.diagnostics import async_get_config_entry_diagnostics
from tests.integration.helpers import operator_entry
from tests.integration.test_return_monitor_runtime import notifications, setup_monitor  # noqa: F401
from tests.integration.test_runtime_reconciliation import (
    advance,  # noqa: F401
    reported,
    runtime_factory,  # noqa: F401
)


async def test_typed_diagnostics_allowlist_values_and_pseudonymize_options_and_conditions(
    hass, runtime_factory
):
    private_option = "Private family mode with sensitive label"
    hass.states.async_set("number.private_setpoint", "21.5", {"min": 0, "max": 100, "step": 1})
    hass.states.async_set(
        "select.private_mode", "Private idle", {"options": ["Private idle", private_option]}
    )
    hass.states.async_set("binary_sensor.private_permission", "on")
    commands = []

    async def record(call):
        commands.append(call.service)

    hass.services.async_register("number", "set_value", record)
    hass.services.async_register("select", "select_option", record)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={
            "private_number": {
                "name": "Private setpoint",
                "kind": "number",
                "entity_id": "number.private_setpoint",
                "unit_of_measurement": None,
                "tolerance": 0,
                "command_conditions": {
                    "set_value": [
                        {
                            "condition": "state",
                            "entity_id": "binary_sensor.private_permission",
                            "state": "on",
                        }
                    ]
                },
            },
            "private_select": {
                "name": "Private mode",
                "kind": "select",
                "entity_id": "select.private_mode",
            },
        },
        policies={
            "private_policy": {
                "name": "Private admission",
                "kind": "state",
                "resource_id": "private_select",
                "target": {"option": private_option},
                "conditions": [
                    {
                        "condition": "state",
                        "entity_id": "binary_sensor.private_permission",
                        "state": "on",
                    }
                ],
            }
        },
    )
    await runtime.async_set_mode("private_number", "live")
    await runtime.async_set_mode("private_select", "live")
    await runtime.async_request("private_number", target={"value": 37.25}, indefinite=True)
    await runtime.async_request(
        "private_select", target={"option": private_option}, indefinite=True
    )
    await hass.async_block_till_done()
    assert set(commands) == {"set_value", "select_option"}
    before = json.dumps(runtime._state, sort_keys=True)
    diagnostics = await async_get_config_entry_diagnostics(hass, runtime.entry)
    assert json.dumps(runtime._state, sort_keys=True) == before
    resources = {resource["kind"]: resource for resource in diagnostics["resources"].values()}
    number = resources["number"]
    assert number["desired"] == {"value": 37.25}
    assert number["observed"] == {"value": 21.5}
    assert number["manual"]["target"] == number["last_command"]["target"] == {"value": 37.25}
    assert number["command_conditions"] == {
        "set_value": {
            "status": "passed",
            "evaluation_error": False,
            "evaluated_condition_count": 1,
        }
    }
    select = resources["select"]
    pseudonym = select["desired"]["option"]
    assert isinstance(pseudonym, str) and len(pseudonym) == 12
    assert (
        select["manual"]["target"]["option"]
        == select["last_command"]["target"]["option"]
        == pseudonym
    )
    assert select["observed"]["option"] != pseudonym
    assert list(diagnostics["policies"].values()) == [{"enabled": True}]
    exported = json.dumps(diagnostics)
    for private in (
        "Private",
        "private_",
        "binary_sensor.",
        "number.",
        "select.",
        'conditions": [{',
    ):
        assert private not in exported


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("repair", [False, True])
async def test_absent_or_closed_diagnostics_report_structure_and_repair_without_names(
    hass, runtime_factory, closed, repair
):
    if closed:
        reported(hass)
        factory, _ = runtime_factory
        runtime = await factory()
        entry = runtime.entry
        await runtime.async_close()
    else:
        entry = operator_entry(
            resources={
                "private_resource": {
                    "name": "Private resource",
                    "kind": "switch",
                    "entity_id": "switch.private_raw",
                }
            }
        )
        entry.add_to_hass(hass)
    if repair:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"configuration_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="invalid_native_condition",
        )
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["runtime_state"] == ("closed" if closed else "absent")
    assert result["faulted"] is repair
    assert result["subentries"] == {"resource": 1, "policy": 0, "requirement": 0, "intent": 0}
    assert (
        result["resources"]
        == result["policies"]
        == result["requirements"]
        == result["intents"]
        == {}
    )
    assert "private" not in json.dumps(result).lower()


async def test_return_monitor_diagnostics_preserve_deadline_phase_and_anonymous_relationships(
    hass, runtime_factory, freezer, notifications
):
    factory, _ = runtime_factory
    runtime = await setup_monitor(hass, factory, name="Private household roof")
    waiting = await async_get_config_entry_diagnostics(hass, runtime.entry)
    resource_id, resource = next(iter(waiting["resources"].items()))
    due = runtime.return_monitor("roof").due_at
    assert resource_id != "roof"
    assert resource["return_monitor"] == {
        "phase": "waiting",
        "target_position": 7,
        "due_at": due,
        "overdue": False,
    }
    await advance(hass, freezer, 300)
    before = json.dumps(runtime._state, sort_keys=True)
    overdue = await async_get_config_entry_diagnostics(hass, runtime.entry)
    assert json.dumps(runtime._state, sort_keys=True) == before
    assert list(overdue["resources"]) == [resource_id]
    assert overdue["resources"][resource_id]["return_monitor"] == {
        "phase": "overdue",
        "target_position": 7,
        "due_at": due,
        "overdue": True,
    }
    exported = json.dumps(overdue)
    for private in ("Private", "household", "roof", "cover.raw", "Cold", "return_monitors"):
        assert private not in exported
    reported(hass, position=7)
    await hass.async_block_till_done()
    confirmed = await async_get_config_entry_diagnostics(hass, runtime.entry)
    assert confirmed["resources"][resource_id]["return_monitor"] == {
        "phase": "idle",
        "target_position": None,
        "due_at": None,
        "overdue": False,
    }
