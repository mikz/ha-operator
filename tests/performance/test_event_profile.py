"""Opt-in, offline measurement of a packaged integration in native HA fixtures.

Run through scripts/profile_event_load.py, which supplies a fresh extracted ZIP
for each process. No production configuration or credentials are accepted.
"""

from __future__ import annotations

import asyncio
import cProfile
import gc
import json
import os
import platform
import statistics
import time
import tracemalloc
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import pytest
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import EVENT_STATE_CHANGED, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_report_event
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator.const import DOMAIN

_NATIVE_LOAD = Store._async_load
_NATIVE_WRITE = Store._async_write_data


@pytest.fixture(autouse=True)
def native_operator_storage(hass_storage):
    with pytest.MonkeyPatch.context() as patcher:
        for method, native in (("_async_load", _NATIVE_LOAD), ("_async_write_data", _NATIVE_WRITE)):
            mocked = getattr(Store, method)

            def route(native=native, mocked=mocked):
                async def dispatch(store, *args, **kwargs):
                    return await (native if store.key.startswith("ha_operator.") else mocked)(
                        store, *args, **kwargs
                    )

                return dispatch

            patcher.setattr(Store, method, route())
        yield


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Enable only integrations installed in this disposable working directory."""


async def test_profile(hass, tmp_path):
    output = Path(os.environ["OPERATOR_PROFILE_OUTPUT"])
    workload = os.environ["OPERATOR_PROFILE_WORKLOAD"]
    measure = os.environ["OPERATOR_PROFILE_MEASURE"]
    count = int(os.environ["OPERATOR_PROFILE_COUNT"])
    # Match production scheduling rather than pytest's expensive asyncio debug.
    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    loop.set_debug(False)
    hass.config.config_dir = str(tmp_path)
    followers = workload.startswith("followers_")
    raw_ids = [
        f"{'switch' if followers else 'cover'}.profile_raw_{index}"
        for index in range(15 if followers else 4)
    ]

    def report(position=0):
        hass.states.async_set(
            raw_ids[0],
            "open" if position else "closed",
            {"current_position": position, "supported_features": 15},
        )

    counts = Counter()

    class FeedbackSwitch(SwitchEntity):
        _attr_should_poll = False
        _attr_is_on = False

        def __init__(self, entity_id):
            self.entity_id = entity_id
            self._attr_unique_id = entity_id

        async def async_turn_on(self, **kwargs):
            counts["actuator_commands"] += 1
            self._attr_is_on = True
            self.async_write_ha_state()

        async def async_turn_off(self, **kwargs):
            counts["actuator_commands"] += 1
            self._attr_is_on = False
            self.async_write_ha_state()

    for entity_id in raw_ids if not followers else ():
        hass.states.async_set(
            entity_id, "closed", {"current_position": 0, "supported_features": 15}
        )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Profile",
        data={},
        options={},
        subentries_data=[
            {
                "subentry_id": f"resource{index}",
                "subentry_type": "resource",
                "title": f"Profile {index}",
                "data": {
                    "name": f"Profile {index}",
                    "kind": "switch" if followers else "cover",
                    "entity_id": entity_id,
                    **({"manual_control": False, "command_interval": 0.001} if followers else {}),
                },
                "unique_id": None,
            }
            for index, entity_id in enumerate(raw_ids)
        ],
    )
    entry.add_to_hass(hass)
    if followers:
        assert await async_setup_component(hass, "switch", {})
        await hass.data["switch"].async_add_entities([FeedbackSwitch(key) for key in raw_ids])
        from homeassistant.config_entries import ConfigSubentry

        for key, targets in (("room", []), ("central", ["room"])):
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    subentry_id=key,
                    subentry_type="intent",
                    title=key,
                    unique_id=None,
                    data={"name": key, "initial_value": False, "on_targets": targets},
                ),
            )
        for index in range(15):
            key = f"follow{index}"
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    subentry_id=key,
                    subentry_type="policy",
                    title=key,
                    unique_id=None,
                    data={
                        "name": key,
                        "kind": "state",
                        "resource_id": f"resource{index}",
                        "intent_id": "room" if index < 4 else "central",
                    },
                ),
            )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    if followers:
        for resource_id in runtime.resources:
            await runtime.async_set_mode(resource_id, "live")
        source_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, "central_desired")
        await hass.async_block_till_done()
    managed_ids = {
        state.entity_id for state in hass.states.async_all() if state.entity_id not in set(raw_ids)
    }

    @callback
    def publication(event):
        if event.data["entity_id"] in managed_ids:
            counts[event.event_type] += 1

    @callback
    def command(call):
        counts["actuator_commands"] += 1

    hass.services.async_register("cover", "set_cover_position", command)
    remove_changed = hass.bus.async_listen(EVENT_STATE_CHANGED, publication)
    remove_reported = async_track_state_report_event(hass, managed_ids, publication)
    original_recompute = runtime._recompute

    def recompute():
        counts["recomputations"] += 1
        return original_recompute()

    runtime._recompute = recompute

    async def batch(index):
        if workload == "followers_commands":
            await hass.services.async_call(
                "switch",
                "turn_on" if index % 2 else "turn_off",
                {"entity_id": source_id},
                blocking=True,
            )
        elif workload == "followers_reports":
            for entity_id in raw_ids:
                current = hass.states.get(entity_id)
                hass.states.async_set(entity_id, current.state, current.attributes)
        elif workload == "unchanged":
            report()
        elif workload == "changed_burst":
            # Twenty-five changes in one loop turn; alternate the final position.
            for position in range(1, 26):
                report(position if index % 2 else 100 - position)
        await hass.async_block_till_done()

    # Warm platforms, caches, and the same workload before the measured interval.
    for index in range(10):
        await batch(index)
    counts.clear()
    gc.collect()
    tasks_before = len(asyncio.all_tasks())
    profiler = cProfile.Profile() if measure == "cprofile" else None
    if measure == "memory":
        tracemalloc.start()
        allocated_before = tracemalloc.get_traced_memory()[0]
    if profiler:
        profiler.enable()
    latencies = []
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    for index in range(count):
        started = time.perf_counter()
        await batch(index)
        latencies.append(time.perf_counter() - started)
    elapsed_cpu = time.process_time() - started_cpu
    elapsed_wall = time.perf_counter() - started_wall
    if profiler:
        profiler.disable()
    tasks_after = len(asyncio.all_tasks())
    memory = None
    if measure == "memory":
        # Exclude the harness's retained latency samples from retained allocations.
        latencies.clear()
        gc.collect()
        allocated, peak = tracemalloc.get_traced_memory()
        memory = {"retained_bytes": allocated - allocated_before, "peak_bytes": peak}
        tracemalloc.stop()
    result = {
        "workload": workload,
        "measurement": measure,
        "measurement_boundary": "workload only; final unload save excluded",
        "batches": count,
        "input_events": count
        * (25 if workload == "changed_burst" else 15 if workload == "followers_reports" else 1),
        "entity_count": len(managed_ids),
        "counts": dict(counts),
        "wall_seconds": elapsed_wall,
        "cpu_seconds": elapsed_cpu,
        "median_batch_ms": statistics.median(latencies) * 1000 if latencies else None,
        "p95_batch_ms": sorted(latencies)[int(len(latencies) * 0.95)] * 1000 if latencies else None,
        "max_batch_ms": max(latencies) * 1000 if latencies else None,
        "memory": memory,
        "task_count_before": tasks_before,
        "task_count_after": tasks_after,
        "worker_count": len(runtime._tasks),
        "python": platform.python_version(),
        "ha_version": version("homeassistant"),
        "fixture_version": version("pytest-homeassistant-custom-component"),
        "asyncio_debug": False,
        "scope": "native HA fixtures; live simulated feedback switches for followers, "
        "observe mode for covers; no Recorder, KNX, or full-house load; "
        "memory measured after workload and garbage collection",
    }

    # Verify actual writer I/O outside the measured workload. Unload performs
    # the candidate's ordinary final Store save; it is not part of timing.
    await hass.config_entries.async_unload(entry.entry_id)
    native_store = getattr(runtime, "_store", None)
    snapshot_path = Path(native_store.path if native_store else runtime.store._path)
    assert await hass.async_add_executor_job(snapshot_path.is_file)
    result["storage_backend"] = "native Store" if native_store else "baseline snapshot"
    result["saved_file_verified_after_unload"] = True

    # Output and profile serialization are excluded from the measured interval.
    def save():
        output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        if profiler:
            profiler.dump_stats(str(output.with_suffix(".cprof")))

    await hass.async_add_executor_job(save)
    remove_changed()
    remove_reported()
    loop.set_debug(previous_debug)
    if workload != "followers_commands":
        assert counts["actuator_commands"] == 0
