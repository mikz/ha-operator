"""Native concurrent calls use durable admission and independent output workers."""

from __future__ import annotations

import asyncio
import json
import threading

from custom_components.ha_operator import (
    binary_sensor,
    button,
    cover,
    fan,
    select,
    sensor,
    storage,
    switch,
)

from .helpers import (
    PhysicalCover,
    async_activate,
    async_add_physical_cover,
    async_setup_operator,
    managed_id,
)


def test_event_driven_platforms_delegate_concurrency_to_runtime():
    assert all(
        platform.PARALLEL_UPDATES == 0
        for platform in (cover, fan, switch, sensor, binary_sensor, select, button)
    )


async def test_native_independent_resource_progress_and_stop_bypass(hass, tmp_path, monkeypatch):
    first = await async_add_physical_cover(hass)
    second = PhysicalCover()
    second.entity_id = "cover.physical_second"
    second._attr_unique_id = "physical_second"
    second._attr_name = "Physical second"
    await hass.data["cover"].async_add_entities([second])
    entry = await async_setup_operator(
        hass,
        tmp_path,
        resources={
            "roof": {"name": "First", "kind": "cover", "entity_id": first.entity_id},
            "second": {"name": "Second", "kind": "cover", "entity_id": second.entity_id},
        },
    )
    runtime = entry.runtime_data
    await async_activate(hass)
    await async_activate(hass, "second")
    entered, release, progressed, stopped = (asyncio.Event() for _ in range(4))
    first_command = first.async_set_cover_position
    second_command = second.async_set_cover_position
    physical_stop = first.async_stop_cover

    async def slow_command(**kwargs):
        entered.set()
        await release.wait()
        await first_command(**kwargs)

    async def independent_command(**kwargs):
        await second_command(**kwargs)
        progressed.set()

    async def emergency_stop(**kwargs):
        await physical_stop(**kwargs)
        stopped.set()

    monkeypatch.setattr(first, "async_set_cover_position", slow_command)
    monkeypatch.setattr(second, "async_set_cover_position", independent_command)
    monkeypatch.setattr(first, "async_stop_cover", emergency_stop)

    async def command(identifier, position):
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": identifier, "position": position},
            blocking=True,
        )

    stopping = None
    try:
        await command(managed_id(hass, "cover"), 80)
        await asyncio.wait_for(entered.wait(), 1)
        await command(managed_id(hass, "cover", "second"), 40)
        await asyncio.wait_for(progressed.wait(), 1)
        assert not release.is_set() and second.commands == [("position", 40)]
        stopping = asyncio.create_task(
            hass.services.async_call(
                "cover", "stop_cover", {"entity_id": managed_id(hass, "cover")}, blocking=True
            )
        )
        await asyncio.wait_for(stopped.wait(), 1)
        assert first.commands == [("stop", None)]
        assert not stopping.done() and runtime.manual("roof").mode == "hands_off"
        release.set()
        await asyncio.wait_for(stopping, 1)
        await hass.async_block_till_done()
        assert first.commands == [("stop", None), ("position", 80), ("stop", None)]
        assert len(runtime._tasks) == 2 and not runtime._stop_tasks
        assert runtime.manual("roof").mode == "hands_off"
    finally:
        release.set()
        if stopping:
            await asyncio.gather(stopping, return_exceptions=True)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_same_resource_requests_acknowledge_in_durable_order(
    hass, tmp_path, monkeypatch
):
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    entry = await async_setup_operator(hass, tmp_path)
    runtime = entry.runtime_data
    await async_activate(hass)
    entered, release = threading.Event(), threading.Event()
    original = storage._write_snapshot
    writes = []

    def delayed_write(path, state):
        position = state["manuals"]["roof"]["target"]["position"]
        if not writes:
            entered.set()
            assert release.wait(5)
        original(path, state)
        writes.append((state["revision"], position))

    monkeypatch.setattr(storage, "_write_snapshot", delayed_write)
    identifier = managed_id(hass, "cover")

    async def command(position):
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": identifier, "position": position},
            blocking=True,
        )

    first = asyncio.create_task(command(25))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(command(75))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert runtime.manual("roof") is None
        release.set()
        await asyncio.gather(first, second)
        await hass.async_block_till_done()
        assert [position for _, position in writes] == [25, 75]
        assert writes[1][0] == writes[0][0] + 1
        state = await hass.async_add_executor_job(runtime.store._path.read_text)
        assert json.loads(state)["data"]["manuals"]["roof"]["target"] == {"position": 75}
        assert runtime.manual("roof").target.position == 75
        assert len(runtime._tasks) == 1 and not runtime._tasks[0].done()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        assert await hass.config_entries.async_unload(entry.entry_id)
