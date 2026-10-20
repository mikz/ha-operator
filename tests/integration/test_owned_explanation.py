"""Real Reason publications own bounded native traces without automations."""

import asyncio
import json
from unittest.mock import patch

import pytest
from homeassistant.components.trace.const import DATA_TRACE
from homeassistant.core import Context, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import ExtendedJSONEncoder
from homeassistant.helpers.script import DATA_SCRIPTS
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .helpers import async_activate, async_add_physical_cover, async_setup_operator, managed_id
from .test_provenance import policy


@pytest.fixture(autouse=True)
def _custom_integrations(recorder_db_url, enable_custom_integrations):
    pass


def traces(hass):
    return hass.data[DATA_TRACE]["script.roof_reason"].runs


async def test_first_reason_state_has_native_trace_and_literal_snapshot(
    hass, tmp_path, recorder_mock
):
    from datetime import timedelta

    from homeassistant.components.logbook.helpers import async_determine_event_types
    from homeassistant.components.logbook.processor import EventProcessor
    from pytest_homeassistant_custom_component.common import mock_component
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    await async_add_physical_cover(hass)
    assert await async_setup_component(hass, "script", {})
    mock_component(hass, "frontend")
    assert await async_setup_component(hass, "logbook", {})
    hass.states.async_set("input_boolean.enabled", "on", {"friendly_name": "{{ 2 + 2 }}"})
    initial_events = []
    remove = hass.bus.async_listen("state_changed", initial_events.append)
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "active": policy(
                "{{ 7 * 7 }}",
                conditions=[
                    {"condition": "state", "entity_id": "input_boolean.enabled", "state": "on"}
                ],
            )
        },
    )
    reason_id = managed_id(hass, "sensor", key="reason")
    first = hass.states.get(reason_id)
    assert first.state == "{{ 7 * 7 }}"
    assert len(traces(hass)) == 1
    trace = next(iter(traces(hass).values()))
    initial_reason = next(
        event.data["new_state"] for event in initial_events if event.data["entity_id"] == reason_id
    )
    assert trace.context.id == initial_reason.context.id
    assert all(
        event.data["old_state"].state == event.data["new_state"].state
        for event in initial_events
        if event.data["entity_id"] == reason_id and event.data["old_state"] is not None
    )
    remove()
    assert trace.context.parent_id is None
    extended = trace.as_extended_dict()
    variables = extended["trace"]["sequence/0"][0]["changed_variables"]
    assert variables["selection"]["selection_reason"] == "{{ 7 * 7 }}"
    assert variables["source"]["source_entity_name"] == "{{ 2 + 2 }}"
    assert variables["source"]["source_value"] == "on"
    assert variables["decision"]["candidates"][0]["source"] == "policy:active"
    assert variables["outputs"]["cover.physical_roof"]["state"] == "closed"
    assert not hass.states.async_entity_ids("script")
    assert not hass.states.async_entity_ids("automation")
    er.async_get(hass).async_update_entity(
        managed_id(hass, "sensor", key="observed"), new_entity_id="sensor.renamed_observed"
    )
    await hass.async_block_till_done()
    first = hass.states.get(reason_id)
    assert first.context.id != trace.context.id
    assert len(traces(hass)) == 1
    await async_wait_recording_done(hass)
    processor = EventProcessor(
        hass, async_determine_event_types(hass, [reason_id], None), entity_ids=[reason_id]
    )
    rows = await hass.async_add_executor_job(
        processor.get_events,
        dt_util.utcnow() - timedelta(minutes=1),
        dt_util.utcnow() + timedelta(minutes=1),
    )
    started = next(row for row in rows if row.get("context_id") == trace.context.id)
    # Stock Logbook excludes newly-added states with no old Recorder row.
    # Verify the actual first Recorder row directly, without fabricating a predecessor.
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.db_schema import States, StatesMeta
    from homeassistant.components.recorder.models import ulid_to_bytes_or_none
    from sqlalchemy import select

    def first_recorded_context():
        with get_instance(hass).get_session() as session:
            return session.execute(
                select(States.context_id_bin)
                .join(StatesMeta)
                .where(StatesMeta.entity_id == reason_id)
                .order_by(States.state_id)
            ).scalar()

    assert await get_instance(hass).async_add_executor_job(
        first_recorded_context
    ) == ulid_to_bytes_or_none(trace.context.id)
    assert started["entity_id"] == reason_id
    stable = json.dumps(extended, cls=ExtendedJSONEncoder, sort_keys=True)
    for _ in range(3):
        await entry.runtime_data.async_reconcile()
        hass.states.async_set("input_boolean.enabled", "on", {"friendly_name": "{{ 2 + 2 }}"})
        await hass.async_block_till_done()
    assert hass.states.get(reason_id) is first
    assert len(traces(hass)) == 1
    assert json.dumps(trace.as_extended_dict(), cls=ExtendedJSONEncoder, sort_keys=True) == stable
    hass.states.async_set("input_boolean.enabled", "off")
    await hass.async_block_till_done()
    changed = hass.states.get(reason_id)
    assert changed.state == "No eligible request"
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        processor.get_events,
        dt_util.utcnow() - timedelta(minutes=1),
        dt_util.utcnow() + timedelta(minutes=1),
    )
    state = next(row for row in rows if row.get("state") == changed.state)
    assert state["context_event_type"] == "script_started"
    assert state["context_entity_id"] == reason_id
    assert any(item.context.id == changed.context.id for item in traces(hass).values())
    runtime = entry.runtime_data
    runners = list(runtime._explanation_scripts.values())
    await hass.config_entries.async_unload(entry.entry_id)
    assert not runtime._explanation_tasks
    assert not runtime._explanation_scripts
    assert all(id(runner) not in hass.data[DATA_SCRIPTS] for runner in runners)


