"""Native condition truth, native admission and atomic configuration migration."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import json
from copy import deepcopy

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator import async_migrate_entry
from custom_components.ha_operator.const import DOMAIN
from tests.integration.helpers import operator_entry
from tests.integration.test_config_flow import _resource, _start, native_sources  # noqa: F401
from tests.integration.test_runtime_reconciliation import (  # noqa: F401
    advance,
    reported,
    runtime_factory,
)


async def test_continuous_native_conditions_startup_tracking_and_no_global_veto(
    hass, runtime_factory
):
    reported(hass)
    hass.states.async_set("binary_sensor.ready", "on")
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "p": {
                "name": "Native",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 50},
                "conditions": [
                    {
                        "condition": "or",
                        "conditions": [
                            {
                                "condition": "state",
                                "entity_id": "binary_sensor.missing",
                                "state": "on",
                            },
                            {
                                "condition": "state",
                                "entity_id": "binary_sensor.ready",
                                "state": "on",
                            },
                        ],
                    }
                ],
            }
        }
    )
    assert runtime.decisions["roof"].target.position == 50
    hass.states.async_set("binary_sensor.ready", "off")
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].target is None
    assert runtime.explain()["policies"]["p"]["status"] == "unknown"
    hass.states.async_set("binary_sensor.ready", "on")
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].target.position == 50


@pytest.mark.parametrize(
    "conditions,selected",
    [
        ([], True),
        ([{"condition": "template", "value_template": "{{ false }}", "enabled": False}], True),
        ([{"condition": "or", "conditions": []}], False),
        ([{"condition": "template", "value_template": "{{ 1 / 0 }}"}], False),
    ],
)
async def test_native_policy_empty_disabled_compound_and_error(
    hass, runtime_factory, conditions, selected
):
    reported(hass)
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "p": {
                "name": "Native",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 50},
                "conditions": conditions,
            }
        }
    )
    assert (runtime.decisions["roof"].target is not None) is selected


async def test_native_trigger_condition_uses_real_admission_variables(hass, runtime_factory):
    reported(hass)
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "p": {
                "name": "Native",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 50},
                "triggers": [{"trigger": "event", "event_type": "native_policy", "id": "admit"}],
                "duration": {"seconds": 20},
                "conditions": [{"condition": "trigger", "id": "admit"}],
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    hass.bus.async_fire("native_policy")
    await hass.async_block_till_done()
    assert len(runtime._state["occurrences"]) == 1
    external = await runtime.async_submit_occurrence(
        "p", "external", next(iter(runtime._state["occurrences"].values()))["expires_at"]
    )
    assert external["skipped"]  # No fabricated trigger data for the external API.


async def test_purpose_specific_native_event_received_trigger(hass, runtime_factory):
    assert await async_setup_component(hass, "event", {})
    hass.states.async_set("event.example", "2026-10-20T00:00:00+00:00", {"event_type": "sample"})
    reported(hass)
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "p": {
                "name": "Native event",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 50},
                "triggers": [
                    {
                        "trigger": "event.received",
                        "target": {"entity_id": "event.example"},
                        "options": {"event_type": ["sample"]},
                    }
                ],
                "duration": {"seconds": 20},
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert runtime._trigger_owners["p"].ready
    hass.states.async_set("event.example", "2026-10-20T00:00:01+00:00", {"event_type": "sample"})
    await hass.async_block_till_done()
    assert len(runtime._state["occurrences"]) == 1


async def test_native_policy_selector_roundtrip_keeps_json_templates_and_duration(
    hass, native_sources
):
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    assert {"conditions", "triggers", "cancel_triggers", "duration"} <= set(
        form["data_schema"].schema
    )
    data = {
        "name": "Native",
        "kind": "occurrence",
        "resource_id": resource.subentry_id,
        "target": {"position": 7},
        "duration": {"minutes": 2},
        "triggers": [{"trigger": "event", "event_type": "sample", "enabled": "{{ false }}"}],
        "conditions": [{"condition": "template", "value_template": "{{ true }}"}],
    }
    created = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert created["type"] is FlowResultType.CREATE_ENTRY
    policy = next(iter(entry.get_subentries_of_type("policy")))
    assert json.loads(json.dumps(dict(policy.data)))["triggers"][0]["enabled"] == "{{ false }}"
    edit = await entry.start_subentry_reconfigure_flow(hass, policy.subentry_id)
    schema = edit["data_schema"].schema
    for field in ("triggers", "conditions", "duration"):
        key = next(key for key in schema if key == field)
        assert key.description["suggested_value"] == policy.data[field]
        schema[key](policy.data[field])
    changed = await hass.config_entries.subentries.async_configure(edit["flow_id"], data)
    assert changed["reason"] == "reconfigure_successful"
    assert policy.subentry_id in entry.subentries


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "state", "input": {"type": "qualified_numeric"}},
        {"kind": "occurrence", "eligibility_entity": "binary_sensor.ready"},
    ],
)
async def test_migration_preflights_all_subentries_before_any_mutation(hass, bad):
    entry = operator_entry(
        version=3,
        policies={
            "first": {
                "name": "First",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 7},
                "eligibility_entity": "binary_sensor.ready",
            },
            "last": {"name": "Last", "resource_id": "roof", "target": {"position": 7}, **bad},
        },
    )
    entry.add_to_hass(hass)
    before = {key: deepcopy(dict(item.data)) for key, item in entry.subentries.items()}
    assert not await async_migrate_entry(hass, entry)
    assert entry.version == 3
    assert {key: dict(item.data) for key, item in entry.subentries.items()} == before
    assert (DOMAIN, f"native_policy_migration_{entry.entry_id}") in ir.async_get(hass).issues


@pytest.mark.parametrize("checks", [None, []])
async def test_migration_translates_state_pair_and_strips_only_cleared_keys(hass, checks):
    resource = {"name": "Roof", "kind": "cover", "entity_id": "cover.raw"}
    if checks is not None:
        resource["health_checks"] = checks
    entry = operator_entry(
        version=3,
        resources={"roof": resource},
        policies={
            "p": {
                "name": "State",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 7},
                "eligibility_entity": "binary_sensor.ready",
                "eligibility_state": "off",
                "priority": 8,
            }
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 4
    assert entry.subentries["p"].data["conditions"] == [
        {"condition": "state", "entity_id": "binary_sensor.ready", "state": "off"}
    ]
    assert "health_checks" not in entry.subentries["roof"].data
    assert entry.subentries["p"].data["priority"] == 8


async def test_accepted_occurrence_keeps_admission_evidence_behind_manual(
    hass, runtime_factory, freezer
):
    factory, _ = runtime_factory
    reported(hass)
    hass.states.async_set("binary_sensor.ready", "on")
    runtime = await factory(
        policies={
            "p": {
                "name": "Native",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 50},
                "duration": {"minutes": 5},
                "triggers": [{"trigger": "event", "event_type": "admit"}],
                "conditions": [
                    {"condition": "state", "entity_id": "binary_sensor.ready", "state": "on"}
                ],
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await runtime.async_request("roof", target={"position": 7}, indefinite=True)
    hass.bus.async_fire("admit")
    await hass.async_block_till_done()
    accepted = next(
        item for item in runtime._state["occurrences"].values() if item.get("trigger_owned")
    )
    key = ("occurrence", "p", accepted["occurrence_id"])
    original = deepcopy(runtime._ingress_details[key])
    await advance(hass, freezer, 1)
    hass.states.async_set("binary_sensor.ready", "off")
    hass.bus.async_fire("admit")
    await hass.async_block_till_done()
    assert runtime._policy_results["p"].status == "failed"
    await runtime.async_release("roof")
    await hass.async_block_till_done()
    reason = runtime.reason_details("roof")
    assert reason["policy_conditions"] == original["policy_conditions"]
    assert reason["captured_at"] == original["captured_at"]
    assert reason["policy_conditions"]["status"] == "passed"


async def test_trigger_owned_slot_retires_at_exact_expiry(hass, runtime_factory, freezer):
    factory, _ = runtime_factory
    reported(hass)
    runtime = await factory(
        policies={
            "p": {
                "name": "Native",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 50},
                "duration": {"seconds": 10},
                "triggers": [{"trigger": "event", "event_type": "exact_expiry"}],
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    hass.bus.async_fire("exact_expiry")
    await hass.async_block_till_done()
    assert len(runtime._state["occurrences"]) == 1
    await advance(hass, freezer, 10)
    assert not runtime._state["occurrences"]


async def test_native_numeric_trigger_freezes_genuine_source_evidence(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    hass.states.async_set(
        "sensor.native_source",
        "10",
        {"unit_of_measurement": "°C", "friendly_name": "Example source"},
    )
    runtime = await factory(
        policies={
            "p": {
                "name": "Native source",
                "resource_id": "roof",
                "kind": "occurrence",
                "target": {"position": 50},
                "triggers": [
                    {"trigger": "numeric_state", "entity_id": "sensor.native_source", "above": 15}
                ],
                "duration": {"minutes": 2},
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    hass.states.async_set(
        "sensor.native_source",
        "20",
        {"unit_of_measurement": "°C", "friendly_name": "Example source"},
    )
    await hass.async_block_till_done()
    reason = deepcopy(runtime.reason_details("roof"))
    assert reason["source_capture"] == "native_trigger"
    assert reason["source_entity"] == "sensor.native_source"
    assert reason["source_value"] == "20"
    assert reason["source_unit"] == "°C"
    assert reason["source_updated_at"]
    hass.states.async_set("sensor.native_source", "21", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert runtime.reason_details("roof")["source_value"] == "20"


@pytest.mark.parametrize(
    "conditions",
    [[], [{"condition": "template", "value_template": "{{ false }}", "enabled": False}]],
)
async def test_unconditional_state_policy_avoids_empty_evaluation_and_keeps_reason_evidence(
    hass, runtime_factory, monkeypatch, conditions
):
    from unittest.mock import Mock

    reported(hass)
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "p": {
                "name": "Unconditional",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 7},
                "conditions": conditions,
            }
        }
    )
    checker = runtime._policy_checks["p"]
    evaluate = Mock(wraps=checker.evaluate)
    monkeypatch.setattr(checker, "evaluate", evaluate)
    for _ in range(3):
        await runtime.async_reconcile()
    assert bool(evaluate.call_count) is bool(conditions)
    assert runtime.decisions["roof"].target.position == 7
    evidence = runtime.reason_details("roof")["policy_conditions"]
    assert evidence["status"] == "skipped"
    assert len(evidence["conditions"]) == len(conditions)
