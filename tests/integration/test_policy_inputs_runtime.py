"""Native source events, strict admission, and independently journaled commands."""

# ruff: noqa: F811 - imported fixture intentionally injected by name

import asyncio
import json
import threading

import pytest
from homeassistant.setup import async_setup_component

from custom_components.ha_operator import storage
from tests.integration.test_runtime_reconciliation import (
    advance,  # noqa: F401
    reported,
    runtime_factory,  # noqa: F401
)


@pytest.fixture(autouse=True)
async def cleanup_native_timer(hass):
    yield
    if hass.services.has_service("timer", "cancel"):
        await timer_service(hass, "cancel")
        await hass.async_block_till_done()


def numeric_policy():
    return {
        "cold": {
            "name": "Cold",
            "kind": "state",
            "resource_id": "roof",
            "target": {"position": 7},
            "input": {
                "type": "qualified_numeric",
                "entity_id": "sensor.temperature",
                "comparison": "below",
                "threshold": 16,
                "unit": "°C",
                "qualification_seconds": 60,
            },
        }
    }


def timer_policy():
    return {
        "vent": {
            "name": "Vent",
            "kind": "occurrence",
            "resource_id": "roof",
            "target": {"position": 100},
            "input": {
                "type": "timer_episode",
                "entity_id": "timer.ventilation",
                "qualification_seconds": 60,
                "request_seconds": 1800,
            },
        }
    }


def temperature(hass, value, unit="°C"):
    hass.states.async_set("sensor.temperature", str(value), {"unit_of_measurement": unit})


def input_state(runtime, policy_id="vent"):
    return runtime._state["policy_inputs"][policy_id]["state"]


async def timer_service(hass, service, **kwargs):
    await hass.services.async_call(
        "timer", service, {"entity_id": "timer.ventilation", **kwargs}, blocking=True
    )


async def setup_timer(hass, factory, live=True):
    assert await async_setup_component(
        hass, "timer", {"timer": {"ventilation": {"duration": "00:10:00"}}}
    )
    reported(hass)
    runtime = await factory(policies=timer_policy())
    await hass.async_start()
    if live:
        await runtime.async_set_mode("roof", "live")
    return runtime


