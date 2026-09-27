"""Shadow locks fail closed across native options updates and pending operations."""

from __future__ import annotations

import asyncio
import json

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.ha_operator.runtime import OperatorRuntime

from .helpers import operator_entry


@pytest.fixture
async def shadow_factory(hass, tmp_path):
    """Real stores/workers with raw HA service capture and explicit reload boundaries."""
    hass.config.config_dir = str(tmp_path)
    hass.states.async_set(
        "cover.physical_roof", "closed", {"supported_features": 15, "current_position": 0}
    )
    hass.states.async_set("binary_sensor.schedule", "off")
    commands = []
    instances = []

    async def capture(call):
        commands.append({"service": call.service, "data": dict(call.data)})

    hass.services.async_register("cover", "set_cover_position", capture)
    hass.services.async_register("cover", "stop_cover", capture)

    async def create(*, entry=None, options=None, policies=None):
        if entry is None:
            entry = operator_entry(policies=policies)
            entry.add_to_hass(hass)
        if options is not None:
            hass.config_entries.async_update_entry(entry, options=options)
        runtime = OperatorRuntime(hass, entry)
        entry.runtime_data = runtime
        instances.append(runtime)
        await runtime.async_start()
        await hass.async_block_till_done()
        return runtime

    yield create, commands
    for runtime in instances:
        await runtime.async_close()


async def saved_state(hass, runtime):
    contents = await hass.async_add_executor_job(runtime.store._path.read_text)
    return json.loads(contents)["data"]


async def test_locked_start_durably_demotes_saved_live_mode(hass, shadow_factory):
    create, commands = shadow_factory
    original = await create()
    await original.async_set_mode("roof", "live")
    await original.async_request("roof", target={"position": 70}, indefinite=True)
    await hass.async_block_till_done()
    assert commands[0]["data"]["position"] == 70
    assert (await saved_state(hass, original))["modes"]["roof"] == "live"
    await original.async_close()
    commands.clear()

    locked = await create(entry=original.entry, options={"shadow_lock": True})
    assert locked.shadow_locked and locked.mode("roof") == "observe"
    assert locked.manual("roof").target.position == 70  # Accepted intent is retained.
    assert (await saved_state(hass, locked))["modes"]["roof"] == "observe"
    assert commands == []
    with pytest.raises(ServiceValidationError, match="Shadow lock"):
        await locked.async_set_mode("roof", "live")
    with pytest.raises(ServiceValidationError, match="observe"):
        await locked.async_stop("roof")
    assert commands == []


