"""Public HA lifecycle fences native timer intent before a replacement starts."""

import asyncio
import json
import threading
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState, OperationNotAllowed
from homeassistant.core import CoreState, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

from custom_components.ha_operator import storage
from custom_components.ha_operator.const import DOMAIN
from custom_components.ha_operator.policy_inputs import NumericState, TimerState
from tests.integration.helpers import (
    async_activate,
    async_add_physical_cover,
    async_setup_operator,
    managed_id,
    operator_entry,
)
from tests.integration.test_policy_inputs_runtime import (
    numeric_policy,
    temperature,
    timer_policy,
    timer_service,
)


@pytest.fixture(autouse=True)
async def cleanup_timer(hass):
    yield
    if hass.services.has_service("timer", "cancel") and not hass.is_stopping:
        await timer_service(hass, "cancel")
        await hass.async_block_till_done()


async def advance(hass, freezer, seconds):
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=seconds))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def setup(hass, tmp_path, *, numeric=False, extra=None, trace=False):
    raw = await async_add_physical_cover(hass)
    assert await async_setup_component(
        hass, "timer", {"timer": {"ventilation": {"duration": "00:10:00", "restore": True}}}
    )
    policies = timer_policy() | (numeric_policy() if numeric else {})
    if trace:
        hass.config.config_dir = str(tmp_path)
        entry = operator_entry(policies=policies, **(extra or {}))
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, options={"trace_enabled": True})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    else:
        entry = await async_setup_operator(hass, tmp_path, policies=policies, **(extra or {}))
    await async_activate(hass)
    return raw, entry


async def start(hass, entry, freezer, *, accepted=True):
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    if accepted:
        await advance(hass, freezer, 60)
    return entry.runtime_data.policy_input("vent")


def disk(entry):
    return json.loads(entry.runtime_data.store._path.read_text())["data"]


@pytest.mark.parametrize("accepted", [True, False])
async def test_public_reload_suppresses_timer_episode_and_keeps_one_worker(
    hass, tmp_path, freezer, accepted
):
    raw, entry = await setup(hass, tmp_path)
    episode = await start(hass, entry, freezer, accepted=accepted)
    logs = []

    @callback
    def record(event):
        if "interrupted" in event.data.get("message", ""):
            logs.append(event.data)

    remove = hass.bus.async_listen("logbook_entry", record)
    try:
        before = list(raw.commands)
        for _ in range(3):
            old = entry.runtime_data
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            runtime = entry.runtime_data
            assert runtime is not old and old._closed
            assert all(task.done() for task in old._tasks)
            assert len(runtime._tasks) == 1 and not runtime._tasks[0].done()
            assert runtime.policy_input("vent").phase == "suppressed"
            assert runtime.policy_input("vent").episode_id == episode.episode_id
            assert all(item["skipped"] for item in runtime.store.state["occurrences"].values())
            assert hass.states.get("timer.ventilation").state == "active"
        assert raw.commands == before
        assert len(logs) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        remove()


