"""Native entity ingress, truthful state, lifecycle and diagnostic disclosure tests."""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.components.fan import FanEntityFeature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.ha_operator import (
    binary_sensor,
    button,
    cover,
    fan,
    select,
    sensor,
    switch,
)
from custom_components.ha_operator.core import ManualLease, Observation, Target
from custom_components.ha_operator.diagnostics import async_get_config_entry_diagnostics


@pytest.fixture
def fake_runtime():
    """A request boundary mock; observations never follow its mocked writes."""
    callbacks = set()

    def subscribe(callback):
        callbacks.add(callback)
        return lambda: callbacks.remove(callback)

    resources = {
        "roof": {"kind": "cover", "name": "Private roof"},
        "native": {"kind": "fan", "name": "Native"},
        "relay": {
            "kind": "relay_fan",
            "name": "Relay",
            "profiles": {
                "off": {"percentage": 0},
                "low": {"percentage": 50},
                "high": {"percentage": 100},
                "inward": {"percentage": 100},
            },
        },
        "plug": {"kind": "switch", "name": "Plug"},
    }
    adapters = {
        "roof": SimpleNamespace(supported_features=15),
        "native": SimpleNamespace(supported_features=63, speed_count=4),
        "relay": SimpleNamespace(supported_features=53),
        "plug": SimpleNamespace(supported_features=0),
    }
    result = SimpleNamespace(
        status="satisfied",
        selected_provider="roof",
        acquiring_provider=None,
        reason="confirmed",
    )
    runtime = SimpleNamespace(
        resources=resources,
        policies={"morning": {"name": "Morning", "kind": "state"}},
        requirements={"air": {"name": "Air"}},
        observations={
            "roof": Observation(Target(position=0), True),
            "native": Observation(Target(on=False, percentage=0, direction="forward"), True),
            "relay": Observation(Target(on=True, percentage=50, profile="low"), True),
            "plug": Observation(Target(on=False), True),
        },
        decisions={},
        requirement_results={"air": result},
        next_attempts={},
        attempts={},
        last_commands={},
        fault=None,
        shadow_locked=False,
        trace_health=Mock(return_value={"enabled": False, "healthy": True, "complete": True}),
        history=deque(maxlen=100),
        mode=Mock(return_value="observe"),
        manual=Mock(return_value=None),
        policy_enabled=Mock(return_value=True),
        adapter=lambda key: adapters[key],
        subscribe=subscribe,
        callbacks=callbacks,
        async_request=AsyncMock(),
        async_release=AsyncMock(),
        async_reconcile=AsyncMock(),
        async_stop=AsyncMock(),
        async_set_mode=AsyncMock(),
        async_set_policy_enabled=AsyncMock(),
    )
    return runtime


@pytest.mark.parametrize(
    "module,expected",
    [
        (cover, {"roof": 1}),
        (fan, {"native": 1, "relay": 1}),
        (switch, {"plug": 1, "morning": 1}),
        (sensor, {"roof": 5, "native": 5, "relay": 5, "plug": 5, "air": 2}),
        (binary_sensor, {"roof": 1, "native": 1, "relay": 1, "plug": 1, "air": 1}),
        (button, {"roof": 2, "native": 2, "relay": 2, "plug": 2}),
        (select, {"roof": 1, "native": 1, "relay": 1, "plug": 1}),
    ],
)
async def test_platforms_attach_stable_subentries(hass, fake_runtime, module, expected):
    added = {}

    def add(entities, *, config_subentry_id):
        assert config_subentry_id not in added
        added[config_subentry_id] = list(entities)

    await module.async_setup_entry(hass, SimpleNamespace(runtime_data=fake_runtime), add)
    assert {key: len(items) for key, items in added.items()} == expected
    for key, items in added.items():
        for entity in items:
            assert entity.unique_id.startswith(f"{key}_")
            assert entity.device_info["identifiers"] == {("ha_operator", key)}
            assert entity.should_poll is False