async def test_current_options_lock_blocks_command_already_waiting_for_actuator(
    hass, shadow_factory
):
    create, commands = shadow_factory
    runtime = await create(
        policies={
            "morning": {
                "name": "Morning",
                "resource_id": "roof",
                "kind": "state",
                "target": {"position": 70},
                "eligibility_entity": "binary_sensor.schedule",
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    actuator = runtime._actuator_locks["roof"]
    await actuator.acquire()
    try:
        hass.states.async_set("binary_sensor.schedule", "on")
        await hass.async_block_till_done()
        assert runtime.decisions["roof"].target.position == 70
        assert "roof" in runtime._applying
        assert commands == []
        # Update options without allowing a reload to close the original worker.
        hass.config_entries.async_update_entry(runtime.entry, options={"shadow_lock": True})
    finally:
        actuator.release()
    await hass.async_block_till_done()
    assert runtime.mode("roof") == "observe"
    assert runtime.decisions["roof"].status == "observe"
    assert commands == [] and runtime.last_commands == {}


async def test_queued_live_mode_rechecks_lock_after_waiting_for_persistence(hass, shadow_factory):
    create, commands = shadow_factory
    runtime = await create()
    entered = asyncio.Event()
    await runtime.store._lock.acquire()

    async def pending_mode_change():
        entered.set()
        await runtime.async_set_mode("roof", "live")

    pending = asyncio.create_task(pending_mode_change())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not pending.done()
        hass.config_entries.async_update_entry(runtime.entry, options={"shadow_lock": True})
    finally:
        runtime.store._lock.release()
    with pytest.raises(ServiceValidationError, match="Shadow lock"):
        await pending
    assert runtime.store.fault is None
    assert runtime.mode("roof") == "observe"
    assert (await saved_state(hass, runtime))["modes"].get("roof", "observe") == "observe"
    assert commands == []


async def test_unlock_before_old_runtime_closes_cannot_resurrect_saved_live_mode(
    hass, shadow_factory
):
    create, commands = shadow_factory
    original = await create()
    await original.async_set_mode("roof", "live")
    hass.config_entries.async_update_entry(original.entry, options={"shadow_lock": True})
    assert original.mode("roof") == "observe"  # Latch the lock in the current runtime.
    assert original.store.state["modes"]["roof"] == "live"
    hass.config_entries.async_update_entry(original.entry, options={"shadow_lock": False})
    assert original.mode("roof") == "observe"
    await original.async_close()
    assert (await saved_state(hass, original))["modes"]["roof"] == "observe"

    unlocked = await create(entry=original.entry)
    assert unlocked.shadow_locked is False
    assert unlocked.mode("roof") == "observe"
    assert commands == []
    await unlocked.async_set_mode("roof", "live")
    assert unlocked.mode("roof") == "live"  # Explicit activation is still available.


async def test_shadow_lock_suppresses_queued_stop_tail(hass, shadow_factory):
    create, commands = shadow_factory
    runtime = await create()
    await runtime.async_set_mode("roof", "live")
    motion_started = asyncio.Event()
    finish_motion = asyncio.Event()
    first_stop = asyncio.Event()

    async def move(call):
        commands.append({"service": call.service, "locked": runtime.shadow_locked})
        motion_started.set()
        await finish_motion.wait()

    async def stop(call):
        commands.append({"service": call.service, "locked": runtime.shadow_locked})
        first_stop.set()

    hass.services.async_register("cover", "set_cover_position", move)
    hass.services.async_register("cover", "stop_cover", stop)
    await runtime.async_request("roof", target={"position": 80}, indefinite=True)
    await asyncio.wait_for(motion_started.wait(), 5)
    stop_request = asyncio.create_task(runtime.async_stop("roof"))
    try:
        await asyncio.wait_for(first_stop.wait(), 5)
        hass.config_entries.async_update_entry(runtime.entry, options={"shadow_lock": True})
    finally:
        finish_motion.set()
    await asyncio.wait_for(stop_request, 5)
    await hass.async_block_till_done()
    assert [command["service"] for command in commands] == ["set_cover_position", "stop_cover"]
    assert all(command["locked"] is False for command in commands)
    assert runtime.manual("roof").mode == "hands_off"
    assert runtime.mode("roof") == "observe"


async def test_failed_close_demotes_by_relatching_options_before_replacement(
    hass, shadow_factory, monkeypatch
):
    from custom_components.ha_operator import storage

    create, commands = shadow_factory
    original = await create()
    await original.async_set_mode("roof", "live")
    hass.config_entries.async_update_entry(original.entry, options={"shadow_lock": True})
    assert original.mode("roof") == "observe"
    hass.config_entries.async_update_entry(original.entry, options={"shadow_lock": False})
    assert original.store.state["modes"]["roof"] == "live"
    write = storage._write_snapshot

    def failed_demotion(*_):
        raise OSError("simulated disk full during close")

    monkeypatch.setattr(storage, "_write_snapshot", failed_demotion)
    await original.async_close()
    assert original.entry.options["shadow_lock"] is True
    assert original.fault == "storage_error"
    assert (await saved_state(hass, original))["modes"]["roof"] == "live"
    assert commands == []

    monkeypatch.setattr(storage, "_write_snapshot", write)
    replacement = await create(entry=original.entry)
    assert replacement.shadow_locked and replacement.mode("roof") == "observe"
    assert (await saved_state(hass, replacement))["modes"]["roof"] == "observe"
    assert commands == []