async def test_entity_registry_enable_schedules_native_reload_and_interrupts_timer(
    hass, tmp_path, freezer
):
    raw, entry = await setup(hass, tmp_path)
    await start(hass, entry, freezer)
    old = entry.runtime_data
    before = list(raw.commands)
    registry = er.async_get(hass)
    diagnostic = registry.async_get_entity_id("sensor", DOMAIN, "vent_input_phase")
    assert registry.async_get(diagnostic).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    registry.async_update_entity(diagnostic, disabled_by=None)
    await hass.async_block_till_done()
    assert entry.runtime_data is old
    await advance(hass, freezer, 31)
    assert entry.runtime_data is not old and old._closed
    assert all(task.done() for task in old._tasks)
    assert entry.runtime_data.policy_input("vent").phase == "suppressed"
    assert hass.states.get(diagnostic).state == "suppressed"
    assert raw.commands == before
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("accepted", [True, False])
async def test_ha_shutdown_preserves_accepted_expiry_discards_pending_on_restart(
    hass, tmp_path, freezer, accepted
):
    raw, entry = await setup(hass, tmp_path)
    episode = await start(hass, entry, freezer, accepted=accepted)
    old = entry.runtime_data
    await hass.async_stop(force=True)
    assert old._closed and old._close_interrupts_timer_inputs is False
    assert all(task.done() for task in old._tasks)
    saved = disk(entry)
    assert saved["policy_inputs"]["vent"]["state"]["expires_at"] == episode.expires_at
    assert saved["policy_inputs"]["vent"]["state"]["phase"] == (
        "accepted" if accepted else "qualifying"
    )
    # Restart the native config entry using its same snapshot and existing HA test host.
    # Mark stopping for the unload; the first shutdown boundary remains authoritative.
    hass.set_state(CoreState.stopping)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(CoreState.running)
    raw.async_write_ha_state()  # Fresh physical feedback, not restored virtual intent.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fresh = entry.runtime_data
    assert fresh.policy_input("vent").phase == ("accepted" if accepted else "suppressed")
    assert fresh.policy_input("vent").expires_at == episode.expires_at
    if accepted:
        await timer_service(hass, "finish")
        await hass.async_block_till_done()
        assert fresh.policy_input("vent").phase == "accepted"
        assert fresh.policy_input("vent").expires_at == episode.expires_at
    else:
        await advance(hass, freezer, 61)
        assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_numeric_recovery_keeps_due_but_requires_fresh_numeric_report(
    hass, tmp_path, freezer
):
    raw, entry = await setup(hass, tmp_path, numeric=True)
    temperature(hass, 15)
    await hass.async_block_till_done()
    due = entry.runtime_data.policy_input("cold").due_at
    await advance(hass, freezer, 60)
    assert entry.runtime_data.policy_input("cold").qualified
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    fresh = entry.runtime_data
    assert fresh.policy_input("cold").due_at == due
    assert fresh.policy_input("cold").recovery_pending
    before = list(raw.commands)
    temperature(hass, "unknown")
    await hass.async_block_till_done()
    assert raw.commands == before and fresh.policy_input("cold").recovery_pending
    temperature(hass, 15)
    await hass.async_block_till_done()
    assert fresh.policy_input("cold").qualified and not fresh.policy_input("cold").recovery_pending
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_interruption_returns_failed_unload_and_retains_visible_inhibition(
    hass, tmp_path, freezer, monkeypatch
):
    raw, entry = await setup(hass, tmp_path)
    await start(hass, entry, freezer)
    old = entry.runtime_data
    before = list(raw.commands)

    def fail(*_):
        raise OSError("interruption save failed")

    monkeypatch.setattr(storage, "_write_snapshot", fail)
    assert not await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert entry.runtime_data is old and old._closed
    assert old.fault == "storage_error"
    assert all(task.done() for task in old._tasks)
    status = hass.states.get(managed_id(hass, "sensor", key="status"))
    assert status.state == "fault" and status.attributes["reason"] == "storage_error"
    managed = hass.states.get(managed_id(hass, "cover"))
    assert managed.state == "unavailable"
    assert managed.attributes.get("current_position") is None
    observed = hass.states.get(managed_id(hass, "sensor", key="observed"))
    assert observed.state == "unavailable" and observed.attributes.get("target") is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
    with pytest.raises(HomeAssistantError, match="unloaded"):
        await old.async_set_policy_enabled("vent", False)
    assert not await old.async_close(interrupt_timer_inputs=True)  # Cached failure remains failure.
    with pytest.raises(OperationNotAllowed, match="non recoverable"):
        await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data is old
    assert disk(entry)["policy_inputs"]["vent"]["state"]["phase"] == "accepted"
    assert raw.commands == before


