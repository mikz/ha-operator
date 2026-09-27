"""Runtime loops tested against independently reported raw device states."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.ha_operator.core import Target
from custom_components.ha_operator.runtime import OperatorRuntime

FEATURES = int(CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP)


def cover_data(name="Roof", entity_id="cover.raw", **kwargs):
    return {
        "name": name,
        "kind": "cover",
        "entity_id": entity_id,
        "retry_interval": 30,
        "command_interval": 5,
        "movement_timeout": 12,
        "manual_duration": 1800,
        **kwargs,
    }


def reported(hass, entity_id="cover.raw", position=0, state=None, **attributes):
    hass.states.async_set(
        entity_id,
        state or ("closed" if position == 0 else "open"),
        {"current_position": position, "supported_features": FEATURES, **attributes},
    )


async def advance(hass, freezer, seconds):
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=seconds))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()


@pytest.fixture
async def runtime_factory(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    commands = []
    instances = []

    async def command(call):
        commands.append((call.service, dict(call.data), dt_util.utcnow().timestamp()))

    hass.services.async_register("cover", "set_cover_position", command)
    hass.services.async_register("cover", "stop_cover", command)

    async def factory(resources=None, policies=None, requirements=None):
        subentries = []
        for kind, configs in (
            ("resource", resources if resources is not None else {"roof": cover_data()}),
            ("policy", policies or {}),
            ("requirement", requirements or {}),
        ):
            subentries.extend(
                {
                    "subentry_id": key,
                    "subentry_type": kind,
                    "title": config["name"],
                    "data": config,
                    "unique_id": None,
                }
                for key, config in configs.items()
            )
        entry = MockConfigEntry(
            domain="ha_operator",
            title="Operator",
            data={},
            subentries_data=subentries,
            state=ConfigEntryState.LOADED,
        )
        entry.add_to_hass(hass)
        runtime = OperatorRuntime(hass, entry)
        entry.runtime_data = runtime
        instances.append(runtime)
        await runtime.async_start()
        await hass.async_block_till_done()
        return runtime

    yield factory, commands
    for instance in instances:
        if not instance._closed:
            await instance.async_close()
        instance.entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_hidden_refusal_retries_without_changing_admitted_intent(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    receipt = await runtime.async_request("roof", target={"position": 100}, indefinite=True)
    await hass.async_block_till_done()
    assert [item[1]["position"] for item in commands] == [100]
    assert runtime.observations["roof"].target == Target(position=0)
    assert runtime.manual("roof").expires_at is None
    for _ in range(3):
        await advance(hass, freezer, 30)
    assert len(commands) == 4
    assert runtime.manual("roof").request_id == receipt["request_id"]
    assert "rain" not in runtime.decisions["roof"].reason
    reported(hass, position=100)
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].status == "satisfied"
    await advance(hass, freezer, 60)
    assert len(commands) == 4


async def test_unchanged_telemetry_does_not_renew_lease_or_starve_retry(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    receipt = await runtime.async_request("roof", target={"position": 100}, duration=100)
    await hass.async_block_till_done()
    initial_deadline = runtime.next_attempts["roof"]
    for _ in range(5):
        await advance(hass, freezer, 5)
        reported(hass)
        await hass.async_block_till_done()
        assert runtime.next_attempts["roof"] == initial_deadline
        assert runtime.manual("roof").expires_at == receipt["expires_at"]
    assert len(commands) == 1
    await advance(hass, freezer, 5)
    assert len(commands) == 2


async def test_expiry_wakes_without_telemetry_and_removes_old_retry(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100}, duration=8)
    await hass.async_block_till_done()
    await advance(hass, freezer, 8)
    assert runtime.manual("roof") is None
    assert runtime.decisions["roof"].status == "idle"
    await advance(hass, freezer, 100)
    assert len(commands) == 1


async def test_observe_rejects_manual_and_never_dispatches_policy(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(
        policies={
            "day": {
                "name": "Day",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 100},
            },
        }
    )
    assert runtime.decisions["roof"].status == "observe"
    with pytest.raises(ServiceValidationError, match="observe"):
        await runtime.async_request("roof", target={"position": 100})
    with pytest.raises(ServiceValidationError, match="observe"):
        await runtime.async_stop("roof")
    await advance(hass, freezer, 300)
    assert commands == []
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert len(commands) == 1
    await runtime.async_set_mode("roof", "observe")
    await advance(hass, freezer, 300)
    assert len(commands) == 1


async def test_superseded_generation_cannot_send_after_async_boundary(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = runtime.adapter("roof")
    original = adapter.async_apply

    async def delayed(target, still_current):
        if target.position == 100:
            entered.set()
            await release.wait()
        return await original(target, still_current)

    with patch.object(adapter, "async_apply", delayed):
        await runtime.async_request("roof", target={"position": 100})
        await entered.wait()
        await runtime.async_request("roof", target={"position": 40})
        release.set()
        await hass.async_block_till_done()
        await advance(hass, freezer, 5)
    assert [item[1]["position"] for item in commands] == [40]
    assert runtime.last_commands["roof"]["target"] == {"position": 40}


async def test_initial_motion_has_bounded_timeout(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass, position=10, state="opening")
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    assert commands == []
    await advance(hass, freezer, 6)
    reported(hass, position=20, state="opening")
    await hass.async_block_till_done()
    await advance(hass, freezer, 6)
    assert len(commands) == 1


async def test_inflight_motion_uses_movement_timeout_before_retry(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(resources={"roof": cover_data(retry_interval=5, movement_timeout=20)})
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    reported(hass, position=10, state="opening")
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    assert len(commands) == 1
    await advance(hass, freezer, 15)
    assert len(commands) == 2


@pytest.mark.parametrize(
    "attributes,state",
    [
        ({"restored": True}, "open"),
        ({"assumed_state": True}, "open"),
        ({}, "unavailable"),
    ],
)
async def test_nonphysical_observation_inhibits_dispatch_and_recovers(
    hass, runtime_factory, freezer, attributes, state
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    reported(hass, position=0, state=state, **attributes)
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].status == "unavailable"
    await advance(hass, freezer, 60)
    assert commands == []
    reported(hass)
    await hass.async_block_till_done()
    assert len(commands) == 1


async def test_capability_loss_does_not_kill_worker(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    reported(hass, supported_features=0)
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert len(commands) == 1
    reported(hass)
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    assert len(commands) == 2
    assert all(not task.done() for task in runtime._tasks)


async def test_delivery_failure_is_retried_from_current_intent(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    calls = 0

    async def flaky(call):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HomeAssistantError("device refused transport")
        commands.append((call.service, dict(call.data), dt_util.utcnow().timestamp()))

    hass.services.async_register("cover", "set_cover_position", flaky)
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    assert runtime.last_commands == {}
    assert runtime.decisions["roof"].reason == "delivery_failed"
    await advance(hass, freezer, 30)
    assert calls == 2 and len(commands) == 1


async def test_stop_without_capability_reports_failure_but_holds_retry(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass, supported_features=int(CoverEntityFeature.SET_POSITION))
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError, match="does not support physical STOP"):
        await runtime.async_stop("roof")
    assert runtime.manual("roof").mode == "hands_off"
    await advance(hass, freezer, 60)
    assert [item[0] for item in commands] == ["set_cover_position"]


def air_config(provider_ids=("roof",), *, evidence_entity=None):
    return {
        "name": "Fresh air",
        "activation_entities": ["switch.extractor"],
        "acquisition_timeout": 12,
        "providers": [
            {
                "id": key,
                "resource_id": key,
                "target": {"position": 100},
                "evidence": [
                    {
                        "entity_id": evidence_entity
                        or ("cover.raw" if key == "roof" else "cover.other"),
                        "attribute": "current_position",
                        "operator": "gte",
                        "value": 20,
                        "kind": "position",
                    }
                ],
            }
            for key in provider_ids
        ],
    }


async def test_requirement_uses_raw_position_and_attribute_only_updates(hass, runtime_factory):
    factory, commands = runtime_factory
    reported(hass)
    hass.states.async_set("cover.virtual", "open", {"current_position": 100, "assumed_state": True})
    hass.states.async_set("switch.extractor", "on")
    runtime = await factory(requirements={"air": air_config()})
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "acquiring"
    assert len(commands) == 1
    # Physical adapter remains open across attribute updates; confirmation must track attributes.
    reported(hass, position=10, state="open")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "acquiring"
    reported(hass, position=30, state="open")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "satisfied"
    reported(hass, position=10, state="open")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "acquiring"


async def test_unknown_evidence_never_confirms_and_activation_unknown_is_inert(
    hass, runtime_factory
):
    factory, commands = runtime_factory
    reported(hass, position=100, restored=True)
    hass.states.async_set("switch.extractor", "unknown")
    runtime = await factory(requirements={"air": air_config()})
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "unknown"
    assert commands == []
    hass.states.async_set("switch.extractor", "on")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "unknown"
    reported(hass, position=100)
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "satisfied"


async def test_requirement_fallback_cooldown_and_manual_last_inlet_close(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass)
    reported(hass, "cover.other")
    hass.states.async_set("switch.extractor", "off")
    runtime = await factory(
        resources={"roof": cover_data(), "other": cover_data("Other", "cover.other")},
        requirements={"air": air_config(("roof", "other"))},
    )
    await runtime.async_set_mode("roof", "live")
    await runtime.async_set_mode("other", "live")
    hass.states.async_set("switch.extractor", "on")
    await hass.async_block_till_done()
    assert commands[-1][1]["entity_id"] == "cover.raw"
    await advance(hass, freezer, 12)
    assert runtime.requirement_results["air"].acquiring_provider == "other"
    assert commands[-1][1]["entity_id"] == "cover.other"
    await advance(hass, freezer, 12)
    assert runtime.requirement_results["air"].status == "unmet"
    count = len(commands)
    await advance(hass, freezer, 20)
    assert len(commands) == count
    reported(hass, "cover.other", 100)
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].selected_provider == "other"
    await runtime.async_request("other", target={"position": 0})
    await hass.async_block_till_done()
    assert commands[-1][1] == {"entity_id": "cover.other", "position": 0}
    reported(hass, "cover.other", 0)
    await hass.async_block_till_done()
    assert runtime.decisions["other"].target == Target(position=0)
    assert runtime.requirement_results["air"].status == "unmet"
    assert hass.states.get("switch.extractor").state == "on"
    assert all(item[1]["entity_id"].startswith("cover.") for item in commands)


async def test_policy_attribute_target_changes_without_state_transition(hass, runtime_factory):
    factory, commands = runtime_factory
    reported(hass)
    hass.states.async_set("schedule.day", "on", {"position": 20})
    runtime = await factory(
        policies={
            "day": {
                "name": "Day",
                "kind": "state",
                "resource_id": "roof",
                "target_entity": "schedule.day",
                "target_attribute": "position",
                "target_field": "position",
                "eligibility_entity": "schedule.day",
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].target == Target(position=20)
    hass.states.async_set("schedule.day", "on", {"position": 60})
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].target == Target(position=60)
    assert commands[0][1]["position"] == 20


async def test_closed_runtime_ignores_source_updates_and_cancels_retry(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    await runtime.async_close()
    reported(hass, position=50)
    await advance(hass, freezer, 60)
    assert len(commands) == 1
    assert all(task.done() for task in runtime._tasks)
    with pytest.raises(HomeAssistantError, match="unloaded"):
        await runtime.async_reconcile("roof")


@pytest.mark.parametrize(
    "field,status", [("restriction_entity", "restricted"), ("fault_entity", "fault")]
)
async def test_unknown_safety_input_inhibits_until_explicit_clear(
    hass, runtime_factory, freezer, field, status
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(resources={"roof": cover_data(**{field: "binary_sensor.block"})})
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].status == status
    await advance(hass, freezer, 60)
    assert commands == []
    hass.states.async_set("binary_sensor.block", "off")
    await hass.async_block_till_done()
    assert len(commands) == 1


async def test_expiry_during_suspended_actuation_cannot_send(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = runtime.adapter("roof")
    original = adapter.async_apply

    async def delayed(target, still_current):
        entered.set()
        await release.wait()
        return await original(target, still_current)

    with patch.object(adapter, "async_apply", delayed):
        await runtime.async_request("roof", target={"position": 100}, duration=5)
        await entered.wait()
        freezer.move_to(dt_util.utcnow() + timedelta(seconds=5))
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        release.set()
        await hass.async_block_till_done()
    assert commands == []
    assert runtime.decisions["roof"].status == "idle"


@pytest.mark.parametrize("value", [None, "not a number", float("nan"), 101, True])
async def test_invalid_dynamic_policy_value_cannot_dispatch(hass, runtime_factory, value):
    factory, commands = runtime_factory
    reported(hass)
    hass.states.async_set("schedule.day", "on", {"position": value})
    runtime = await factory(
        policies={
            "day": {
                "name": "Day",
                "kind": "state",
                "resource_id": "roof",
                "target_entity": "schedule.day",
                "target_attribute": "position",
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].status == "idle"
    assert commands == []


async def test_airflow_notification_occurs_once_per_unmet_transition(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    hass.states.async_set("switch.extractor", "on")
    with (
        patch(
            "custom_components.ha_operator.runtime.persistent_notification.async_create"
        ) as create,
        patch(
            "custom_components.ha_operator.runtime.persistent_notification.async_dismiss"
        ) as dismiss,
    ):
        await factory(requirements={"air": air_config()})
        assert create.call_count == 1
        for _ in range(3):
            reported(hass)
            await hass.async_block_till_done()
        assert create.call_count == 1
        reported(hass, position=100)
        await hass.async_block_till_done()
        assert dismiss.call_count == 1
        reported(hass)
        await hass.async_block_till_done()
        assert create.call_count == 2


@pytest.mark.parametrize("bad_value", ["invalid", [], {}, 1])
async def test_dynamic_switch_policy_rejects_bad_value_then_recovers(
    hass, runtime_factory, bad_value
):
    factory, commands = runtime_factory
    hass.states.async_set("switch.raw", "off")
    hass.states.async_set("schedule.device", "on", {"target": bad_value})

    async def command(call):
        commands.append((call.service, dict(call.data), dt_util.utcnow().timestamp()))

    hass.services.async_register("switch", "turn_on", command)
    runtime = await factory(
        resources={"plug": {"name": "Plug", "kind": "switch", "entity_id": "switch.raw"}},
        policies={
            "day": {
                "name": "Day",
                "kind": "state",
                "resource_id": "plug",
                "target_entity": "schedule.device",
                "target_attribute": "target",
                "target_field": "on",
            }
        },
    )
    await runtime.async_set_mode("plug", "live")
    await hass.async_block_till_done()
    assert runtime.decisions["plug"].status == "idle"
    assert commands == []
    hass.states.async_set("schedule.device", "on", {"target": "on"})
    await hass.async_block_till_done()
    assert runtime.decisions["plug"].target == Target(on=True)
    assert commands[0][:2] == ("turn_on", {"entity_id": "switch.raw"})
    assert hass.states.get("switch.raw").state == "off"


async def test_requirement_skips_provider_lacking_capability_then_recovers(hass, runtime_factory):
    factory, commands = runtime_factory
    reported(hass, supported_features=0)
    hass.states.async_set("switch.extractor", "on")
    runtime = await factory(requirements={"air": air_config()})
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "unmet"
    assert commands == []
    reported(hass)
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "acquiring"
    assert len(commands) == 1


async def test_passive_measured_airflow_confirms_without_actuator(hass, runtime_factory):
    factory, commands = runtime_factory
    hass.states.async_set("switch.extractor", "on")
    hass.states.async_set("sensor.airflow", "12")
    runtime = await factory(
        resources={},
        requirements={
            "air": {
                "name": "Fresh air",
                "activation_entities": ["switch.extractor"],
                "providers": [
                    {
                        "id": "vent",
                        "evidence": [
                            {
                                "entity_id": "sensor.airflow",
                                "kind": "airflow",
                                "operator": "gte",
                                "value": 10,
                            }
                        ],
                    }
                ],
            }
        },
    )
    assert runtime.requirement_results["air"].status == "satisfied"
    hass.states.async_set("sensor.airflow", "5")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "unmet"
    assert commands == []
    assert runtime.explain()["resources"] == {}
    await runtime.async_close()
    await runtime.async_close()


async def test_empty_installation_can_start_reconcile_and_close(hass, runtime_factory):
    factory, commands = runtime_factory
    runtime = await factory(resources={})
    await runtime.async_reconcile()
    assert runtime.explain()["resources"] == {}
    assert runtime.explain()["requirements"] == {}
    assert commands == []


async def test_airflow_acquisition_and_cooldown_share_one_notification_episode(
    hass, runtime_factory, freezer
):
    factory, _ = runtime_factory
    reported(hass)
    hass.states.async_set("switch.extractor", "off")
    with (
        patch(
            "custom_components.ha_operator.runtime.persistent_notification.async_create"
        ) as create,
        patch(
            "custom_components.ha_operator.runtime.persistent_notification.async_dismiss"
        ) as dismiss,
    ):
        runtime = await factory(requirements={"air": air_config()})
        await runtime.async_set_mode("roof", "live")
        assert create.call_count == 0
        hass.states.async_set("switch.extractor", "on")
        await hass.async_block_till_done()
        assert runtime.requirement_results["air"].status == "acquiring"
        assert create.call_count == 1
        await advance(hass, freezer, 12)
        assert runtime.requirement_results["air"].status == "unmet"
        assert create.call_count == 1 and dismiss.call_count == 0
        await advance(hass, freezer, 300)
        assert runtime.requirement_results["air"].status == "acquiring"
        assert create.call_count == 1 and dismiss.call_count == 0
        hass.states.async_set("switch.extractor", "off")
        await hass.async_block_till_done()
        assert runtime.requirement_results["air"].status == "inactive"
        assert dismiss.call_count == 1
        hass.states.async_set("switch.extractor", "on")
        await hass.async_block_till_done()
        assert create.call_count == 2
        reported(hass, position=100)
        await hass.async_block_till_done()
        assert dismiss.call_count == 2


async def test_explain_reports_independent_raw_evidence_and_activation(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    hass.states.async_set("switch.extractor", "on")
    runtime = await factory(requirements={"air": air_config()})
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    explanation = runtime.explain("roof")
    assert explanation["resources"]["roof"]["decision"]["target"]["position"] == 100
    requirement = explanation["requirements"]["air"]
    assert requirement["status"] == "acquiring"
    assert requirement["activation_values"] == {"switch.extractor": "on"}
    provider = requirement["providers"]["roof"]
    assert provider["target"] == {"position": 100}
    assert provider["confirmed"] is False
    assert provider["evidence"] == [
        {
            "kind": "position",
            "entity_id": "cover.raw",
            "attribute": "current_position",
            "operator": "gte",
            "expected": 20,
            "observed": 0,
            "confirmed": False,
        }
    ]
    reported(hass, position=100, restored=True)
    await hass.async_block_till_done()
    predicate = runtime.explain()["requirements"]["air"]["providers"]["roof"]["evidence"][0]
    assert predicate["observed"] is None and predicate["confirmed"] is None
    hass.states.async_set("switch.extractor", "unknown")
    await hass.async_block_till_done()
    assert runtime.explain()["requirements"]["air"]["activation_values"] == {
        "switch.extractor": None
    }


async def test_explain_preserves_typed_sensor_evidence_without_unrelated_attributes(
    hass, runtime_factory
):
    factory, _ = runtime_factory
    hass.states.async_set("switch.extractor", "on")
    hass.states.async_set("sensor.airflow", "ready", {"measured": 12.5, "private": "not selected"})
    hass.states.async_set("binary_sensor.contact", "on")
    runtime = await factory(
        resources={},
        requirements={
            "air": {
                "name": "Fresh air",
                "activation_entities": ["switch.extractor"],
                "providers": [
                    {
                        "id": "passive",
                        "evidence": [
                            {
                                "entity_id": "sensor.airflow",
                                "attribute": "measured",
                                "kind": "airflow",
                                "operator": "gte",
                                "value": 10,
                            },
                            {
                                "entity_id": "binary_sensor.contact",
                                "kind": "contact",
                                "operator": "eq",
                                "value": "on",
                            },
                        ],
                    }
                ],
            }
        },
    )
    explanation = runtime.explain()
    provider = explanation["requirements"]["air"]["providers"]["passive"]
    assert provider["resource_id"] is None and provider["target"] is None
    assert provider["confirmed"] is True
    assert provider["evidence"][0]["observed"] == 12.5
    assert provider["evidence"][1]["observed"] == "on"
    assert provider["evidence"][1]["attribute"] is None
    assert "not selected" not in str(explanation)


@pytest.mark.parametrize("transition", ["release", "stop", "observe", "expire", "satisfied"])
async def test_ineligible_work_after_suspended_apply_has_no_retry_timer(
    hass, runtime_factory, freezer, transition
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = runtime.adapter("roof")
    original = adapter.async_apply

    async def delayed(target, still_current):
        entered.set()
        await release.wait()
        return await original(target, still_current)

    with patch.object(adapter, "async_apply", delayed):
        await runtime.async_request("roof", target={"position": 100}, duration=10)
        await entered.wait()
        if transition == "release":
            await runtime.async_release("roof")
        elif transition == "stop":
            immediate_stop = asyncio.Event()

            async def observe_stop(call):
                commands.append((call.service, dict(call.data), dt_util.utcnow().timestamp()))
                immediate_stop.set()

            hass.services.async_register("cover", "stop_cover", observe_stop)
            stopping = asyncio.create_task(runtime.async_stop("roof"))
            await immediate_stop.wait()
            assert [item[0] for item in commands] == ["stop_cover"]
            assert not stopping.done()
            release.set()
            await stopping
        elif transition == "observe":
            await runtime.async_set_mode("roof", "observe")
        elif transition == "expire":
            # No timer callback here: the dispatch guard itself discovers expiry.
            freezer.move_to(dt_util.utcnow() + timedelta(seconds=10))
        else:
            reported(hass, position=100)
        release.set()
        await hass.async_block_till_done()
    expected = ["stop_cover", "stop_cover"] if transition == "stop" else []
    assert [item[0] for item in commands] == expected
    assert "roof" not in runtime._timers
    assert runtime.explain("roof")["resources"]["roof"]["next_attempt"] is None
    assert all(not task.done() for task in runtime._tasks)
    await advance(hass, freezer, 60)
    assert [item[0] for item in commands] == expected


@pytest.mark.parametrize("new_target", [30, 100])
async def test_retry_cleanup_preserves_command_spacing_for_new_intent(
    hass, runtime_factory, freezer, new_target
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    first_sent = commands[0][2]
    await advance(hass, freezer, 1)
    await runtime.async_release("roof")
    await hass.async_block_till_done()
    assert runtime.explain("roof")["resources"]["roof"]["next_attempt"] is None
    await runtime.async_request("roof", target={"position": new_target})
    await hass.async_block_till_done()
    assert len(commands) == 1
    assert runtime.explain("roof")["resources"]["roof"]["next_attempt"] == first_sent + 5
    await advance(hass, freezer, 4)
    assert [item[1]["position"] for item in commands] == [100, new_target]
    assert commands[1][2] - commands[0][2] == 5


async def test_conjunctive_airflow_activation_releases_only_its_policy_override(
    hass, runtime_factory, freezer
):
    factory, commands = runtime_factory
    reported(hass)
    hass.states.async_set("switch.extractor", "on")
    hass.states.async_set("binary_sensor.fireplace", "off")
    requirement = air_config()
    requirement["activation_entities"] = ["switch.extractor", "binary_sensor.fireplace"]
    runtime = await factory(
        requirements={"air": requirement},
        policies={
            "evening": {
                "name": "Evening",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 0},
            }
        },
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "inactive"
    assert runtime.decisions["roof"].source == "policy:evening"
    assert commands == []
    hass.states.async_set("binary_sensor.fireplace", "unknown")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "unknown"
    assert runtime.decisions["roof"].source == "policy:evening"
    hass.states.async_set("switch.extractor", "off")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "inactive"
    hass.states.async_set("binary_sensor.fireplace", "on")
    hass.states.async_set("switch.extractor", "on")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "acquiring"
    assert runtime.decisions["roof"].source == "requirement:air:roof"
    assert commands[-1][1]["position"] == 100
    reported(hass, position=100)
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "satisfied"
    assert runtime.explain("roof")["resources"]["roof"]["next_attempt"] is None
    hass.states.async_set("switch.extractor", "off")
    await hass.async_block_till_done()
    assert runtime.requirement_results["air"].status == "inactive"
    assert runtime.decisions["roof"].source == "policy:evening"
    await advance(hass, freezer, 5)
    assert [item[1]["position"] for item in commands] == [100, 0]
    assert runtime.policy_enabled("evening") is True
    assert hass.states.get("switch.extractor").state == "off"


async def test_motion_ending_early_uses_original_retry_deadline(hass, runtime_factory, freezer):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory(resources={"roof": cover_data(retry_interval=5, movement_timeout=20)})
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    reported(hass, position=10, state="opening")
    await hass.async_block_till_done()
    await advance(hass, freezer, 10)
    assert len(commands) == 1
    reported(hass, position=10, state="open")
    await hass.async_block_till_done()
    assert len(commands) == 2
    assert commands[1][2] - commands[0][2] == 10


async def test_synchronous_physical_confirmation_keeps_dispatch_history_without_retry(
    hass, runtime_factory
):
    factory, commands = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")

    async def command(call):
        commands.append((call.service, dict(call.data), dt_util.utcnow().timestamp()))
        reported(hass, position=call.data["position"])

    hass.services.async_register("cover", "set_cover_position", command)
    await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    assert len(commands) == 1
    assert runtime.decisions["roof"].status == "satisfied"
    assert runtime.last_commands["roof"]["target"] == {"position": 100}
    assert runtime.explain("roof")["resources"]["roof"]["next_attempt"] is None
    assert "roof" not in runtime._timers
