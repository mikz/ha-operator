"""Reason Details captures selection-time evidence without following later telemetry."""

from datetime import timedelta

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from .helpers import async_activate, async_add_physical_cover, async_setup_operator, managed_id
from .test_provenance import policy


def reason(hass):
    return hass.states.get(managed_id(hass, "sensor", key="reason"))


async def advance(hass, freezer, seconds):
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=seconds))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()


@pytest.mark.parametrize("entity_override", [False, True])
@pytest.mark.parametrize("friendly_name", ["Temperature", 123])
async def test_numeric_selection_snapshot_and_native_area(
    hass, tmp_path, freezer, entity_override, friendly_name
):
    raw = await async_add_physical_cover(hass)
    source_entry = MockConfigEntry(domain="test")
    source_entry.add_to_hass(hass)
    device_area = ar.async_get(hass).async_create("Device room")
    entity_area = ar.async_get(hass).async_create("Sensor room")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("test", "temperature")},
    )
    dr.async_get(hass).async_update_device(device.id, area_id=device_area.id)
    registered = er.async_get(hass).async_get_or_create(
        "sensor", "test", "temperature", suggested_object_id="temperature", device_id=device.id
    )
    if entity_override:
        er.async_get(hass).async_update_entity(registered.entity_id, area_id=entity_area.id)
    attributes = {"unit_of_measurement": "°C", "friendly_name": friendly_name}
    hass.states.async_set(registered.entity_id, "15", attributes)
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "cold": policy(
                "Cold policy",
                input={
                    "type": "qualified_numeric",
                    "entity_id": registered.entity_id,
                    "comparison": "below",
                    "threshold": 16,
                    "unit": "°C",
                    "qualification_seconds": 60,
                },
            )
        },
    )
    await async_activate(hass)
    hass.states.async_set(registered.entity_id, "14", attributes)
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    hass.states.async_set(registered.entity_id, "13", attributes)
    await hass.async_block_till_done()
    source_updated = hass.states.get(registered.entity_id).last_updated.isoformat()
    await advance(hass, freezer, 31)
    snapshot = reason(hass)
    assert snapshot.state == "Cold policy"
    assert raw.commands[-1] == ("position", 40)
    assert snapshot.attributes["source_name"] == "Cold policy"
    assert snapshot.attributes["source_entity_name"] == (
        "Temperature" if isinstance(friendly_name, str) else registered.entity_id
    )
    assert snapshot.attributes["source_entity"] == registered.entity_id
    assert snapshot.attributes["source_area"] == (
        "Sensor room" if entity_override else "Device room"
    )
    assert snapshot.attributes["source_value"] == 13
    assert snapshot.attributes["source_unit"] == "°C"
    assert snapshot.attributes["source_updated_at"] == source_updated
    assert snapshot.attributes["captured_at"] != source_updated
    assert snapshot.attributes["configured_comparison"] == "below"
    assert snapshot.attributes["configured_threshold"] == 16
    assert snapshot.attributes["qualification_seconds"] == 60
    assert snapshot.attributes["target_position"] == 40
    assert snapshot.attributes["target"] == {"position": 40}
    assert (
        "source_value" not in hass.states.get(managed_id(hass, "sensor", key="desired")).attributes
    )
    await advance(hass, freezer, 5)
    hass.states.async_set(registered.entity_id, "12", attributes)
    await hass.async_block_till_done()
    await entry.runtime_data.async_reconcile()
    await hass.async_block_till_done()
    assert reason(hass) is snapshot
    await entry.runtime_data.async_request("roof", target={"position": 60}, duration=5)
    hass.states.async_set(registered.entity_id, "12", {**attributes, "unit_of_measurement": "°F"})
    await hass.async_block_till_done()
    await advance(hass, freezer, 6)
    assert reason(hass).state == "Cold policy"
    assert reason(hass).attributes["source_entity"] == registered.entity_id
    assert reason(hass).attributes["source_value"] is None
    assert reason(hass).attributes["source_updated_at"] is None
    # A new evaluation after reload is not reconstructed admission evidence.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(registered.entity_id, "11", attributes)
    await hass.async_block_till_done()
    assert reason(hass).attributes["source_value"] == 11
    assert reason(hass).attributes["captured_at"] != snapshot.attributes["captured_at"]
    hass.states.async_set(registered.entity_id, "20", attributes)
    await hass.async_block_till_done()
    assert reason(hass).attributes["source_value"] is None
    assert reason(hass).attributes["source_entity"] is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_state_policy_occurrence_replay_expiry_and_invalid_source(hass, tmp_path, freezer):
    await async_add_physical_cover(hass)
    hass.states.async_set("input_boolean.evening", "on", {"friendly_name": "Evening enabled"})
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "evening": policy("Evening", eligibility_entity="input_boolean.evening"),
            "external": policy("External", 10, kind="occurrence"),
        },
    )
    await async_activate(hass)
    first = reason(hass)
    assert first.attributes["source_value"] == "on"
    assert first.attributes["source_entity_name"] == "Evening enabled"
    assert first.attributes["source_area"] is None
    runtime = entry.runtime_data
    expiry = dt_util.utcnow().timestamp() + 10
    await runtime.async_submit_occurrence("external", "one", expiry)
    await hass.async_block_till_done()
    external = reason(hass)
    assert external.state == "External"
    for key in (
        "source_entity",
        "source_entity_name",
        "source_area",
        "source_value",
        "source_unit",
        "source_updated_at",
        "configured_comparison",
        "configured_threshold",
        "qualification_seconds",
    ):
        assert external.attributes[key] is None
    assert external.attributes["expires_at"] == expiry
    await advance(hass, freezer, 2)
    await runtime.async_submit_occurrence("external", "one", expiry)
    await hass.async_block_till_done()
    assert reason(hass) is external
    await advance(hass, freezer, 9)
    assert reason(hass).state == "Evening"
    assert reason(hass).attributes["source_value"] == "on"
    assert reason(hass).attributes["captured_at"] != first.attributes["captured_at"]
    assert reason(hass).attributes["expires_at"] is None
    # An optimistic state may not serve as captured source evidence.
    hass.states.async_set("input_boolean.evening", "on", {"optimistic": True})
    await hass.async_block_till_done()
    assert reason(hass).attributes["source_value"] is None
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("trigger_type", ["state", "numeric_state"])
@pytest.mark.parametrize("command", ["request", "submit_occurrence"])
@pytest.mark.parametrize("trigger_index", [0, 1])
async def test_native_trigger_snapshot_diverges_from_later_state(
    hass, tmp_path, freezer, trigger_type, command, trigger_index
):
    import asyncio
    from contextvars import copy_context
    from unittest.mock import patch

    from homeassistant.core import Context
    from homeassistant.helpers.trace import trace_get, trace_id_get
    from homeassistant.setup import async_setup_component

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(
        hass, tmp_path, policies={"external": policy("External", kind="occurrence")}
    )
    await async_activate(hass)
    runtime = entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()
    captured = {}

    async def hold(call):
        captured["execution"] = copy_context()
        captured["context"] = call.context
        native_steps = trace_get(clear=False)
        captured["native_steps"] = native_steps
        captured["native_id"] = trace_id_get()
        original_step = next(iter(native_steps.values()))[0]
        original_state = original_step.as_dict()["changed_variables"]["trigger"]["to_state"]
        captured["trigger_step"] = original_step
        captured["trigger_state"] = original_state
        assert runtime._automation_trigger_details(call.context)["source_value"] == "919.68"
        assert trace_get(clear=False) is native_steps
        assert next(iter(native_steps.values()))[0] is original_step
        assert original_step.as_dict()["changed_variables"]["trigger"]["to_state"] is original_state
        assert (
            runtime._automation_trigger_details(Context(parent_id=call.context.parent_id)) is None
        )  # Same parent is insufficient: it must be the exact active run.
        assert (
            runtime._automation_trigger_details(Context(id=call.context.id, parent_id="unrelated"))
            is None
        )
        # Optional internal evidence must tolerate a missing/changed trace shape.
        for unavailable in (None, {}, {"action/0": ()}):
            with patch("homeassistant.helpers.trace.trace_get", return_value=unavailable):
                assert runtime._automation_trigger_details(call.context) is None
        with patch.dict("sys.modules", {"homeassistant.components.trace.const": None}):
            assert runtime._automation_trigger_details(call.context) is None
        entered.set()
        await release.wait()

    hass.services.async_register("test", "hold", hold)
    expiry = dt_util.utcnow().timestamp() + 20
    data = (
        {
            "resource_id": "roof",
            "target": {"position": 40},
            "request_id": "one",
            "expires_at": expiry,
        }
        if command == "request"
        else {"policy_id": "external", "occurrence_id": "one", "expires_at": expiry}
    )
    trigger = {"trigger": trigger_type, "entity_id": "sensor.room_co2"}
    trigger.update({"to": "919.68"} if trigger_type == "state" else {"above": 900})
    triggers = ([{**trigger, "entity_id": "sensor.never"}] if trigger_index else []) + [trigger]
    hass.states.async_set("sensor.room_co2", "800", {"unit_of_measurement": "ppm"})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "capture",
                    "alias": "Capture",
                    "triggers": triggers,
                    "actions": [
                        {"action": "test.hold"},
                        {"action": f"ha_operator.{command}", "data": data},
                    ],
                }
            ]
        },
    )
    hass.states.async_set("sensor.room_co2", "919.68", {"unit_of_measurement": "ppm"})
    await asyncio.wait_for(entered.wait(), 5)
    source_updated = hass.states.get("sensor.room_co2").last_updated.isoformat()
    hass.states.async_set("sensor.room_co2", "1293.44", {"unit_of_measurement": "ppm"})
    release.set()
    await hass.async_block_till_done()
    snapshot = reason(hass)
    assert snapshot.attributes["source_capture"] == "automation_trigger"
    assert snapshot.context.parent_id == captured["context"].id
    assert snapshot.attributes["source_entity"] == "sensor.room_co2"
    assert snapshot.attributes["source_value"] == "919.68"
    assert snapshot.attributes["source_unit"] == "ppm"
    assert snapshot.attributes["source_updated_at"] == source_updated
    assert snapshot.attributes["configured_threshold"] is None
    assert hass.states.get("sensor.room_co2").state == "1293.44"
    assert len(runtime._ingress_details) == 1
    assert all(
        value is None or isinstance(value, (str, int, float, bool))
        for details in runtime._ingress_details.values()
        for value in details.values()
    )
    # The actual completed native trace remains in this copied context, but is stopped.
    assert (
        captured["execution"].run(runtime._automation_trigger_details, captured["context"]) is None
    )
    parent_steps = captured["execution"].run(trace_get, clear=False)
    assert parent_steps is captured["native_steps"]
    assert captured["execution"].run(trace_id_get) == captured["native_id"]
    assert next(iter(parent_steps.values()))[0] is captured["trigger_step"]
    assert (
        captured["trigger_step"].as_dict()["changed_variables"]["trigger"]["to_state"]
        is captured["trigger_state"]
    )
    assert "child_id" not in parent_steps["action/1"][0].as_dict()
    await hass.services.async_call("ha_operator", command, data, blocking=True)
    await hass.async_block_till_done()
    assert reason(hass) is snapshot  # A replay without trace cannot replace the evidence.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert reason(hass).attributes["source_value"] is None
    assert reason(hass).attributes["source_capture"] is None
    await advance(hass, freezer, 21)
    assert reason(hass).attributes["source_capture"] is None
    assert runtime._ingress_details == {}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_script_trace_does_not_enrich_request(hass, tmp_path):
    from homeassistant.components.script.trace import trace_script
    from homeassistant.core import Context
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers.script import Script
    from homeassistant.helpers.trace import trace_get, trace_path

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    sequence = cv.SCRIPT_SCHEMA(
        [
            {
                "action": "ha_operator.request",
                "data": {"resource_id": "roof", "target": {"position": 40}},
            }
        ]
    )
    runner = Script(hass, sequence, "Direct script", "test")
    context = Context()
    with trace_script(
        hass, "direct_script", {"sequence": sequence}, None, context, {"stored_traces": 1}
    ) as trace:
        trace.set_trace(trace_get())
        with trace_path("sequence"):
            await runner.async_run(context=context)
    await runner.async_unload()
    await hass.async_block_till_done()
    assert reason(hass).attributes["source_capture"] is None
    assert entry.runtime_data._ingress_details == {}
    await hass.config_entries.async_unload(entry.entry_id)
