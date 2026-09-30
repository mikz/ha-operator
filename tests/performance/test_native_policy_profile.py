"""Opt-in comparison of five native HA recipe writers and Operator policy inputs.

The runner supplies an extracted integration ZIP and the public recipe module.
All entities, helpers, timers, automations, and storage use offline HA fixtures.
"""

from __future__ import annotations

import asyncio
import cProfile
import gc
import json
import logging
import math
import os
import platform
import statistics
import time
import tracemalloc
from collections import Counter
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

import freezegun.api
import pytest
from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
from homeassistant.core import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_report_event
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)
from window_recipe import cold_automation, overdue_automation, timer_automation

from custom_components.ha_operator import storage
from custom_components.ha_operator.const import DOMAIN
from custom_components.ha_operator.runtime import OperatorRuntime

# Native time-pattern listeners spread their wake within the first half-second.
# Tick at .5 seconds so their ordinary scheduled handles have become due too.
START = "2026-09-30T08:00:00.500000+00:00"
SENSOR = "sensor.profile_temperature"
TIMER = "timer.profile_ventilation"
RAW = [f"cover.profile_raw_{index}" for index in range(3)]
RESOURCES = [f"roof{index}" for index in range(3)]
HELPERS = {
    "input_boolean.profile_timer_ready",
    "input_boolean.profile_cold_ready",
    "input_boolean.profile_cold_qualified",
    "input_text.profile_timer_record",
    "input_text.profile_cold_record",
    *(f"input_text.profile_return_{index}" for index in range(3)),
}
WORKLOADS = ("idle", "unchanged", "changing", "timer", "reload")


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Enable the packaged integration in the disposable working directory."""


async def test_profile(hass, tmp_path, freezer, monkeypatch):
    output = Path(os.environ["OPERATOR_PROFILE_OUTPUT"])
    workload = os.environ["OPERATOR_PROFILE_WORKLOAD"]
    assert workload in WORKLOADS
    measure = os.environ["OPERATOR_PROFILE_MEASURE"]
    count = int(os.environ["OPERATOR_PROFILE_COUNT"])
    warmup = int(os.environ.get("OPERATOR_PROFILE_WARMUP", "10"))
    trace = os.environ["OPERATOR_PROFILE_TRACE"] == "1"
    native = os.environ["OPERATOR_PROFILE_VARIANT"] == "candidate"
    # Freezegun freezes perf_counter too. Its preserved clock measures actual
    # elapsed wall time, while only the HA clock advances through virtual UTC.
    wall_clock = freezegun.api.real_perf_counter
    freezer.move_to(START)
    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    loop.set_debug(False)
    ha_logger = logging.getLogger("homeassistant")
    previous_log_level = ha_logger.level
    ha_logger.setLevel(logging.WARNING)
    hass.config.config_dir = str(tmp_path)
    counts = Counter()

    def temperature(value):
        hass.states.async_set(SENSOR, str(value), {"unit_of_measurement": "°C"})

    def feedback(position):
        for entity_id in RAW:
            hass.states.async_set(
                entity_id,
                "closed" if not position else "open",
                {"current_position": position, "supported_features": 15},
            )

    feedback(7)
    temperature(20)
    assert await async_setup_component(
        hass, "timer", {"timer": {"profile_ventilation": {"duration": "00:01:05"}}}
    )
    if not native:
        assert await async_setup_component(
            hass,
            "input_boolean",
            {
                "input_boolean": {
                    name: {"initial": False}
                    for name in (
                        "profile_timer_ready",
                        "profile_cold_ready",
                        "profile_cold_qualified",
                    )
                }
            },
        )
        assert await async_setup_component(
            hass,
            "input_text",
            {
                "input_text": {
                    name: {"initial": "{}", "max": 255}
                    for name in (
                        "profile_timer_record",
                        "profile_cold_record",
                        *(f"profile_return_{index}" for index in range(3)),
                    )
                }
            },
        )

    original_write = storage._write_snapshot
    original_recompute = OperatorRuntime._recompute

    def write_snapshot(*args, **kwargs):
        counts["operator_snapshot_writes"] += 1
        return original_write(*args, **kwargs)

    def recompute(self, *args, **kwargs):
        counts["recomputations"] += 1
        return original_recompute(self, *args, **kwargs)

    # Patch the class so genuine reloads include replacement runtime instances.
    monkeypatch.setattr(storage, "_write_snapshot", write_snapshot)
    monkeypatch.setattr(OperatorRuntime, "_recompute", recompute)
    subentries = []
    for resource_id, entity_id in zip(RESOURCES, RAW, strict=True):
        data = {
            "name": resource_id,
            "kind": "cover",
            "entity_id": entity_id,
            "manual_control": True,
            "tolerance": 2,
        }
        if native:
            data["return_monitor"] = {"target_at_most": 7, "warning_after_seconds": 300}
        subentries.append((resource_id, "resource", data))
        subentries.append(
            (
                "normal_" + resource_id,
                "policy",
                {
                    "name": "Baseline " + resource_id,
                    "kind": "state",
                    "resource_id": resource_id,
                    "priority": 0,
                    "target": {"position": 7},
                },
            )
        )
    cold = {
        "name": "Cold",
        "kind": "state",
        "resource_id": RESOURCES[0],
        "priority": 50,
        "target": {"position": 7},
    }
    vent = {
        "name": "Ventilation",
        "kind": "occurrence",
        "resource_id": RESOURCES[0],
        "priority": 20,
        "target": {"position": 100},
    }
    if native:
        cold["input"] = {
            "type": "qualified_numeric",
            "entity_id": SENSOR,
            "comparison": "below",
            "threshold": 16,
            "unit": "°C",
            "qualification_seconds": 1800,
        }
        vent["input"] = {
            "type": "timer_episode",
            "entity_id": TIMER,
            "qualification_seconds": 60,
            "request_seconds": 1800,
        }
    else:
        cold["eligibility_entity"] = "input_boolean.profile_cold_qualified"
    subentries.extend((("cold", "policy", cold), ("vent", "policy", vent)))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Native policy profile",
        version=2 if native else 1,
        data={},
        options={"trace_enabled": trace, "trace_entities": [SENSOR, TIMER]},
        subentries_data=[
            {
                "subentry_id": key,
                "subentry_type": kind,
                "title": data["name"],
                "data": data,
                "unique_id": None,
            }
            for key, kind, data in subentries
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    registry = er.async_get(hass)

    def entity(domain, key):
        result = registry.async_get_entity_id(domain, DOMAIN, key)
        assert result is not None
        return result

    automations = set()
    if not native:
        recipes = [
            (
                "profile_timer",
                timer_automation(
                    TIMER,
                    "input_text.profile_timer_record",
                    "vent",
                    entity("cover", "roof0_managed"),
                    "input_boolean.profile_timer_ready",
                    qualify=60,
                    duration=1800,
                ),
            ),
            (
                "profile_cold",
                cold_automation(
                    SENSOR,
                    "input_text.profile_cold_record",
                    "input_boolean.profile_cold_qualified",
                    "input_boolean.profile_cold_ready",
                    threshold=16,
                    qualify=1800,
                ),
            ),
            *[
                (
                    "profile_return_" + resource_id,
                    overdue_automation(
                        resource_id,
                        entity("cover", resource_id + "_managed"),
                        entity("sensor", resource_id + "_desired"),
                        entity("sensor", resource_id + "_status"),
                        f"input_text.profile_return_{index}",
                        baseline=7,
                        timeout=300,
                    ),
                )
                for index, resource_id in enumerate(RESOURCES)
            ],
        ]
        assert len(recipes) == 5
        assert await async_setup_component(
            hass, "automation", {"automation": [{"id": key, **data} for key, data in recipes]}
        )
        automations = {state.entity_id for state in hass.states.async_all("automation")}
        assert len(automations) == 5
        assert all(hass.states.get(entity_id) is not None for entity_id in HELPERS)
    await hass.async_start()
    await hass.async_block_till_done()
    runtime = entry.runtime_data

    @callback
    def command(call):
        counts["actuator_commands"] += 1

    # Commands are acknowledged independently of raw observations. This fixture
    # never turns dispatched intent into simulated physical confirmation.
    for service in ("set_cover_position", "open_cover", "close_cover"):
        hass.services.async_register("cover", service, command)
    for resource_id in RESOURCES:
        await runtime.async_set_mode(resource_id, "live")

    @callback
    def publication(event):
        entity_id = event.data["entity_id"]
        registered = registry.async_get(entity_id)
        if registered and registered.platform == DOMAIN:
            group = "operator"
        elif entity_id in HELPERS and not native:
            group = "helper"
        elif entity_id in automations:
            group = "automation"
        elif entity_id in {*RAW, SENSOR, TIMER}:
            group = "source"
        else:
            return
        counts[group + "_" + event.event_type] += 1

    @callback
    def service_called(event):
        if event.data["domain"] in {"input_text", "input_boolean"}:
            counts["helper_service_calls"] += 1

    @callback
    def automation_run(event):
        if event.data["entity_id"] in automations:
            counts["automation_runs"] += 1

    watched = {
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id in {*RAW, SENSOR, TIMER, *automations, *HELPERS}
        or (
            registry.async_get(state.entity_id)
            and registry.async_get(state.entity_id).platform == DOMAIN
        )
    }
    remove_changed = hass.bus.async_listen(EVENT_STATE_CHANGED, publication)
    remove_reported = async_track_state_report_event(hass, watched, publication)
    remove_service = hass.bus.async_listen(EVENT_CALL_SERVICE, service_called)
    remove_automation = hass.bus.async_listen(EVENT_AUTOMATION_TRIGGERED, automation_run)

    async def advance(seconds=5):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        counts["clock_ticks"] += 1

    async def timer(service):
        await hass.services.async_call("timer", service, {"entity_id": TIMER}, blocking=True)
        await hass.async_block_till_done()
        counts["timer_service_calls"] += 1

    def active_occurrences():
        return [
            item
            for item in runtime._state["occurrences"].values()
            if item["policy_id"] == "vent"
            and not item.get("skipped", False)
            and item["expires_at"] > dt_util.utcnow().timestamp()
        ]

    async def batch(index):
        nonlocal runtime
        if workload == "idle":
            await advance()
        elif workload == "unchanged":
            temperature(20)
            feedback(7)
            current = hass.states.get(TIMER)
            hass.states.async_set(TIMER, current.state, current.attributes)
            await hass.async_block_till_done()
        elif workload == "changing":
            # Below-threshold value changes, plus warm/cold edges in one turn.
            # Legacy actions inspect the final current state, unlike captured
            # native transitions; identical inputs do not imply equal semantics.
            for value in (15, 14, 17, 15):
                temperature(value)
            feedback(100 if index % 2 else 7)
            await hass.async_block_till_done()
            await advance()
        elif workload == "timer":
            # Start, pending restart, pause/resume suppression, cancel, accepted
            # restart, genuine natural finish, and fresh-start cancellation.
            await advance()
            await timer("start")
            await advance()
            await timer("start")
            await timer("pause")
            await timer("start")
            await timer("cancel")
            await advance()
            await timer("start")
            for _ in range(12):
                await advance()
            assert active_occurrences()
            await timer("start")
            for _ in range(13):
                await advance()
            assert hass.states.get(TIMER).state == "idle"
            assert active_occurrences()
            await advance()
            await timer("start")
            await timer("cancel")
            feedback(7)
            await hass.async_block_till_done()
        else:
            assert hass.states.get(TIMER).state == "idle"
            assert not active_occurrences()
            old = runtime
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            runtime = entry.runtime_data
            assert runtime is not old and old._closed
            assert all(task.done() for task in old._tasks)
            temperature(20)
            feedback(7)
            await hass.async_block_till_done()
            assert len(runtime._tasks) == 3
            counts["entry_reloads"] += 1
        assert runtime.fault is None

    # Start the workload after initialization, never on the initialization epoch.
    await advance()
    temperature(20)
    await hass.async_block_till_done()
    for index in range(warmup):
        await batch(index)
    if trace:
        await runtime._trace.queue.join()
    trace_start = runtime.trace_health()
    counts.clear()
    gc.collect()
    tasks_before = len(asyncio.all_tasks())
    virtual_start = dt_util.utcnow().isoformat()
    profiler = cProfile.Profile() if measure == "cprofile" else None
    if measure == "memory":
        tracemalloc.start()
        allocated_before = tracemalloc.get_traced_memory()[0]
    if profiler:
        profiler.enable()
    latencies = []
    started_wall, started_cpu = wall_clock(), time.process_time()
    for index in range(count):
        started = wall_clock()
        await batch(index)
        latencies.append(wall_clock() - started)
    elapsed_cpu = time.process_time() - started_cpu
    elapsed_wall = wall_clock() - started_wall
    if profiler:
        profiler.disable()
    tasks_after = len(asyncio.all_tasks())
    trace_end = runtime.trace_health()
    drain_started_wall, drain_started_cpu = wall_clock(), time.process_time()
    if trace:
        await runtime._trace.queue.join()
    drain_wall = wall_clock() - drain_started_wall
    drain_cpu = time.process_time() - drain_started_cpu
    memory = None
    if measure == "memory":
        latencies.clear()
        gc.collect()
        allocated, peak = tracemalloc.get_traced_memory()
        memory = {"retained_bytes": allocated - allocated_before, "peak_bytes": peak}
        tracemalloc.stop()
    if workload in {"idle", "unchanged"}:
        assert counts["operator_snapshot_writes"] == 0
    if workload == "idle":
        assert counts["recomputations"] == 0
        assert counts["automation_runs"] == (0 if native else 5 * count)
    metrics = dict.fromkeys(
        (
            "operator_snapshot_writes",
            "recomputations",
            "automation_runs",
            "helper_service_calls",
            "actuator_commands",
            "clock_ticks",
            "timer_service_calls",
            "entry_reloads",
            *(
                group + "_" + event
                for group in ("operator", "helper", "automation", "source")
                for event in ("state_changed", "state_reported")
            ),
        ),
        0,
    )
    metrics.update(counts)
    result = {
        "workload": workload,
        "measurement": measure,
        "trace_enabled": trace,
        "batches": count,
        "warmup_batches": warmup,
        "entity_count": sum(
            1
            for state in hass.states.async_all()
            if registry.async_get(state.entity_id)
            and registry.async_get(state.entity_id).platform == DOMAIN
        ),
        "automation_count": len(automations),
        "helper_count": 0 if native else len(HELPERS),
        "counts": metrics,
        "input_events": counts["source_state_changed"] + counts["source_state_reported"],
        "wall_seconds": elapsed_wall,
        "cpu_seconds": elapsed_cpu,
        "median_batch_ms": statistics.median(latencies) * 1000 if latencies else None,
        "p95_batch_ms": sorted(latencies)[math.ceil(len(latencies) * 0.95) - 1] * 1000
        if latencies
        else None,
        "max_batch_ms": max(latencies) * 1000 if latencies else None,
        "memory": memory,
        "task_count_before": tasks_before,
        "task_count_after": tasks_after,
        "worker_count": len(runtime._tasks),
        "trace_start": trace_start,
        "trace_after_workload": trace_end,
        "trace_after_drain": runtime.trace_health(),
        "trace_drain_wall_seconds": drain_wall,
        "trace_drain_cpu_seconds": drain_cpu,
        "virtual_start": virtual_start,
        "virtual_end": dt_util.utcnow().isoformat(),
        "wall_clock": "freezegun.api.real_perf_counter",
        "ha_log_level": "WARNING",
        "python": platform.python_version(),
        "ha_version": version("homeassistant"),
        "fixture_version": version("pytest-homeassistant-custom-component"),
        "asyncio_debug": False,
        "scope": "offline native HA fixture; real five public recipe automations vs "
        "two native policy inputs and three return monitors; three live covers with "
        "independent synthetic raw reports; fixed virtual UTC and 65s source timer; "
        "60s timer qualification, 1800s request and cold qualification, 300s warning; "
        "no Recorder, KNX, full-house load or physical oracle; no production-duration soak; "
        "snapshot counts cover Operator only, not debounced helper storage; "
        "workload timing excludes final trace drain; memory measured after drain",
    }

    def save():
        output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        if profiler:
            profiler.dump_stats(str(output.with_suffix(".cprof")))

    await hass.async_add_executor_job(save)
    remove_changed()
    remove_reported()
    remove_service()
    remove_automation()
    if automations:
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": list(automations)}, blocking=True
        )
    await timer("cancel")
    await hass.config_entries.async_unload(entry.entry_id)
    loop.set_debug(previous_debug)
    ha_logger.setLevel(previous_log_level)
    assert elapsed_wall > 0 and elapsed_cpu > 0
    assert len(runtime._tasks) == 3
