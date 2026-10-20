"""Opt-in candidate-only native-condition workload; no Recorder/devices/network."""

from __future__ import annotations

import cProfile
import gc
import json
import os
import time
import tracemalloc
from collections import Counter
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from weakref import WeakSet

import freezegun.api
import pytest
from homeassistant.core import EVENT_STATE_CHANGED, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.ha_operator.const import DOMAIN
from custom_components.ha_operator.native_conditions import NativeConditionList

_NATIVE_LOAD = Store._async_load
_NATIVE_WRITE = Store._async_write_data


@pytest.fixture(autouse=True)
def native_operator_storage(hass_storage, monkeypatch):
    for method, native in (("_async_load", _NATIVE_LOAD), ("_async_write_data", _NATIVE_WRITE)):
        mocked = getattr(Store, method)

        def route(native=native, mocked=mocked):
            async def dispatch(store, *args, **kwargs):
                return await (native if store.key.startswith("ha_operator.") else mocked)(
                    store, *args, **kwargs
                )

            return dispatch

        monkeypatch.setattr(Store, method, route())


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Load only the integration supplied to the isolated fixture process."""


async def test_profile(hass, tmp_path, freezer, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    count = int(os.environ.get("OPERATOR_PROFILE_COUNT", "20"))
    measure = os.environ.get("OPERATOR_PROFILE_MEASURE", "timing")
    workload = os.environ.get("OPERATOR_PROFILE_WORKLOAD", "conditions")
    assert workload == "conditions"
    counts = Counter()
    instances = WeakSet()
    native_evaluate = NativeConditionList.evaluate

    def evaluate(check):
        counts["condition_evaluations"] += 1
        instances.add(check)
        return native_evaluate(check)

    monkeypatch.setattr(NativeConditionList, "evaluate", evaluate)
    original_write = Store._async_write_data

    async def write(store, data):
        if store.key.startswith("ha_operator."):
            counts["operator_storage_writes"] += 1
        return await original_write(store, data)

    monkeypatch.setattr(Store, "_async_write_data", write)
    freezer.move_to("2026-10-01T08:00:00+00:00")
    gate, metric, raw = "binary_sensor.profile_gate", "sensor.profile_metric", "switch.profile_raw"
    hass.states.async_set(gate, "off")
    hass.states.async_set(metric, "20")
    hass.states.async_set(raw, "off")
    config = {
        "name": "Condition profile",
        "kind": "switch",
        "entity_id": raw,
        "command_conditions": {
            "turn_on": [{"condition": "state", "entity_id": gate, "state": "on"}]
        },
        "health_checks": [
            {
                "alias": f"Metric {index}",
                "conditions": [{"condition": "numeric_state", "entity_id": metric, "above": 10}],
            }
            for index in range(4)
        ],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Conditions",
        version=2,
        data={},
        options={},
        subentries_data=[
            {
                "subentry_id": "resource",
                "subentry_type": "resource",
                "title": config["name"],
                "data": config,
                "unique_id": None,
            }
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_start()
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    await runtime.async_set_mode("resource", "live")
    registry = er.async_get(hass)
    health = registry.async_get_entity_id("sensor", DOMAIN, "resource_health")
    assert health is not None and hass.states.get(health).state == "healthy"

    @callback
    def command(call):
        counts["actuator_commands"] += 1

    for service in ("turn_on", "turn_off"):
        hass.services.async_register("switch", service, command)

    @callback
    def changed(event):
        registered = registry.async_get(event.data["entity_id"])
        if registered and registered.platform == DOMAIN:
            counts["operator_state_changed"] += 1

    @callback
    def trace_started(event):
        registered = registry.async_get(event.data.get("entity_id"))
        if registered and registered.platform == DOMAIN:
            counts["operator_native_traces"] += 1

    remove = hass.bus.async_listen(EVENT_STATE_CHANGED, changed)
    remove_trace = hass.bus.async_listen("script_started", trace_started)

    async def source(entity, value):
        previous = hass.states.get(entity)
        evaluations = counts["condition_evaluations"]
        hass.states.async_set(entity, value)
        await hass.async_block_till_done()
        if entity == metric and previous.state != value:
            counts["shared_input_changes"] += 1
            counts["shared_input_evaluations"] += counts["condition_evaluations"] - evaluations

    async def batch():
        nonlocal runtime
        before = counts.copy()
        for _ in range(5):
            freezer.tick(timedelta(seconds=1))
            async_fire_time_changed_exact(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        assert counts["operator_state_changed"] == before["operator_state_changed"]
        assert counts["operator_native_traces"] == before["operator_native_traces"]
        for _ in range(5):
            await source(metric, "20")
            await source(gate, "off")
        assert counts["operator_state_changed"] == before["operator_state_changed"]
        assert counts["operator_native_traces"] == before["operator_native_traces"]
        snapshot = hass.states.get(health)
        await source(metric, "21")
        await source(metric, "20")
        assert hass.states.get(health).last_updated == snapshot.last_updated
        assert hass.states.get(health).attributes == snapshot.attributes
        assert counts["operator_state_changed"] == before["operator_state_changed"]
        assert counts["operator_native_traces"] == before["operator_native_traces"]
        await source(metric, "5")
        assert hass.states.get(health).state == "unhealthy"
        await source(metric, "unavailable")
        assert hass.states.get(health).state == "unknown"
        await source(metric, "20")
        assert hass.states.get(health).state == "healthy"
        assert counts["shared_input_evaluations"] - before["shared_input_evaluations"] == 5 * (
            counts["shared_input_changes"] - before["shared_input_changes"]
        )
        old_checks = set(instances)
        old = runtime
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        assert runtime is not old and old._closed
        assert all(check._closed and not check._unsubscribers for check in old_checks)
        counts["entry_reloads"] += 1
        assert counts["actuator_commands"] == 0

    await batch()
    counts.clear()
    gc.collect()
    profile = cProfile.Profile() if measure == "cprofile" else None
    if measure == "memory":
        tracemalloc.start()
        allocated_before = tracemalloc.get_traced_memory()[0]
    if profile:
        profile.enable()
    start_wall, start_cpu = freezegun.api.real_perf_counter(), time.process_time()
    for _ in range(count):
        await batch()
    elapsed_wall = freezegun.api.real_perf_counter() - start_wall
    elapsed_cpu = time.process_time() - start_cpu
    if profile:
        profile.disable()
    memory = None
    if measure == "memory":
        gc.collect()
        allocated, peak = tracemalloc.get_traced_memory()
        memory = {"retained_bytes": allocated - allocated_before, "peak_bytes": peak}
        tracemalloc.stop()
    measured = dict.fromkeys(
        (
            "condition_evaluations",
            "operator_state_changed",
            "actuator_commands",
            "operator_storage_writes",
            "entry_reloads",
            "shared_input_changes",
            "shared_input_evaluations",
            "operator_native_traces",
        ),
        0,
    )
    measured.update(counts)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert all(check._closed and not check._unsubscribers for check in instances)
    remove()
    remove_trace()
    output = os.environ.get("OPERATOR_PROFILE_OUTPUT")
    if output:
        result = {
            "workload": workload,
            "measurement": measure,
            "batches": count,
            "counts": measured,
            "wall_seconds": elapsed_wall,
            "cpu_seconds": elapsed_cpu,
            "memory": memory,
            "ha_version": version("homeassistant"),
            "fixture_version": version("pytest-homeassistant-custom-component"),
            "configuration": config,
            "scope": "Candidate-only native HA evaluator: four health checks sharing one metric, "
            "one command gate, five idle ticks, five unchanged "
            "report pairs, two same-result value changes, three health threshold/availability "
            "transitions and one reload "
            "per batch. No Recorder, devices, network, actuation intent or production soak. "
            "Final unload excluded. Weak references observe checker cleanup.",
        }

        def save():
            Path(output).with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
            if profile:
                profile.dump_stats(str(Path(output).with_suffix(".cprof")))

        await hass.async_add_executor_job(save)
