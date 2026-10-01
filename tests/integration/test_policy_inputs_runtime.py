"""Native source events, strict admission, and independently journaled commands."""

# ruff: noqa: F811 - imported fixture intentionally injected by name
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

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
    revision = deepcopy(runtime._state)
    for value in (15, 14, 14):
        temperature(hass, value)
        await hass.async_block_till_done()
    assert deepcopy(runtime._state) == revision
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


async def test_observe_episode_cannot_activate_after_live(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory, live=False)
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"
    await runtime.async_set_mode("roof", "live")
    await advance(hass, freezer, 61)
    assert commands == []


@pytest.mark.parametrize("unknown_first", [False, True])
async def test_numeric_recovery_requires_fresh_finite_report(
    hass, runtime_factory, freezer, unknown_first
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")
    temperature(hass, 15)
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    saved = deepcopy(runtime._state)
    await runtime.async_close()
    recovered = await factory(policies=numeric_policy())
    # Factory assigns another entry ID; seed same validated snapshot before restart.
    await recovered.async_close()
    path = Path(recovered._store.path)
    await hass.async_add_executor_job(path.write_text, json.dumps({"version": 1, "data": saved}))
    from custom_components.ha_operator.runtime import OperatorRuntime

    fresh = OperatorRuntime(hass, recovered.entry)
    await fresh.async_start()
    try:
        assert input_state(fresh, "cold")["recovery_pending"]
        before = len(commands)
        if unknown_first:
            temperature(hass, "unknown")
            await hass.async_block_till_done()
            assert len(commands) == before
        # Without unknown first, this is STATE_REPORTED against the startup snapshot.
        temperature(hass, 15)
        await hass.async_block_till_done()
        assert input_state(fresh, "cold")["qualified"]
        assert not input_state(fresh, "cold")["recovery_pending"]
    finally:
        await fresh.async_close()


async def test_unchanged_numeric_report_at_deadline_qualifies(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    policies = numeric_policy()
    warm = numeric_policy()["cold"]
    warm["input"]["threshold"] = 14
    # The first mapped policy remains unchanged; every mapped policy must be checked.
    runtime = await factory(policies={"warm": warm, **policies})
    await runtime.async_set_mode("roof", "live")
    temperature(hass, 15)
    await hass.async_block_till_done()
    due = input_state(runtime, "cold")["due_at"]
    # Do not fire a time event: the unchanged report must itself recognize the boundary.
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=60))
    temperature(hass, 15)
    await hass.async_block_till_done()
    assert input_state(runtime, "cold")["qualified"]
    assert input_state(runtime, "cold")["due_at"] == due
    assert commands[-1][1]["position"] == 7


async def test_unchanged_numeric_report_still_processes_manual_expiry(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")
    temperature(hass, 17)
    await hass.async_block_till_done()
    await runtime.async_request("roof", target={"position": 100}, duration=8)
    await hass.async_block_till_done()
    ingress = dict(runtime._input_ingress)
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=8))
    temperature(hass, 17)
    await hass.async_block_till_done()
    assert runtime.manual("roof") is None
    assert runtime.decisions["roof"].status == "idle"
    assert runtime._input_ingress == ingress
    assert len(commands) == 1