async def test_numeric_edges_unknown_unit_and_unchanged_reports(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")
    temperature(hass, 15)
    await hass.async_block_till_done()
    due = input_state(runtime, "cold")["due_at"]
    revision = runtime.store.state["revision"]
    for value in (15, 14, 14):
        temperature(hass, value)
        await hass.async_block_till_done()
    assert runtime.store.state["revision"] == revision
    assert input_state(runtime, "cold")["due_at"] == due
    await advance(hass, freezer, 59)
    temperature(hass, 17)
    temperature(hass, 15)
    await hass.async_block_till_done()
    assert input_state(runtime, "cold")["due_at"] == due + 59
    await advance(hass, freezer, 60)
    assert commands[-1][1]["position"] == 7
    temperature(hass, 10, "°F")
    await hass.async_block_till_done()
    assert input_state(runtime, "cold")["qualified"]
    assert input_state(runtime, "cold")["source_quality"] == "unknown"


async def test_native_timer_restart_pause_resume_cancel_finish_and_change(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    first = dict(input_state(runtime))
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    assert input_state(runtime)["episode_id"] != first["episode_id"]
    assert input_state(runtime)["finish_at"] == first["finish_at"]
    await timer_service(hass, "pause")
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"
    paused_episode = input_state(runtime)["episode_id"]
    await timer_service(hass, "start")  # Same-second active restart after resume.
    await hass.async_block_till_done()
    assert input_state(runtime)["episode_id"] != paused_episode
    await timer_service(hass, "pause")
    await hass.async_block_till_done()
    await advance(hass, freezer, 61)
    assert commands == []
    await timer_service(hass, "cancel")
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    await timer_service(hass, "change", duration="-00:00:10")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"
    await timer_service(hass, "cancel")
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    accepted = dict(input_state(runtime))
    assert commands[-1][1]["position"] == 100
    await timer_service(hass, "change", duration="-00:00:10")
    await timer_service(hass, "finish")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "accepted"
    assert input_state(runtime)["expires_at"] == accepted["expires_at"]
    await timer_service(hass, "start")
    await timer_service(hass, "cancel")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"


@pytest.mark.parametrize("service", ["cancel", "pause", "finish", "change"])
async def test_lifecycle_arriving_during_admission_save_fences_raw_dispatch(
    hass, runtime_factory, freezer, monkeypatch, service
):
    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    entered, release = threading.Event(), threading.Event()
    real_write = storage._write_snapshot

    def gate(path, state):
        if state["policy_inputs"]["vent"]["state"]["phase"] == "accepted":
            accepted = state["policy_inputs"]["vent"]["state"]
            occurrence = state["occurrences"][json.dumps(["vent", accepted["episode_id"]])]
            assert occurrence["expires_at"] == accepted["expires_at"]
            assert occurrence["target"] == {"position": 100.0}
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    advancing = asyncio.create_task(advance(hass, freezer, 60))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        flush = runtime._input_flush
        await timer_service(
            hass, service, **({"duration": "-00:00:10"} if service == "change" else {})
        )
        assert runtime._input_flush is flush
        assert commands == []
    finally:
        release.set()
    await advancing
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"
    assert commands == []
    assert all(item["skipped"] for item in runtime.store.state["occurrences"].values())


async def test_observe_episode_cannot_activate_after_live(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory, live=False)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"
    await runtime.async_set_mode("roof", "live")
    await advance(hass, freezer, 61)
    assert commands == []


async def test_failed_numeric_save_inhibits_commands(hass, runtime_factory, monkeypatch):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(storage, "_write_snapshot", fail)
    temperature(hass, 15)
    await hass.async_block_till_done()
    assert runtime.fault == "storage_error"
    assert commands == []


async def test_numeric_recovery_requires_fresh_finite_report(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")
    temperature(hass, 15)
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    saved = runtime.store.state
    await runtime.async_close()
    recovered = await factory(policies=numeric_policy())
    # Factory assigns another entry ID; seed same validated snapshot before restart.
    await recovered.async_close()
    path = recovered.store._path
    path.write_text(json.dumps({"version": 3, "data": saved}))
    from custom_components.ha_operator.runtime import OperatorRuntime

    fresh = OperatorRuntime(hass, recovered.entry)
    await fresh.async_start()
    try:
        assert input_state(fresh, "cold")["recovery_pending"]
        before = len(commands)
        temperature(hass, "unknown")
        await hass.async_block_till_done()
        assert len(commands) == before
        temperature(hass, 15)
        await hass.async_block_till_done()
        assert input_state(fresh, "cold")["qualified"]
        assert not input_state(fresh, "cold")["recovery_pending"]
    finally:
        await fresh.async_close()


@pytest.mark.parametrize("command", ["observe", "disable"])
async def test_qualification_dead_ends_through_reenable(hass, runtime_factory, freezer, command):
    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    if command == "observe":
        await runtime.async_set_mode("roof", "observe")
        await runtime.async_set_mode("roof", "live")
    else:
        await runtime.async_set_policy_enabled("vent", False)
        await runtime.async_set_policy_enabled("vent", True)
    await advance(hass, freezer, 61)
    assert input_state(runtime)["phase"] == "suppressed"
    assert commands == []


async def test_cancelled_input_write_publishes_and_unfences_live_runtime(
    hass, runtime_factory, freezer, monkeypatch
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")
    entered, release = threading.Event(), threading.Event()
    real_write = storage._write_snapshot

    def gate(path, state):
        entered.set()
        assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    temperature(hass, 15)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        flush = runtime._input_flush
        flush.cancel()
        temperature(hass, 17)
        temperature(hass, 15)
        assert runtime._input_flush is flush
        assert commands == []
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await flush
    await hass.async_block_till_done()
    assert runtime._input_ingress == runtime._input_processed
    assert not runtime.fault
    await advance(hass, freezer, 60)
    assert commands[-1][1]["position"] == 7


async def test_nested_native_timer_events_use_ordered_state_facts(hass, runtime_factory):
    from homeassistant.core import callback
    from homeassistant.helpers.entity_component import DATA_INSTANCES

    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    timer = hass.data[DATA_INSTANCES]["timer"].get_entity("timer.ventilation")

    @callback
    def nested(_):
        timer.async_start()
        timer.async_pause()
        timer.async_start()
        timer.async_cancel()
        timer.async_start()

    remove = hass.bus.async_listen("test_nested", nested)
    try:
        hass.bus.async_fire("test_nested")
        await hass.async_block_till_done()
        assert input_state(runtime)["phase"] == "qualifying"
        assert len(runtime.store.state["occurrences"]) == 1
        assert commands == []
    finally:
        remove()


async def test_startup_timer_start_is_not_a_new_episode(hass, runtime_factory, freezer):
    from homeassistant.core import CoreState

    hass.set_state(CoreState.starting)
    factory, commands = runtime_factory
    assert await async_setup_component(
        hass, "timer", {"timer": {"ventilation": {"duration": "00:10:00"}}}
    )
    reported(hass)
    runtime = await factory(policies=timer_policy())
    await runtime.async_set_mode("roof", "live")
    await timer_service(hass, "start")  # Native restored timer's start before HA started.
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "idle"
    hass.set_state(CoreState.not_running)
    await hass.async_start()
    await advance(hass, freezer, 61)
    assert commands == []
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "qualifying"


@pytest.mark.parametrize("during_startup", [True, False])
async def test_real_native_timer_restore_never_opens_episode(hass, runtime_factory, during_startup):
    from datetime import timedelta

    from homeassistant.core import CoreState, State, callback
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    if during_startup:
        hass.set_state(CoreState.starting)
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=timer_policy())
    await runtime.async_set_mode("roof", "live")
    mock_restore_cache(
        hass,
        [
            State(
                "timer.ventilation",
                "active",
                {
                    "duration": "00:10:00",
                    "finishes_at": (dt_util.utcnow() + timedelta(minutes=10)).isoformat(),
                },
            )
        ],
    )
    events = []

    @callback
    def record(event):
        events.append(event.event_type)

    remove = hass.bus.async_listen("timer.restarted", record)
    try:
        assert await async_setup_component(
            hass, "timer", {"timer": {"ventilation": {"duration": "00:10:00", "restore": True}}}
        )
        await hass.async_block_till_done()
        assert events == ["timer.restarted"]
        assert hass.states.get("timer.ventilation").state == "active"
        assert input_state(runtime)["phase"] == "idle"
        assert commands == []
    finally:
        remove()


@pytest.mark.parametrize("accepted", [True, False])
async def test_timer_restart_recovers_only_accepted_intent(
    hass, runtime_factory, freezer, accepted
):
    from custom_components.ha_operator.runtime import OperatorRuntime

    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    if accepted:
        await advance(hass, freezer, 60)
    saved = dict(input_state(runtime))
    await runtime.async_close()
    fresh = OperatorRuntime(hass, runtime.entry)
    await fresh.async_start()
    try:
        await hass.async_block_till_done()
        assert input_state(fresh)["phase"] == ("accepted" if accepted else "suppressed")
        assert input_state(fresh)["expires_at"] == saved["expires_at"]
        if not accepted:
            await advance(hass, freezer, 61)
            assert commands == []
        else:
            assert not any(item["skipped"] for item in fresh.store.state["occurrences"].values())
    finally:
        await fresh.async_close()


async def test_changed_numeric_config_does_not_reuse_qualification(hass, runtime_factory, freezer):
    from custom_components.ha_operator.runtime import OperatorRuntime

    factory, _ = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    temperature(hass, 15)
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    assert input_state(runtime, "cold")["qualified"]
    await runtime.async_close()
    subentry = runtime.entry.subentries["cold"]
    source = {**subentry.data["input"], "threshold": 14}
    hass.config_entries.async_update_subentry(
        runtime.entry, subentry, data={**subentry.data, "input": source}
    )
    fresh = OperatorRuntime(hass, runtime.entry)
    await fresh.async_start()
    try:
        assert not input_state(fresh, "cold")["qualified"]
        assert input_state(fresh, "cold")["due_at"] is None
    finally:
        await fresh.async_close()


async def test_delayed_start_processing_never_dispatches_expired_request(
    hass, runtime_factory, freezer, monkeypatch
):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    entered, release = threading.Event(), threading.Event()
    real_write = storage._write_snapshot

    def gate(path, state):
        if state["policy_inputs"]["vent"]["state"]["phase"] == "qualifying":
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    await timer_service(hass, "start")
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        freezer.move_to(dt_util.utcnow() + timedelta(seconds=1861))
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        assert commands == []
    finally:
        release.set()
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] in {"expired", "suppressed"}
    assert commands == []


async def test_cancelled_failed_input_write_inhibits_control(hass, runtime_factory, monkeypatch):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")
    entered, release = threading.Event(), threading.Event()

    def fail(path, state):
        entered.set()
        assert release.wait(5)
        raise OSError("uncertain write")

    monkeypatch.setattr(storage, "_write_snapshot", fail)
    temperature(hass, 15)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        flush = runtime._input_flush
        flush.cancel()
    finally:
        release.set()
    await flush
    await hass.async_block_till_done()
    assert runtime.fault == "storage_error"
    assert commands == []


async def test_nested_unchanged_numeric_report_uses_preceding_snapshot(hass, runtime_factory):
    from homeassistant.core import callback

    factory, _ = runtime_factory
    reported(hass)
    temperature(hass, 17)
    runtime = await factory(policies=numeric_policy())
    captured = []
    real_apply = runtime._apply_policy_input

    async def capture(policy_id, event, at, context, admissible):
        if event is not None:
            captured.append(event.value)
        await real_apply(policy_id, event, at, context, admissible)

    runtime._apply_policy_input = capture

    @callback
    def nested(_):
        temperature(hass, 17)  # state_reported precedes the later cold snapshot.
        temperature(hass, 15)
        temperature(hass, 17)
        temperature(hass, 17)

    remove = hass.bus.async_listen("test_numeric_nested", nested)
    try:
        hass.bus.async_fire("test_numeric_nested")
        await hass.async_block_till_done()
        assert captured == [17, 15, 17, 17]
        assert input_state(runtime, "cold")["due_at"] is None
    finally:
        remove()


@pytest.mark.parametrize("quality", ["unknown", "unavailable", "removed"])
async def test_lost_timer_source_dead_ends_pending_qualification(
    hass, runtime_factory, freezer, quality
):
    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    if quality == "removed":
        hass.states.async_remove("timer.ventilation")
    else:
        hass.states.async_set("timer.ventilation", quality)
    await hass.async_block_till_done()
    await advance(hass, freezer, 61)
    assert input_state(runtime)["phase"] == "suppressed"
    assert commands == []


async def test_direct_reconcile_processes_qualification_deadline(hass, runtime_factory, freezer):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=60))
    await runtime.async_reconcile("roof")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "accepted"
    assert commands[-1][1]["position"] == 100


