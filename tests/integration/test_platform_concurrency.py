"""Native concurrent calls use runtime admission and independent output workers."""

from __future__ import annotations

import asyncio

from custom_components.ha_operator import (
    binary_sensor,
    button,
    cover,
    fan,
    select,
    sensor,
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


async def test_native_same_resource_requests_keep_event_loop_order(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    entry = await async_setup_operator(hass, tmp_path)
    runtime = entry.runtime_data
    await async_activate(hass)
    identifier = managed_id(hass, "cover")

    async def command(position):
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": identifier, "position": position},
            blocking=True,
        )

    await asyncio.gather(command(25), command(75))
    await hass.async_block_till_done()
    assert runtime.manual("roof").target.position == 75
    assert len(runtime._tasks) == 1 and not runtime._tasks[0].done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data.manual("roof").target.position == 75
    assert await hass.config_entries.async_unload(entry.entry_id)