async def test_selection_changes_use_one_script_and_five_native_runs(hass, tmp_path):
    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    runtime = entry.runtime_data
    reason_id = managed_id(hass, "sensor", key="reason")
    runners = tuple(runtime._explanation_scripts.values())
    for number in range(8):
        origin = Context()
        await runtime.async_request("roof", target={"position": number + 10}, context=origin)
        state = hass.states.get(reason_id)
        trace = next(
            trace for trace in traces(hass).values() if trace.context.id == state.context.id
        )
        assert trace.context.parent_id == origin.id
    assert len(traces(hass)) == 5
    assert tuple(runtime._explanation_scripts.values()) == runners
    assert len(runtime._explanation_tasks) <= 1
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("failure", ["create", "store", "start", "task"])
async def test_explanation_failure_never_blocks_request_or_dispatch(hass, tmp_path, failure):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    runtime = entry.runtime_data
    if failure == "create":
        await runtime._explanation_scripts.pop("roof").async_unload()
    if failure == "task":
        original = hass.async_create_task

        def unavailable(target, name=None, eager_start=True):
            if name == "ha_operator:explanation":
                raise RuntimeError("unavailable")
            return original(target, name, eager_start)

        mocked = patch.object(hass, "async_create_task", side_effect=unavailable)
    else:
        target = {
            "create": "custom_components.ha_operator.explanation.create_script",
            "store": "homeassistant.components.script.trace.async_store_trace",
            "start": "custom_components.ha_operator.explanation.start_trace",
        }[failure]
        mocked = patch(target, side_effect=RuntimeError("unavailable"))
    with mocked:
        await runtime.async_request("roof", target={"position": 60})
    await hass.async_block_till_done()
    assert raw.commands[-1] == ("position", 60)
    state = hass.states.get(managed_id(hass, "sensor", key="reason"))
    assert state.state == "Manual target"
    assert all(trace.context.id != state.context.id for trace in traces(hass).values())
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("unload", [False, True])
async def test_late_start_is_bounded_unattributed_and_cancelled_on_unload(hass, tmp_path, unload):
    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    runtime = entry.runtime_data
    runner = runtime._explanation_scripts["roof"]
    real_run = runner.async_run
    gate = asyncio.Event()
    events = []
    remove = hass.bus.async_listen("script_started", events.append)

    async def delayed(*args, **kwargs):
        await gate.wait()
        return await real_run(*args, **kwargs)

    with patch.object(runner, "async_run", side_effect=delayed):
        for position in range(60, 68):
            await runtime.async_request("roof", target={"position": position})
        assert len(runtime._explanation_tasks) == 1
        pending = runtime._explanation_tasks["roof"]
        if unload:
            await hass.config_entries.async_unload(entry.entry_id)
            assert pending.cancelled()
            assert id(runner) not in hass.data[DATA_SCRIPTS]
        else:
            gate.set()
            await hass.async_block_till_done()
            assert not runtime._explanation_tasks
            state = hass.states.get(managed_id(hass, "sensor", key="reason"))
            assert all(trace.context.id != state.context.id for trace in traces(hass).values())
            await hass.config_entries.async_unload(entry.entry_id)
    assert events == []  # Never claim a late lifecycle as the cause of the earlier state.
    remove()


async def test_reentrant_selection_never_receives_older_trace_context(hass, tmp_path):
    await async_add_physical_cover(hass)
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "baseline": policy("Baseline"),
            "evening": policy(
                "Evening",
                10,
                conditions=[
                    {"condition": "state", "entity_id": "input_boolean.evening", "state": "on"}
                ],
            ),
            "morning": policy(
                "Morning",
                20,
                conditions=[
                    {"condition": "state", "entity_id": "input_boolean.morning", "state": "on"}
                ],
            ),
        },
    )
    runtime = entry.runtime_data
    started = []

    @callback
    def replace_selection(event):
        started.append(event.context)
        hass.states.async_set("input_boolean.morning", "on")
        hass.async_create_task(runtime.async_reconcile(), eager_start=True)

    remove = hass.bus.async_listen_once("script_started", replace_selection)
    hass.states.async_set("input_boolean.evening", "on")
    await hass.async_block_till_done()
    state = hass.states.get(managed_id(hass, "sensor", key="reason"))
    assert state.state == "Morning"
    assert started and state.context.id != started[0].id
    remove()
    await hass.config_entries.async_unload(entry.entry_id)