async def test_queued_noop_input_commit_does_not_trace_durable_admission(
    hass, runtime_factory, freezer, monkeypatch
):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    entered, release = threading.Event(), threading.Event()
    real_write = storage._write_snapshot
    trace = []
    monkeypatch.setattr(runtime._trace, "event", lambda kind, data: trace.append((kind, data)))

    def gate(path, state):
        if state["modes"].get("roof") == "observe":
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    observing = asyncio.create_task(runtime.async_set_mode("roof", "observe"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        freezer.move_to(dt_util.utcnow() + timedelta(seconds=60))
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        await asyncio.sleep(0)
        assert runtime._input_flush is not None
        assert commands == []
    finally:
        release.set()
    await observing
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"
    assert commands == []
    assert not [
        data for kind, data in trace if kind == "admission" and data["action"] == "policy_input"
    ]


async def test_accepted_timer_source_loss_retains_original_expiry(hass, runtime_factory, freezer):
    factory, _ = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    expiry = input_state(runtime)["expires_at"]
    hass.states.async_remove("timer.ventilation")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "accepted"
    assert input_state(runtime)["expires_at"] == expiry
    assert not any(item["skipped"] for item in runtime.store.state["occurrences"].values())


async def test_cancelled_final_timer_admission_write_unfences_without_new_events(
    hass, runtime_factory, freezer, monkeypatch
):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    entered, release = threading.Event(), threading.Event()
    real_write = storage._write_snapshot

    def gate(path, state):
        if state["policy_inputs"]["vent"]["state"]["phase"] == "accepted":
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=60))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        flush = runtime._input_flush
        flush.cancel()
        assert commands == []
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await flush
    await hass.async_block_till_done()
    assert runtime._input_ingress == runtime._input_processed
    assert input_state(runtime)["phase"] == "accepted"
    assert commands[-1][1]["position"] == 100