async def test_cover_commands_do_not_claim_motion(fake_runtime):
    entity = cover.OperatorCover(fake_runtime, "roof")
    assert entity.available and entity.is_closed and entity.current_cover_position == 0
    assert entity.supported_features == CoverEntityFeature(15)
    await entity.async_open_cover()
    fake_runtime.async_request.assert_awaited_with(
        "roof", target={"position": 100}, source="entity"
    )
    assert entity.is_closed  # successful admission does not change physical feedback
    await entity.async_close_cover()
    fake_runtime.async_request.assert_awaited_with("roof", target={"position": 0}, source="entity")
    await entity.async_set_cover_position(position=42)
    fake_runtime.async_request.assert_awaited_with("roof", target={"position": 42}, source="entity")
    await entity.async_stop_cover()
    fake_runtime.async_stop.assert_awaited_once_with("roof")
    fake_runtime.adapter("roof").supported_features = 7
    assert not entity.supported_features & CoverEntityFeature.STOP
    assert entity.extra_state_attributes == {"control_mode": "observe"}
    fake_runtime.observations["roof"] = Observation(Target(position=49.6), True)
    assert entity.current_cover_position == 50 and entity.is_closed is False
    fake_runtime.observations["roof"] = Observation(Target(position=100), False)
    assert (
        not entity.available and entity.current_cover_position is None and entity.is_closed is None
    )
    del fake_runtime.observations["roof"]
    assert not entity.available


