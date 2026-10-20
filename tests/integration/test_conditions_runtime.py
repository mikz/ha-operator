"""Native command permission and health stay separate from selected intent and I/O."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import asyncio
import json

import pytest
from homeassistant.components.trace.const import DATA_TRACE
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.ha_operator.core import Target
from custom_components.ha_operator.diagnostics import async_get_config_entry_diagnostics
from custom_components.ha_operator.fan import OperatorFan

from .helpers import async_add_physical_cover, async_setup_operator, managed_id
from .test_runtime_completion_pacing import relay_bundle  # noqa: F401
from .test_runtime_reconciliation import advance, reported, runtime_factory  # noqa: F401


def gate(entity="binary_sensor.ready"):
    return [{"condition": "state", "entity_id": entity, "state": "on"}]


async def switch_runtime(hass, runtime_factory, *, policies=None, **resource):
    hass.states.async_set("switch.raw", "off")
    commands = []

    async def command(call):
        commands.append(call.service)

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={
            "plug": {
                "name": "Stream",
                "kind": "switch",
                "entity_id": "switch.raw",
                "command_interval": 5,
                "retry_interval": 30,
                **resource,
            }
        },
        policies=policies,
    )
    return runtime, commands


async def test_manual_block_preserves_intent_and_off_ignores_on_gate(hass, runtime_factory):
    hass.states.async_set("binary_sensor.ready", "off")
    runtime, commands = await switch_runtime(
        hass, runtime_factory, command_conditions={"turn_on": gate()}
    )
    await runtime.async_set_mode("plug", "live")
    receipt = await runtime.async_request("plug", target={"on": True}, indefinite=True)
    await hass.async_block_till_done()
    assert runtime.decisions["plug"].target == Target(on=True)
    assert runtime.decisions["plug"].reason == "command_conditions_blocked"
    assert runtime.manual("plug").request_id == receipt["request_id"]
    assert not commands and not runtime.attempts and not runtime.last_commands
    assert "plug" not in runtime._timers
    hass.states.async_set("switch.raw", "on")
    await runtime.async_request("plug", target={"on": False}, indefinite=True)
    await hass.async_block_till_done()
    assert commands == ["turn_off"]
    assert runtime.attempts["plug"] == 1
    assert runtime.observations["plug"].target == Target(on=True)


async def test_policy_unblock_retries_at_original_physical_pacing(hass, runtime_factory, freezer):
    hass.states.async_set("binary_sensor.ready", "on")
    runtime, commands = await switch_runtime(
        hass,
        runtime_factory,
        command_conditions={"turn_on": gate()},
        policies={
            "follow": {
                "name": "Follow",
                "resource_id": "plug",
                "kind": "state",
                "target": {"on": True},
            }
        },
    )
    await runtime.async_set_mode("plug", "live")
    await hass.async_block_till_done()
    assert commands == ["turn_on"]
    due = runtime.next_attempts["plug"]
    hass.states.async_set("binary_sensor.ready", "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 10)
    hass.states.async_set("binary_sensor.ready", "on")
    await hass.async_block_till_done()
    assert commands == ["turn_on"]
    assert runtime.next_attempts["plug"] == due
    await advance(hass, freezer, 20)
    assert commands == ["turn_on", "turn_on"]


async def test_blocked_lease_expires_and_cannot_dispatch_on_later_unblock(
    hass, runtime_factory, freezer
):
    hass.states.async_set("binary_sensor.ready", "off")
    runtime, commands = await switch_runtime(
        hass, runtime_factory, command_conditions={"turn_on": gate()}
    )
    await runtime.async_set_mode("plug", "live")
    await runtime.async_request("plug", target={"on": True}, duration=5)
    await advance(hass, freezer, 6)
    hass.states.async_set("binary_sensor.ready", "on")
    await hass.async_block_till_done()
    assert runtime.manual("plug") is None
    assert not commands and not runtime.last_commands


async def test_gate_loss_while_lock_waits_does_not_count_attempt(hass, runtime_factory):
    hass.states.async_set("binary_sensor.ready", "on")
    runtime, commands = await switch_runtime(
        hass, runtime_factory, command_conditions={"turn_on": gate()}
    )
    await runtime.async_set_mode("plug", "live")
    lock = runtime._actuator_locks["plug"]
    await lock.acquire()
    await runtime.async_request("plug", target={"on": True}, indefinite=True)
    await asyncio.sleep(0)
    hass.states.async_set("binary_sensor.ready", "off")
    await asyncio.sleep(0)
    lock.release()
    await hass.async_block_till_done()
    assert not commands and not runtime.attempts and not runtime._last_send


async def test_predispatch_capability_failure_is_paced_without_fake_send(
    hass, runtime_factory, freezer
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    lock = runtime._actuator_locks["roof"]
    await lock.acquire()
    await runtime.async_request("roof", target={"position": 40}, indefinite=True)
    await asyncio.sleep(0)
    reported(hass, supported_features=0)
    adapter = runtime.adapter("roof")
    normalize = adapter.normalize
    calls = []

    def bounded_normalize(target):
        now = dt_util.utcnow().timestamp()
        assert not calls or now > calls[-1], "Pre-service failure retried without waiting"
        calls.append(now)
        return normalize(target)

    adapter.normalize = bounded_normalize
    lock.release()
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert (
        not commands
        and not runtime.attempts
        and not runtime._last_send
        and not runtime.last_commands
    )
    assert "roof" in runtime._timers
    await advance(hass, freezer, 30)
    assert len(calls) == 2


@pytest.mark.parametrize("features", [5, 53])
async def test_fan_on_gate_covers_direction_and_percentage_fallback(
    hass, runtime_factory, features
):
    hass.states.async_set("binary_sensor.ready", "off")
    hass.states.async_set(
        "fan.raw", "off", {"supported_features": features, "percentage": 0, "direction": "forward"}
    )
    commands = []

    async def command(call):
        commands.append(call.service)

    for service in ("set_direction", "set_percentage", "turn_on", "turn_off"):
        hass.services.async_register("fan", service, command)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={
            "fan": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 50, "direction": "forward"},
                "command_conditions": {"turn_on": gate()},
            }
        }
    )
    await runtime.async_set_mode("fan", "live")
    await runtime.async_request("fan", target={"on": True}, indefinite=True)
    await hass.async_block_till_done()
    assert not commands and not runtime.attempts
    hass.states.async_set("binary_sensor.ready", "on")
    await hass.async_block_till_done()
    assert commands == ["set_direction", "set_percentage" if features == 5 else "turn_on"]


async def test_managed_percentage_request_obeys_guard_on_percentage_bearing_turn_on(
    hass, runtime_factory
):
    hass.states.async_set("binary_sensor.ready", "off")
    hass.states.async_set(
        "fan.raw", "on", {"supported_features": 53, "percentage": 20, "direction": "forward"}
    )
    commands = []

    async def command(call):
        commands.append((call.service, dict(call.data)))

    hass.services.async_register("fan", "turn_on", command)
    hass.services.async_register("fan", "set_direction", command)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={
            "fan": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 50},
                "command_conditions": {"set_percentage": gate()},
            }
        }
    )
    await runtime.async_set_mode("fan", "live")
    entity = OperatorFan(runtime, "fan")
    await entity.async_set_percentage(80)
    await hass.async_block_till_done()
    assert not commands and not runtime.attempts and not runtime.last_commands
    assert runtime.decisions["fan"].reason == "command_conditions_blocked"
    assert runtime.decisions["fan"].target.percentage == 80
    hass.states.async_set("binary_sensor.ready", "on")
    await hass.async_block_till_done()
    assert commands == [
        ("set_direction", {"entity_id": "fan.raw", "direction": "forward"}),
        ("turn_on", {"entity_id": "fan.raw", "percentage": 80}),
    ]


async def test_percentage_guard_rechecked_after_direction_service_await(hass, runtime_factory):
    hass.states.async_set("binary_sensor.ready", "on")
    hass.states.async_set(
        "fan.raw", "on", {"supported_features": 53, "percentage": 20, "direction": "forward"}
    )
    commands = []

    async def command(call):
        commands.append(call.service)
        if call.service == "set_direction":
            hass.states.async_set("binary_sensor.ready", "off")
            await asyncio.sleep(0)

    for service in ("turn_on", "set_direction"):
        hass.services.async_register("fan", service, command)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={
            "fan": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 50},
                "command_conditions": {"set_percentage": gate()},
            }
        }
    )
    await runtime.async_set_mode("fan", "live")
    await runtime.async_request(
        "fan", target={"percentage": 80, "direction": "reverse"}, indefinite=True
    )
    await hass.async_block_till_done()
    assert commands == ["set_direction"]
    assert runtime.attempts["fan"] == 1 and not runtime.last_commands


@pytest.mark.parametrize("features", [1, 49])
async def test_native_off_ignores_percentage_guard_but_percentage_fallback_off_obeys_it(
    hass, runtime_factory, features
):
    hass.states.async_set("binary_sensor.ready", "off")
    hass.states.async_set("fan.raw", "on", {"supported_features": features, "percentage": 20})
    commands = []

    async def command(call):
        commands.append(call.service)

    for service in ("turn_off", "set_percentage"):
        hass.services.async_register("fan", service, command)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={
            "fan": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 50},
                "command_conditions": {"set_percentage": gate()},
            }
        }
    )
    await runtime.async_set_mode("fan", "live")
    await OperatorFan(runtime, "fan").async_set_percentage(0)
    await hass.async_block_till_done()
    assert commands == ([] if features == 1 else ["turn_off"])
    if features == 1:
        assert not runtime.attempts


async def test_direction_while_off_does_not_use_blocked_on_guard(hass, runtime_factory):
    hass.states.async_set("binary_sensor.ready", "off")
    hass.states.async_set(
        "fan.raw", "off", {"supported_features": 53, "percentage": 0, "direction": "forward"}
    )
    commands = []

    async def command(call):
        commands.append(call.service)

    for service in ("set_direction", "turn_off"):
        hass.services.async_register("fan", service, command)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={
            "fan": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 50},
                "command_conditions": {"turn_on": gate()},
            }
        }
    )
    await runtime.async_set_mode("fan", "live")
    await runtime.async_request("fan", target={"direction": "reverse"}, indefinite=True)
    await hass.async_block_till_done()
    assert commands == ["set_direction", "turn_off"]


async def test_cover_stop_bypasses_configured_movement_guard(hass, runtime_factory):
    reported(hass)
    hass.states.async_set("binary_sensor.ready", "off")
    factory, commands = runtime_factory
    runtime = await factory(
        resources={
            "roof": {
                "name": "Roof",
                "kind": "cover",
                "entity_id": "cover.raw",
                "command_conditions": {"set_cover_position": gate()},
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 40}, indefinite=True)
    await hass.async_block_till_done()
    assert not commands
    await runtime.async_stop("roof")
    await hass.async_block_till_done()
    assert [call[0] for call in commands] == ["stop_cover"]
    assert not runtime.attempts and not runtime.last_commands


async def test_relay_safety_off_bypasses_off_gate_and_on_gate_rechecks_after_deadtime(
    hass, runtime_factory, relay_bundle
):
    hass.states.async_set("binary_sensor.ready", "on")
    relay_bundle["command_conditions"] = {
        "turn_on": gate(),
        "turn_off": [{"condition": "template", "value_template": "{{ false }}"}],
    }
    factory, _ = runtime_factory
    runtime = await factory(resources={"relay": relay_bundle})
    commands = []
    all_off = asyncio.Event()

    async def command(call):
        commands.append((call.service, call.data["entity_id"]))
        hass.states.async_set(call.data["entity_id"], "off")
        if len(commands) == len(relay_bundle["outputs"]):
            all_off.set()

    for service in ("turn_on", "turn_off"):
        hass.services.async_register("switch", service, command)
    await runtime.async_set_mode("relay", "live")
    await runtime.async_request("relay", target={"profile": "reverse"}, indefinite=True)
    await asyncio.wait_for(all_off.wait(), 2)
    hass.states.async_set("binary_sensor.ready", "off")
    await asyncio.sleep(0.2)
    await hass.async_block_till_done()
    assert commands == [("turn_off", output) for output in relay_bundle["outputs"]]
    assert "relay" not in runtime.last_commands
    assert runtime.decisions["relay"].target.profile == "reverse"


async def test_health_frozen_on_semantic_changes_explain_fresh_and_diagnostics_private(
    hass, tmp_path
):
    await async_add_physical_cover(hass)
    hass.states.async_set(
        "sensor.metric",
        "12",
        {"friendly_name": "Private frame metric", "unit_of_measurement": "fps"},
    )
    checks = [
        {
            "alias": "Private frames",
            "conditions": [
                {"condition": "numeric_state", "entity_id": "sensor.metric", "above": 10}
            ],
        }
    ]
    entry = await async_setup_operator(
        hass,
        tmp_path,
        resources={
            "roof": {
                "name": "Roof",
                "kind": "cover",
                "entity_id": "cover.physical_roof",
                "health_checks": checks,
            }
        },
    )
    runtime = entry.runtime_data
    health_id = managed_id(hass, "sensor", key="health")
    reason_id = managed_id(hass, "sensor", key="reason")
    initial = hass.states.get(health_id)
    assert initial.state == "healthy"
    traces = hass.data[DATA_TRACE]["script.roof_reason"].runs
    before_traces = len(traces)
    events = []
    remove = hass.bus.async_listen("state_changed", events.append)
    hass.states.async_set(
        "sensor.metric",
        "13",
        {"friendly_name": "Private frame metric", "unit_of_measurement": "fps"},
    )
    await hass.async_block_till_done()
    assert hass.states.get(health_id) is initial
    assert not [event for event in events if event.data["entity_id"] in {health_id, reason_id}]
    assert len(traces) == before_traces
    fresh = runtime.explain("roof")["resources"]["roof"]["conditions"]
    assert (
        fresh["health"]["checks"][0]["conditions"]["conditions"][0]["sources"]["sensor.metric"][
            "state"
        ]
        == "13"
    )
    hass.states.async_set("sensor.metric", "1")
    await hass.async_block_till_done()
    assert hass.states.get(health_id).state == "unhealthy"
    assert len([event for event in events if event.data["entity_id"] == health_id]) == 1
    assert len(traces) == before_traces + 1
    assert not runtime.attempts and not runtime.last_commands
    diagnostic = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for private in ("Private", "sensor.metric", "value_template", "above"):
        assert private not in diagnostic
    assert "unhealthy" in diagnostic
    remove()


async def test_unhealthy_live_resource_keeps_selected_on_without_restart(hass, runtime_factory):
    hass.states.async_set("sensor.metric", "20")
    runtime, commands = await switch_runtime(
        hass,
        runtime_factory,
        health_checks=[
            {
                "alias": "Rate",
                "conditions": [
                    {"condition": "numeric_state", "entity_id": "sensor.metric", "above": 10}
                ],
            }
        ],
        policies={
            "follow": {
                "name": "Follow",
                "resource_id": "plug",
                "kind": "state",
                "target": {"on": True},
            }
        },
    )
    hass.states.async_set("switch.raw", "on")
    await runtime.async_set_mode("plug", "live")
    await hass.async_block_till_done()
    assert runtime.decisions["plug"].status == "satisfied"
    hass.states.async_set("sensor.metric", "1")
    await hass.async_block_till_done()
    assert runtime.health_details("plug")["status"] == "unhealthy"
    assert runtime.decisions["plug"].status == "satisfied"
    assert runtime.decisions["plug"].target == Target(on=True)
    assert not commands and not runtime.attempts and not runtime.last_commands


async def test_health_registry_removal_readdition_and_user_disable_preserved(hass, tmp_path):
    await async_add_physical_cover(hass)
    data = {"name": "Roof", "kind": "cover", "entity_id": "cover.physical_roof"}
    checks = [
        {
            "alias": "Check",
            "conditions": [{"condition": "template", "value_template": "{{ true }}"}],
        }
    ]
    entry = await async_setup_operator(
        hass, tmp_path, resources={"roof": {**data, "health_checks": checks}}
    )
    registry = er.async_get(hass)
    entity_id = managed_id(hass, "sensor", key="health")
    old_runtime = entry.runtime_data
    old_checkers = [
        checker for checks in old_runtime._native_lists().values() for checker in checks
    ]
    original = entry.subentries["roof"]
    hass.config_entries.async_update_subentry(entry, original, data=data)
    await hass.async_block_till_done()
    assert old_runtime._closed and all(
        checker._closed and not checker._unsubscribers for checker in old_checkers
    )
    assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    hass.config_entries.async_update_subentry(
        entry, entry.subentries["roof"], data={**data, "health_checks": checks}
    )
    await hass.async_block_till_done()
    assert managed_id(hass, "sensor", key="health") == entity_id
    assert registry.async_get(entity_id).disabled_by is None
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    hass.config_entries.async_update_subentry(entry, entry.subentries["roof"], data=data)
    await hass.async_block_till_done()
    hass.config_entries.async_update_subentry(
        entry, entry.subentries["roof"], data={**data, "health_checks": checks}
    )
    await hass.async_block_till_done()
    assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.USER


@pytest.mark.parametrize("qualified_health", [False, True])
async def test_diagnostic_condition_and_qualification_errors_are_allowlisted(
    hass, runtime_factory, qualified_health
):
    data = {
        "command_conditions": {
            "turn_on": [{"condition": "template", "value_template": "{{ 1 / 0 }}"}]
        }
    }
    if qualified_health:
        data["health_checks"] = [
            {
                "alias": "Private health",
                "conditions": [],
                "qualification": [{"condition": "template", "value_template": "{{ 1 / 0 }}"}],
            }
        ]
    runtime, _ = await switch_runtime(hass, runtime_factory, **data)
    diagnostics = await async_get_config_entry_diagnostics(hass, runtime.entry)
    resource = next(iter(diagnostics["resources"].values()))
    assert resource["command_conditions"]["turn_on"]["evaluation_error"]
    if qualified_health:
        assert resource["health"]["checks"][0]["evaluation_error"]
    assert "Private" not in json.dumps(diagnostics)
    assert "value_template" not in json.dumps(diagnostics)