@pytest.mark.parametrize("cancel_close", [False, True])
async def test_reload_drains_admission_then_fences_and_rejects_queued_mutator(
    hass, tmp_path, freezer, monkeypatch, cancel_close
):
    raw, entry = await setup(hass, tmp_path)
    await start(hass, entry, freezer, accepted=False)
    old = entry.runtime_data
    entered, release = threading.Event(), threading.Event()
    real_write = storage._write_snapshot
    revisions = []

    def gate(path, state):
        phase = state["policy_inputs"]["vent"]["state"]["phase"]
        revisions.append((state["revision"], phase))
        if phase == "accepted":
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=60))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    queued = closing = None
    close_entered = asyncio.Event()
    original_close = old._async_close

    async def closing_cleanup():
        close_entered.set()
        return await original_close()

    monkeypatch.setattr(old, "_async_close", closing_cleanup)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        queued = asyncio.create_task(old.async_set_policy_enabled("vent", False))
        await asyncio.sleep(0)
        closing = asyncio.create_task(
            old.async_close(interrupt_timer_inputs=True)
            if cancel_close
            else hass.config_entries.async_reload(entry.entry_id)
        )
        await asyncio.wait_for(close_entered.wait(), 2)
        assert old._closed
        if cancel_close:
            closing.cancel()
        assert raw.commands == []
        # Native events close to unload cannot enter the fenced runtime.
        await timer_service(hass, "cancel")
        await timer_service(hass, "start")
        assert not old._policy_events
    finally:
        release.set()
    try:
        with pytest.raises(HomeAssistantError, match="unloaded"):
            await queued
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert old.store.state["policy_inputs"]["vent"]["state"]["phase"] == "suppressed"
            assert await hass.config_entries.async_reload(entry.entry_id)
        else:
            assert await closing
        await hass.async_block_till_done()
        assert revisions[:2] == [(revisions[0][0], "accepted"), (revisions[0][0] + 1, "suppressed")]
        assert entry.runtime_data is not old
        assert all(task.done() for task in old._tasks)
        assert entry.runtime_data.policy_enabled("vent")  # Queued disable never committed.
        assert entry.runtime_data.policy_input("vent").phase == "suppressed"
        assert raw.commands == []
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        # Assertion failures (including deliberate mutants) must not leave the
        # replacement runtime or closing task alive and obscure the causal result.
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_unload_trace_uses_existing_policy_input_admission_contract(hass, tmp_path, freezer):
    from custom_components.ha_operator.shadow import alias
    from tests.lab.shadow_trace import validate_trace

    _, entry = await setup(hass, tmp_path, trace=True)
    await start(hass, entry, freezer)
    old = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    exported = await old.async_export_trace(limit=1000)
    assert validate_trace(exported).records
    inputs = [
        row["data"]
        for row in exported["records"]
        if row["kind"] == "admission" and row["data"]["action"] == "policy_input"
    ]
    assert inputs[-1]["policy_id"] == alias("vent", "p_")
    assert inputs[-1]["input"]["state"]["phase"] == "suppressed"
    assert inputs[-1]["occurrences"][0]["skipped"]


async def test_unload_keeps_unrelated_manual_fan_and_sleep_intent(hass, tmp_path, freezer):
    hass.states.async_set("fan.raw", "off", {"supported_features": 1, "percentage": 0})
    hass.states.async_set("binary_sensor.fan_guard", "on")
    extra = {
        "resources": {
            "roof": {"name": "Roof", "kind": "cover", "entity_id": "cover.physical_roof"},
            "fan": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 50},
                "restriction_entity": "binary_sensor.fan_guard",
            },
        },
        "intents": {"sleep": {"name": "Sleep", "initial_value": False}},
    }
    _, entry = await setup(hass, tmp_path, extra=extra)
    runtime = entry.runtime_data
    await runtime.async_set_desired("sleep", True)
    await runtime.async_set_mode("fan", "live")
    await runtime.async_request("fan", target={"on": True, "percentage": 70}, indefinite=True)
    await runtime.async_request("roof", target={"position": 40}, indefinite=True)
    await start(hass, entry, freezer)
    saved = runtime.store.state
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    current = entry.runtime_data.store.state
    assert current["intents"] == saved["intents"] == {"sleep": True}
    assert current["manuals"] == saved["manuals"]
    assert current["manuals"]["fan"]["fan_settings"]["percentage"] == 70
    assert current["modes"]["fan"] == "live"
    assert entry.runtime_data.policy_input("vent").phase == "suppressed"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("stage", ["admission", "cancel"])