async def test_fan_commands_and_features(fake_runtime):
    entity = fan.OperatorFan(fake_runtime, "native")
    assert entity.is_on is False and entity.percentage == 0
    assert entity.current_direction == "forward" and entity.speed_count == 4
    assert entity.supported_features == (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.DIRECTION
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    with pytest.raises(ServiceValidationError, match="Preset modes"):
        await entity.async_turn_on(preset_mode="sleep")
    await entity.async_turn_on()
    fake_runtime.async_request.assert_awaited_with("native", target={"on": True}, source="entity")
    await entity.async_turn_on(percentage=50)
    fake_runtime.async_request.assert_awaited_with(
        "native", target={"on": True, "percentage": 50}, source="entity"
    )
    await entity.async_turn_on(percentage=0)
    fake_runtime.async_request.assert_awaited_with(
        "native", target={"on": False, "percentage": 0}, source="entity"
    )
    await entity.async_set_percentage(100)
    fake_runtime.async_request.assert_awaited_with(
        "native", target={"on": True, "percentage": 100}, source="entity"
    )
    await entity.async_turn_off()
    fake_runtime.async_request.assert_awaited_with("native", target={"on": False}, source="entity")
    await entity.async_set_direction("reverse")
    fake_runtime.async_request.assert_awaited_with(
        "native", target={"direction": "reverse"}, source="entity"
    )
    assert entity.is_on is False and entity.current_direction == "forward"
    del fake_runtime.observations["native"]
    assert entity.is_on is None and entity.percentage is None and entity.current_direction is None
    relay = fan.OperatorFan(fake_runtime, "relay")
    assert relay.speed_count == 2 and relay.percentage_step == 50


async def test_switch_and_policy_controls(fake_runtime):
    entity = switch.OperatorSwitch(fake_runtime, "plug")
    assert entity.is_on is False
    await entity.async_turn_on()
    fake_runtime.async_request.assert_awaited_with("plug", target={"on": True}, source="entity")
    assert entity.is_on is False
    await entity.async_turn_off()
    fake_runtime.async_request.assert_awaited_with("plug", target={"on": False}, source="entity")
    del fake_runtime.observations["plug"]
    assert entity.is_on is None
    policy = switch.PolicySwitch(fake_runtime, "morning")
    assert policy.is_on is True
    await policy.async_turn_off()
    fake_runtime.async_set_policy_enabled.assert_awaited_with("morning", False)
    await policy.async_turn_on()
    fake_runtime.async_set_policy_enabled.assert_awaited_with("morning", True)


async def test_control_callbacks_and_failed_persistence(fake_runtime):
    mode = select.ModeSelect(fake_runtime, "roof")
    assert mode.current_option == "observe"
    await mode.async_select_option("live")
    fake_runtime.async_set_mode.assert_awaited_once_with("roof", "live")
    assert mode.current_option == "observe"  # runtime publishes only committed mode
    with pytest.raises(ServiceValidationError):
        await mode.async_select_option("automatic")
    await button.OperatorButton(fake_runtime, "roof", "release").async_press()
    fake_runtime.async_release.assert_awaited_once_with("roof")
    await button.OperatorButton(fake_runtime, "roof", "reconcile").async_press()
    fake_runtime.async_reconcile.assert_awaited_once_with("roof")
    fake_runtime.async_request.side_effect = ServiceValidationError("durable save failed")
    with pytest.raises(ServiceValidationError, match="durable save failed"):
        await cover.OperatorCover(fake_runtime, "roof").async_open_cover()
    assert fake_runtime.observations["roof"].target.position == 0


async def test_native_lifecycle_updates_and_unsubscribes(hass, fake_runtime):
    component = EntityComponent(logging.getLogger(__name__), "cover", hass)
    entity = cover.OperatorCover(fake_runtime, "roof")
    await component.async_add_entities([entity])
    assert len(fake_runtime.callbacks) == 1
    assert hass.states.get(entity.entity_id).attributes["current_position"] == 0
    fake_runtime.observations["roof"] = Observation(Target(position=60), True)
    for callback in tuple(fake_runtime.callbacks):
        callback()
    assert hass.states.get(entity.entity_id).attributes["current_position"] == 60
    await component.async_remove_entity(entity.entity_id)
    assert not fake_runtime.callbacks


@pytest.mark.parametrize(
    "target,value",
    [
        (None, None),
        (Target(profile="low", on=True, percentage=50), "low"),
        (Target(position=20), 20),
        (Target(on=False, percentage=50), "off"),
        (Target(on=True, percentage=50), 50),
        (Target(on=True), "on"),
        (Target(direction="reverse"), "reverse"),
    ],
)
def test_target_scalar_preserves_off(target, value):
    assert sensor.target_value(target) == value


def test_resource_sensors_keep_intent_separate(fake_runtime):
    desired = sensor.ResourceSensor(fake_runtime, "roof", "desired")
    status = sensor.ResourceSensor(fake_runtime, "roof", "status")
    expiry = sensor.ResourceSensor(fake_runtime, "roof", "expiry")
    retry = sensor.ResourceSensor(fake_runtime, "roof", "next_attempt")
    attempts = sensor.ResourceSensor(fake_runtime, "roof", "attempts")
    assert desired.native_value is None and desired.extra_state_attributes == {"target": None}
    assert status.native_value == "initializing" and status.extra_state_attributes is None
    assert expiry.native_value is None and expiry.extra_state_attributes is None
    assert retry.native_value is None and attempts.native_value == 0
    assert (
        not retry.entity_registry_enabled_default and not attempts.entity_registry_enabled_default
    )
    fake_runtime.decisions["roof"] = SimpleNamespace(
        target=Target(position=100), status="pending", reason="target not reached", source="manual"
    )
    fake_runtime.next_attempts["roof"] = 1000
    fake_runtime.attempts["roof"] = 2
    fake_runtime.manual.return_value = ManualLease("roof", "target", Target(position=100), 2000)
    assert desired.native_value == 100
    assert cover.OperatorCover(fake_runtime, "roof").current_cover_position == 0
    assert desired.extra_state_attributes == {"target": {"position": 100}}
    assert status.native_value == "pending"
    assert status.extra_state_attributes == {"reason": "target not reached", "source": "manual"}
    assert expiry.native_value == datetime.fromtimestamp(2000, UTC)
    assert retry.native_value == datetime.fromtimestamp(1000, UTC) and attempts.native_value == 2
    fake_runtime.fault = "storage"
    assert status.native_value == "fault"
    fake_runtime.manual.return_value = ManualLease("roof", "hands_off")
    assert expiry.native_value is None


def test_manual_and_requirement_indicators(fake_runtime):
    manual = binary_sensor.ManualSensor(fake_runtime, "roof")
    assert not manual.is_on
    fake_runtime.manual.return_value = ManualLease("roof", "hands_off")
    assert manual.is_on
    status = sensor.RequirementSensor(fake_runtime, "air", "status")
    provider = sensor.RequirementSensor(fake_runtime, "air", "provider")
    unmet = binary_sensor.UnmetSensor(fake_runtime, "air")
    assert status.native_value == "satisfied" and provider.native_value == "roof"
    assert not unmet.is_on and provider.extra_state_attributes is None
    assert status.extra_state_attributes == {"reason": "confirmed", "acquiring_provider": None}
    fake_runtime.requirement_results["air"].status = "unmet"
    assert unmet.is_on
    fake_runtime.requirement_results["air"].status = "unknown"
    assert unmet.is_on is None
    del fake_runtime.requirement_results["air"]
    assert unmet.is_on is None and status.native_value is None and provider.native_value is None
    assert status.extra_state_attributes is None


async def test_diagnostics_allowlist_and_bounded_history(hass, fake_runtime):
    secret = "private-address-and-token"
    fake_runtime.resources["roof"].update(token=secret, entity_id="cover.secret_room")
    fake_runtime.fault = secret
    fake_runtime.shadow_locked = True
    fake_runtime.trace_health.return_value = {
        "enabled": True,
        "healthy": False,
        "complete": False,
        "session_id": secret,
        "last_sequence": 24,
        "durable_sequence": 20,
        "queued_records": 4,
        "dropped_records": 0,
        "write_errors": 1,
        "rotations": 2,
        "last_heartbeat": 10,
        "last_write_at": 9,
        "path": secret,
        "records": [{"secret": secret}],
        "token": secret,
    }
    fake_runtime.decisions["roof"] = SimpleNamespace(
        status="pending", source=secret, target=Target(position=100, profile=secret)
    )
    fake_runtime.manual.return_value = ManualLease(
        "roof", "target", Target(position=100, direction="reverse"), expires_at=5000
    )
    fake_runtime.last_commands["roof"] = {"target": {"on": True}, "at": 4000, "secret": secret}
    fake_runtime.history = [
        {
            "resource_id": "roof",
            "source": secret,
            "status": "pending",
            "target": {"position": 100, "direction": secret, "secret": secret},
            "at": i,
        }
        for i in range(120)
    ]
    result = await async_get_config_entry_diagnostics(
        hass, SimpleNamespace(runtime_data=fake_runtime)
    )
    text = json.dumps(result)
    assert secret not in text and "Private roof" not in text and "cover.secret_room" not in text
    assert '"roof"' not in text and '"morning"' not in text
    assert result["faulted"] and len(result["history"]) == 100
    assert result["shadow_locked"] is True
    assert result["trace"]["enabled"] is True
    assert result["trace"]["healthy"] is False
    assert result["trace"]["durable_sequence"] == 20
    assert result["trace"]["queued_records"] == 4
    assert "records" not in result["trace"] and "path" not in result["trace"]
    assert fake_runtime.trace_health.call_count == 1
    assert result["history"][0]["at"] == 20
    assert len(fake_runtime.history) == 120  # download is read-only
    fake_runtime.async_request.assert_not_awaited()
    assert len(result["resources"]) == 4 and len(result["policies"]) == 1
    fake_runtime.manual.return_value = ManualLease("roof", "hands_off")
    fake_runtime.history = [{"status": secret, "target": secret, "at": secret}]
    fake_runtime.observations = {}
    fake_runtime.requirement_results = {}
    fake_runtime.resources["roof"]["kind"] = secret
    result = await async_get_config_entry_diagnostics(
        hass, SimpleNamespace(runtime_data=fake_runtime)
    )
    assert secret not in json.dumps(result)
    assert result["history"][0]["status"] == "unknown"
