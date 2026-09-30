"""Native event, admission, shutdown, and trace/control independence boundaries."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.ha_operator.runtime import OperatorRuntime
from custom_components.ha_operator.shadow import alias, entity_alias

from .helpers import operator_entry

RAW = "cover.physical_roof"
EXTRA = "timer.cellar_manual"
ATTRS = {"current_position": 0, "supported_features": 15}


@pytest.fixture
async def trace_factory(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    hass.states.async_set(RAW, "closed", ATTRS)
    commands, instances = [], []

    async def capture(call):
        commands.append((call.service, dict(call.data), call.context.id))

    hass.services.async_register("cover", "set_cover_position", capture)
    hass.services.async_register("cover", "stop_cover", capture)

    async def create(*, options=None, policies=None, entry=None):
        if entry is None:
            entry = operator_entry(policies=policies)
            entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            entry, options=options or {"trace_enabled": True, "shadow_lock": True}
        )
        runtime = OperatorRuntime(hass, entry)
        entry.runtime_data = runtime
        instances.append(runtime)
        await runtime.async_start()
        await hass.async_block_till_done()
        return runtime

    yield create, commands
    for runtime in instances:
        await runtime.async_close()


async def rows(runtime, kind=None, after=None):
    result = await runtime.async_export_trace(after=after, limit=1000)
    assert not result["more"]
    return [row for row in result["records"] if kind is None or row["kind"] == kind]


async def test_initial_snapshot_extra_allowlist_and_unbound_reports_do_not_wake_control(
    hass, trace_factory
):
    create, commands = trace_factory
    hass.states.async_set(
        EXTRA,
        "active",
        {
            "duration": "0:30:00",
            "finishes_at": "2026-09-27T20:00:00+02:00",
            "friendly_name": "Private cellar",
            "token": "private-credential",
        },
    )
    runtime = await create(
        options={
            "trace_enabled": True,
            "shadow_lock": True,
            "trace_entities": [EXTRA],
        }
    )
    initial = await rows(runtime, "input")
    assert {item["data"]["entity_id"] for item in initial} == {
        entity_alias(RAW),
        entity_alias(EXTRA),
    }
    timer = next(
        item["data"] for item in initial if item["data"]["entity_id"] == entity_alias(EXTRA)
    )
    assert timer["event_type"] == "initial"
    assert timer["attributes"] == {
        "duration": "0:30:00",
        "finishes_at": "2026-09-27T20:00:00+02:00",
    }
    cursor = runtime.trace_health()["last_sequence"]
    with patch.object(runtime, "_recompute", wraps=runtime._recompute) as recompute:
        hass.states.async_set(EXTRA, "paused", {"remaining": "0:10:00"})
        await hass.async_block_till_done()
        recompute.assert_not_called()
    changes = await rows(runtime, after=cursor)
    assert [item["kind"] for item in changes] == ["input"]
    assert changes[0]["data"]["state"] == "paused"
    assert commands == []


async def test_unchanged_reports_copy_event_timestamps_and_context_before_next_mutation(
    hass, trace_factory
):
    create, _ = trace_factory
    runtime = await create()
    original = hass.states.get(RAW)
    original_report = original.last_reported.timestamp()
    cursor = runtime.trace_health()["last_sequence"]
    first_at, second_at = original_report + 1, original_report + 2
    parent = Context()
    first = Context(parent_id=parent.id)
    second = Context()
    # No event-loop yield: HA reuses the same State for both unchanged writes.
    hass.states.async_set(RAW, "closed", ATTRS, context=first, timestamp=first_at)
    hass.states.async_set(RAW, "closed", ATTRS, context=second, timestamp=second_at)
    await hass.async_block_till_done()
    reports = [
        item
        for item in await rows(runtime, "input", after=cursor)
        if item["data"]["event_type"] == "state_reported"
    ]
    assert len(reports) == 2
    assert [item["at"] for item in reports] == [first_at, second_at]
    assert [item["data"]["last_reported"] for item in reports] == [first_at, second_at]
    assert [item["data"]["old_last_reported"] for item in reports] == [original_report, first_at]
    assert reports[0]["data"]["event_context"] == alias(first.id, "context_id_")
    assert reports[0]["data"]["event_parent_context"] == alias(parent.id, "context_id_")
    assert reports[1]["data"]["event_context"] == alias(second.id, "context_id_")
    assert original.context.id not in {first.id, second.id}
    hass.states.async_remove(RAW)
    await hass.async_block_till_done()
    assert (await rows(runtime, "input"))[-1]["data"]["state"] is None


async def test_homekit_bridge_request_is_filtered_observation_without_manual_ownership(
    hass, trace_factory
):
    create, commands = trace_factory
    runtime = await create()
    context = Context()
    hass.bus.async_fire(
        "homekit_state_change",
        {
            "entity_id": RAW,
            "service": "set_cover_position",
            "value": 71,
            "display_name": "Private room",
        },
        context=context,
    )
    hass.bus.async_fire(
        "homekit_state_change",
        {
            "entity_id": "cover.unrelated",
            "service": "open_cover",
            "value": True,
        },
    )
    hass.bus.async_fire(
        "homekit_state_change",
        {
            "entity_id": RAW,
            "service": "private_service",
            "value": "private-data",
        },
    )
    hass.bus.async_fire(
        "homekit_state_change",
        {
            "entity_id": RAW,
            "service": "turn_on",
            "value": {"percentage": 40, "password": "private-password", "position": 20},
        },
    )
    await hass.async_block_till_done()
    recorded = await rows(runtime, "external_command")
    assert len(recorded) == 2
    item = recorded[0]["data"]
    assert item["source"] == "homekit" and item["evidence"] == "bridge_request"
    assert item["context_id"] == alias(context.id, "context_id_")
    assert item["entity_id"] == entity_alias(RAW) and item["value"] == 71
    assert "display_name" not in item
    assert recorded[1]["data"]["value"] == {"percentage": 40, "position": 20}
    assert runtime.manual("roof") is None and commands == []


async def test_admissions_reflect_committed_retries_and_dispatch_has_native_context(
    hass, trace_factory
):
    create, commands = trace_factory
    runtime = await create(options={"trace_enabled": True})
    with pytest.raises(ServiceValidationError, match="observe"):
        await runtime.async_request("roof", target={"position": 40})
    assert await rows(runtime, "admission") == []
    await runtime.async_set_mode("roof", "live")
    receipt = await runtime.async_request("roof", target={"position": 40}, request_id="private-id")
    await hass.async_block_till_done()
    await runtime.async_release("roof")
    assert (
        await runtime.async_request("roof", target={"position": 40}, request_id="private-id")
        == receipt
    )
    admissions = await rows(runtime, "admission")
    assert [item["data"]["action"] for item in admissions] == [
        "set_mode",
        "request",
        "release",
        "request",
    ]
    assert admissions[1]["data"]["manual"]["target"] == {"position": 40}
    assert admissions[-1]["data"]["manual"] is None and admissions[-1]["data"]["replayed"]
    assert runtime.manual("roof") is None
    dispatch = await rows(runtime, "dispatch")
    assert [item["data"]["status"] for item in dispatch] == ["started", "completed"]
    expected_context = alias(commands[0][2], "context_id_")
    assert {item["data"]["context_id"] for item in dispatch} == {expected_context}
    assert dispatch[0]["data"]["data"]["entity_id"] == entity_alias(RAW)
    assert all(item["data"]["domain"] == "cover" for item in dispatch)


async def test_rejected_queued_call_never_becomes_false_admission_from_other_commit(
    hass, trace_factory
):
    create, _ = trace_factory
    runtime = await create(options={"trace_enabled": True})
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 40}, request_id="same-id")
    cursor = runtime.trace_health()["last_sequence"]
    await runtime.store._lock.acquire()
    released = asyncio.create_task(runtime.async_release("roof"))
    rejected = asyncio.create_task(
        runtime.async_request("roof", target={"position": 80}, request_id="same-id")
    )
    await asyncio.sleep(0)
    runtime.store._lock.release()
    await released
    with pytest.raises(ServiceValidationError, match="already used"):
        await rejected
    admissions = await rows(runtime, "admission", after=cursor)
    assert [item["data"]["action"] for item in admissions] == ["release"]


async def test_cancelled_successful_write_still_records_actual_admission(hass, trace_factory):
    create, _ = trace_factory
    runtime = await create(options={"trace_enabled": True})
    await runtime.async_set_mode("roof", "live")
    original = runtime.store.async_update

    async def committed_then_cancelled(mutator):
        await original(mutator)
        raise asyncio.CancelledError

    with patch.object(runtime.store, "async_update", committed_then_cancelled):
        with pytest.raises(asyncio.CancelledError):
            await runtime.async_request("roof", target={"position": 55}, request_id="cancelled")
    assert runtime.manual("roof").target.position == 55
    admission = (await rows(runtime, "admission"))[-1]["data"]
    assert admission["action"] == "request" and admission["manual"]["target"] == {"position": 55}


async def test_trace_write_failure_does_not_inhibit_durable_manual_control(hass, trace_factory):
    create, commands = trace_factory
    runtime = await create(options={"trace_enabled": True})
    await rows(runtime)
    with patch.object(runtime._trace.disk, "append", side_effect=OSError("disk unavailable")):
        await runtime.async_set_mode("roof", "live")
        response = await runtime.async_request("roof", target={"position": 73})
        await hass.async_block_till_done()
        await runtime.async_export_trace()
    assert response["accepted"] and runtime.fault is None
    assert runtime.manual("roof").target.position == 73
    assert commands[0][1]["position"] == 73
    assert not runtime.trace_health()["healthy"] and not runtime.trace_health()["complete"]


async def test_native_shutdown_drains_clean_trace_before_background_task_cancellation(
    hass, trace_factory
):
    create, _ = trace_factory
    runtime = await create()
    await hass.async_start()
    hass.states.async_set(RAW, "open", {**ATTRS, "current_position": 51})
    await hass.async_stop(force=True)
    assert runtime._closed and runtime._trace._writer.done()
    exported = await runtime.async_export_trace(limit=1000)
    assert exported["records"][-1]["kind"] == "session_end"
    assert exported["health"]["complete"] and exported["health"]["queued_records"] == 0
    assert any(
        item["kind"] == "input" and item["data"]["attributes"].get("current_position") == 51
        for item in exported["records"]
    )


async def test_restart_preserves_global_cursor_and_clean_session_boundary(hass, trace_factory):
    create, commands = trace_factory
    first = await create()
    await first.async_close()
    cursor = first.trace_health()["last_sequence"]
    second = await create(entry=first.entry)
    header = (await rows(second, "session_start", after=cursor))[0]
    assert header["sequence"] == cursor + 1 and header["session_id"] != first._trace.session_id
    assert header["data"]["previous_session_closed"] is True
    assert (
        header["data"]["gap_since_previous"]["from"] <= header["data"]["gap_since_previous"]["to"]
    )
    assert second.trace_health()["complete"] and commands == []


async def test_occurrence_queued_before_lock_is_rejected_at_commit(hass, trace_factory):
    create, _ = trace_factory
    runtime = await create(
        options={"trace_enabled": True},
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 80},
            },
        },
    )
    await runtime.async_set_mode("roof", "live")
    await runtime.store._lock.acquire()
    pending = asyncio.create_task(
        runtime.async_submit_occurrence(
            "morning",
            "Monday",
            (dt_util.utcnow() + timedelta(hours=1)).timestamp(),
        )
    )
    await asyncio.sleep(0)
    hass.config_entries.async_update_entry(
        runtime.entry, options={"trace_enabled": True, "shadow_lock": True}
    )
    runtime.store._lock.release()
    with pytest.raises(ServiceValidationError, match="observe"):
        await pending
    assert runtime.store.state["occurrences"] == {}
    assert not any(
        item["data"]["action"] == "submit_occurrence" for item in await rows(runtime, "admission")
    )


async def test_unchanged_recompute_deduplicates_decision_frames(hass, trace_factory):
    create, _ = trace_factory
    runtime = await create()
    before = await rows(runtime, "decision")
    runtime._recompute()
    runtime._recompute()
    assert len(await rows(runtime, "decision")) == len(before)


async def test_native_input_reports_initial_state_and_unit_metadata_are_sanitized(
    hass, trace_factory
):
    source = "sensor.private_native_temperature"
    hass.states.async_set(source, "15", {"unit_of_measurement": "private-native-unit"})
    create, commands = trace_factory
    runtime = await create(
        policies={
            "native-private": {
                "name": "Private native name",
                "resource_id": "roof",
                "kind": "state",
                "target": {"position": 7},
                "input": {
                    "type": "qualified_numeric",
                    "entity_id": source,
                    "comparison": "below",
                    "threshold": 16,
                    "unit": "private-native-unit",
                    "qualification_seconds": 60,
                },
            }
        }
    )
    initial = next(row for row in await rows(runtime) if row["kind"] == "session_start")
    key = alias("native-private", "p_")
    assert initial["data"]["intent"]["policy_inputs"][key]["type"] == "qualified_numeric"
    cursor = runtime.trace_health()["last_sequence"]
    hass.states.async_set(
        source,
        "14",
        {
            "unit_of_measurement": "private-native-unit",
            "friendly_name": "Private native name",
            "token": "native-private-token",
        },
    )
    await hass.async_block_till_done()
    captured = await rows(runtime, after=cursor)
    report = next(
        row
        for row in captured
        if row["kind"] == "input" and row["data"]["entity_id"] == entity_alias(source)
    )
    assert (
        report["data"]["attributes"]["unit_of_measurement"]
        == initial["data"]["config"]["policies"][key]["input"]["unit"]
    )
    import json

    encoded = json.dumps([initial, captured])
    for private in (
        source,
        "private-native-unit",
        "Private native name",
        "native-private",
        "native-private-token",
    ):
        assert private not in encoded
    assert commands == []