async def test_public_shutdown_drains_inflight_save_before_recovery(
    hass, tmp_path, freezer, monkeypatch, stage
):
    raw, entry = await setup(hass, tmp_path)
    episode = await start(hass, entry, freezer, accepted=stage == "cancel")
    old = entry.runtime_data
    before = list(raw.commands)
    entered, release = threading.Event(), threading.Event()
    close_entered = asyncio.Event()
    real_write = storage._write_snapshot
    real_close = old._async_close

    async def cleanup():
        close_entered.set()
        return await real_close()

    monkeypatch.setattr(old, "_async_close", cleanup)
    expected = "accepted" if stage == "admission" else "suppressed"

    def gate(path, state):
        if state["policy_inputs"]["vent"]["state"]["phase"] == expected:
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    if stage == "admission":
        freezer.move_to(dt_util.utcnow() + timedelta(seconds=60))
        async_fire_time_changed_exact(hass, dt_util.utcnow())
    else:
        await timer_service(hass, "cancel")
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stopping = asyncio.create_task(hass.async_stop(force=True))
        await asyncio.wait_for(close_entered.wait(), 2)
        assert old._closed and not old._close_interrupts_timer_inputs
        assert raw.commands == before
    finally:
        release.set()
    await stopping
    saved = disk(entry)["policy_inputs"]["vent"]["state"]
    assert saved["phase"] == expected and saved["expires_at"] == episode.expires_at
    assert all(task.done() for task in old._tasks)
    hass.set_state(CoreState.stopping)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(CoreState.running)
    raw.async_write_ha_state()
    monkeypatch.setattr(storage, "_write_snapshot", real_write)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.policy_input("vent").phase == expected
    assert entry.runtime_data.policy_input("vent").expires_at == episode.expires_at
    if stage == "cancel":
        assert raw.commands == before
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("configured_type", ["timer_episode", "qualified_numeric"])
async def test_saved_input_type_matches_current_fingerprint_at_startup(
    hass, tmp_path, configured_type
):
    """A correlated saved input must fail through actionable storage readiness."""
    raw, entry = await setup(hass, tmp_path, numeric=True)
    runtime = entry.runtime_data
    policy_id = "vent" if configured_type == "timer_episode" else "cold"
    fingerprint = runtime._input_fingerprints[policy_id]
    saved = disk(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    other = NumericState() if configured_type == "timer_episode" else TimerState()
    saved["policy_inputs"][policy_id] = {
        "type": "qualified_numeric" if configured_type == "timer_episode" else "timer_episode",
        "fingerprint": fingerprint,
        "state": other.to_record(),
    }
    path = runtime.store._path
    contents = json.dumps({"version": 3, "data": saved})
    await hass.async_add_executor_job(path.write_text, contents)
    before = list(raw.commands)
    try:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
        assert entry.runtime_data._closed and not entry.runtime_data._tasks
        assert path.read_text() == contents and raw.commands == before
    finally:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("configured_type", ["timer_episode", "qualified_numeric"])
async def test_changed_input_fingerprint_recovers_without_replaying_old_type(
    hass, tmp_path, configured_type
):
    """Legitimate native input type changes retain timer suppression behavior."""
    raw, entry = await setup(hass, tmp_path, numeric=True)
    runtime = entry.runtime_data
    policy_id = "vent" if configured_type == "timer_episode" else "cold"
    old_policy_id = "cold" if configured_type == "timer_episode" else "vent"
    saved = disk(entry)
    expiry = dt_util.utcnow().timestamp() + 1800
    old = (
        NumericState(due_at=expiry, qualified=True)
        if configured_type == "timer_episode"
        else TimerState(episode_id="old-episode", phase="accepted", expires_at=expiry)
    )
    saved["policy_inputs"][policy_id] = {
        "type": "qualified_numeric" if configured_type == "timer_episode" else "timer_episode",
        "fingerprint": runtime._input_fingerprints[old_policy_id],
        "state": old.to_record(),
    }
    if configured_type == "qualified_numeric":
        saved["occurrences"][json.dumps([policy_id, "old-episode"])] = {
            "policy_id": policy_id,
            "occurrence_id": "old-episode",
            "expires_at": expiry,
            "target": {"position": 100},
        }
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_add_executor_job(
        runtime.store._path.write_text, json.dumps({"version": 3, "data": saved})
    )
    before = list(raw.commands)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        current = entry.runtime_data
        record = current._state["policy_inputs"][policy_id]
        assert record["type"] == configured_type
        assert record["fingerprint"] == current._input_fingerprints[policy_id]
        assert current.policy_input(policy_id).phase == "idle"
        if configured_type == "qualified_numeric":
            assert current._state["occurrences"][json.dumps([policy_id, "old-episode"])]["skipped"]
        assert raw.commands == before
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is None
    finally:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