async def test_unchanged_numeric_report_preserves_shared_target_requirement_and_applying_role(
    hass, runtime_factory
):
    factory, commands = runtime_factory
    reported(hass)
    hass.states.async_set("switch.extractor", "on")
    policies = numeric_policy()
    policies["target"] = {
        "name": "Target",
        "kind": "state",
        "resource_id": "roof",
        "target_entity": "sensor.temperature",
    }
    runtime = await factory(
        policies=policies,
        requirements={
            "air": {
                "name": "Fresh air",
                "activation_entities": ["switch.extractor"],
                "providers": [
                    {
                        "id": "passive",
                        "evidence": [
                            {
                                "entity_id": "sensor.temperature",
                                "kind": "airflow",
                                "operator": "gte",
                                "value": 18,
                            }
                        ],
                    }
                ],
            }
        },
    )
    temperature(hass, 17)
    await hass.async_block_till_done()
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert commands[-1][1]["position"] == 17
    assert runtime.requirement_results["air"].status == "unmet"
    ingress = dict(runtime._input_ingress)
    with patch.object(runtime, "_recompute", wraps=runtime._recompute) as recompute:
        runtime._applying.add("roof")
        try:
            temperature(hass, 17)
            await hass.async_block_till_done()
            assert recompute.call_count > 0
            assert runtime._input_ingress == ingress
        finally:
            runtime._applying.discard("roof")
    temperature(hass, 18)
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].target.position == 18
    assert runtime.requirement_results["air"].status == "satisfied"


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
        assert len(deepcopy(runtime._state)["occurrences"]) == 1
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
            assert not any(
                item["skipped"] for item in deepcopy(fresh._state)["occurrences"].values()
            )
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
    assert not any(item["skipped"] for item in deepcopy(runtime._state)["occurrences"].values())


@pytest.mark.parametrize("invalid", ["not numeric", "nan", "inf"])
async def test_numeric_unusable_report_requires_fresh_recovery(hass, runtime_factory, invalid):
    factory, commands = runtime_factory
    reported(hass)
    temperature(hass, invalid)
    runtime = await factory(policies=numeric_policy())
    await runtime.async_set_mode("roof", "live")
    assert input_state(runtime, "cold")["source_quality"] == "unknown"
    assert commands == []
    temperature(hass, 15)
    await hass.async_block_till_done()
    assert input_state(runtime, "cold")["due_at"] is not None
    assert commands == []


async def test_timer_unavailable_dynamic_target_is_suppressed(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    assert await async_setup_component(
        hass, "timer", {"timer": {"ventilation": {"duration": "00:10:00"}}}
    )
    reported(hass)
    policies = timer_policy()
    policies["vent"].pop("target")
    policies["vent"]["target_entity"] = "sensor.opening"
    hass.states.async_set("sensor.opening", "unknown")
    runtime = await factory(policies=policies)
    await hass.async_start()
    await runtime.async_set_mode("roof", "live")
    await timer_service(hass, "start")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    assert input_state(runtime)["phase"] == "suppressed"
    assert commands == []
    hass.states.async_set("sensor.opening", "80")
    await hass.async_block_till_done()
    assert commands == []


async def test_pending_cancel_is_checked_inside_inflight_adapter(
    hass, runtime_factory, freezer, monkeypatch
):
    """A captured cancellation fences dispatch before queued input processing."""
    import asyncio

    factory, commands = runtime_factory
    runtime = await setup_timer(hass, factory)
    applying, apply_finished, release_apply = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered, release_inputs = asyncio.Event(), asyncio.Event()
    adapter = runtime.adapter("roof")
    real_apply = adapter.async_apply
    real_flush = runtime._async_flush_inputs

    async def paused_apply(target, current):
        applying.set()
        await release_apply.wait()
        try:
            return await real_apply(target, current)
        finally:
            apply_finished.set()

    async def paused_inputs():
        entered.set()
        await release_inputs.wait()
        await real_flush()

    monkeypatch.setattr(adapter, "async_apply", paused_apply)
    try:
        await timer_service(hass, "start")
        await hass.async_block_till_done()
        await advance(hass, freezer, 60)
        await asyncio.wait_for(applying.wait(), 2)
        assert input_state(runtime)["phase"] == "accepted"
        monkeypatch.setattr(runtime, "_async_flush_inputs", paused_inputs)
        await timer_service(hass, "cancel")
        await asyncio.wait_for(entered.wait(), 2)
        release_apply.set()
        await asyncio.wait_for(apply_finished.wait(), 2)
        assert commands == []
    finally:
        release_apply.set()
        release_inputs.set()
    await hass.async_block_till_done()
    assert input_state(runtime)["phase"] == "suppressed"
    assert commands == []
