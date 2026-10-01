"""Physical-boundary checks with independent state reports and mocked transport."""

import asyncio
from copy import deepcopy
from unittest.mock import ANY, AsyncMock, patch

import pytest
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.components.fan import FanEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_operator.adapters import (
    Adapter,
    CoverAdapter,
    FanAdapter,
    RelayFanAdapter,
    SwitchAdapter,
    create_adapter,
)
from custom_components.ha_operator.core import Target

COVER_FEATURES = int(CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP)
FAN_FEATURES = int(
    FanEntityFeature.SET_SPEED
    | FanEntityFeature.DIRECTION
    | FanEntityFeature.TURN_ON
    | FanEntityFeature.TURN_OFF
)


@pytest.fixture
def relay_config():
    return {
        "kind": "relay_fan",
        "outputs": ["switch.low", "switch.high", "switch.reverse"],
        "profiles": {
            "off": {
                "outputs": {"switch.low": False, "switch.high": False, "switch.reverse": False},
                "percentage": 0,
            },
            "low": {
                "outputs": {"switch.low": True, "switch.high": False, "switch.reverse": False},
                "percentage": 50,
                "direction": "forward",
            },
            "high": {
                "outputs": {"switch.low": False, "switch.high": True, "switch.reverse": False},
                "percentage": 100,
                "direction": "forward",
            },
            "reverse": {
                "outputs": {"switch.low": False, "switch.high": False, "switch.reverse": True},
                "percentage": 100,
                "direction": "reverse",
            },
        },
        "default_target": {"profile": "low"},
        "reversal_dead_time": 0.001,
        "movement_timeout": 0.015,
    }


def set_relays(hass, config, profile="off"):
    for entity_id, on in config["profiles"][profile]["outputs"].items():
        hass.states.async_set(entity_id, "on" if on else "off")


def set_fan(hass, state="off", **attributes):
    hass.states.async_set(
        "fan.raw",
        state,
        {"supported_features": FAN_FEATURES, "percentage": 0, "percentage_step": 50, **attributes},
    )


def set_cover(hass, state="closed", **attributes):
    hass.states.async_set(
        "cover.raw",
        state,
        {"supported_features": COVER_FEATURES, "current_position": 0, **attributes},
    )


def cover(hass, **config):
    return CoverAdapter(hass, "resource", {"entity_id": "cover.raw", **config})


def fan(hass, **config):
    return FanAdapter(hass, "resource", {"entity_id": "fan.raw", **config})


async def test_cover_feedback_is_not_a_service_response(hass):
    set_cover(hass)
    adapter = cover(hass)
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        assert await adapter.async_apply(Target(position=72), lambda: True)
        call.assert_awaited_once_with(
            "cover",
            "set_cover_position",
            {"entity_id": "cover.raw", "position": 72},
            blocking=True,
            context=ANY,
        )
        assert adapter.read_observation(0).target == Target(position=0)
        set_cover(hass, "opening", current_position=10)
        assert adapter.read_observation(0).moving
        assert adapter.read_observation(0).target == Target(position=10)
        await adapter.async_stop()
        assert call.await_args.args[1] == "stop_cover"
    assert adapter.supported_features == 15


@pytest.mark.parametrize(
    "attributes",
    [
        {"current_position": None},
        {"current_position": float("nan")},
        {"current_position": True},
        {"current_position": -1},
        {"current_position": 101},
        {"assumed_state": True},
        {"restored": True},
    ],
)
async def test_cover_unknown_or_optimistic_position_never_becomes_evidence(hass, attributes):
    set_cover(hass, **attributes)
    assert cover(hass).read_observation(4).target is None


async def test_missing_cover_and_unsupported_capabilities(hass):
    adapter = cover(hass)
    assert adapter.supported_features == 0
    assert not adapter.supports_stop
    assert not adapter.read_observation(7).available
    assert adapter.read_observation(7).reported_at == 7
    with pytest.raises(ValueError, match="support"):
        adapter.normalize(Target(position=50))
    with pytest.raises(HomeAssistantError, match="STOP"):
        await adapter.async_stop()
    set_cover(hass, supported_features=int(CoverEntityFeature.OPEN))
    assert adapter.supported_features == 0


@pytest.mark.parametrize(
    "target",
    [
        Target(),
        Target(on=True),
    ],
)
async def test_cover_rejects_invalid_targets_before_io(hass, target):
    set_cover(hass)
    with pytest.raises(ValueError):
        cover(hass).normalize(target)


async def test_generation_and_restrictions_checked_at_each_call(hass):
    set_cover(hass)
    adapter = cover(
        hass, restriction_entity="binary_sensor.rain", fault_entity="binary_sensor.fault"
    )
    assert adapter.read_observation(0).restriction == "fault_unknown"
    hass.states.async_set("binary_sensor.fault", "off")
    assert adapter.read_observation(0).restriction == "restricted_unknown"
    hass.states.async_set("binary_sensor.rain", "on")
    assert adapter.read_observation(0).restriction == "restricted"
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        assert not await adapter.async_apply(Target(position=50), lambda: False)
        with pytest.raises(HomeAssistantError, match="restricted"):
            await adapter.async_apply(Target(position=50), lambda: True)
        call.assert_not_called()
        # STOP must bypass both a restriction and pacing.
        await adapter.async_stop()
        assert call.await_count == 1
    hass.states.async_set("binary_sensor.rain", "off")
    assert adapter.read_observation(0).restriction is None


async def test_switch_observations_and_targets(hass):
    adapter = SwitchAdapter(hass, "s", {"entity_id": "switch.raw"})
    assert adapter.read_observation(0).target is None
    for state in ("unknown", "unavailable", "garbage"):
        hass.states.async_set("switch.raw", state)
        assert adapter.read_observation(0).target is None
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        for on in (True, False):
            hass.states.async_set("switch.raw", "on" if on else "off")
            assert adapter.read_observation(0).target == Target(on=on)
            assert await adapter.async_apply(Target(on=on), lambda: True)
            assert call.await_args.args[1] == ("turn_on" if on else "turn_off")
    with pytest.raises(ValueError):
        adapter.normalize(Target(on=1))
    with pytest.raises(ValueError, match="boolean"):
        adapter.normalize(Target())
    assert adapter.supported_features == 0


@pytest.mark.parametrize(
    "requested,expected", [(0, 0), (1, 50), (49.5, 50), (50, 50), (51, 100), (100, 100)]
)
async def test_native_fan_quantization_uses_supported_speed_bins(hass, requested, expected):
    set_fan(hass)
    target = fan(hass).normalize(Target(percentage=requested))
    assert target.percentage == (expected or None)
    assert target.on == bool(expected)


async def test_native_fan_default_and_direction_without_turning_on(hass):
    set_fan(hass, direction="forward")
    adapter = fan(hass, default_target={"percentage": 50})
    assert adapter.supported_features == FAN_FEATURES
    assert adapter.normalize(Target(on=True)) == Target(on=True, percentage=50)
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        assert await adapter.async_apply(Target(direction="reverse"), lambda: True)
        assert [c.args[1] for c in call.await_args_list] == ["set_direction", "turn_off"]
        assert adapter.read_observation(0).target.on is False
        await adapter.async_apply(Target(on=True), lambda: True)
        assert call.await_args.args == (
            "fan",
            "turn_on",
            {"entity_id": "fan.raw", "percentage": 50},
        )
    set_fan(hass, "on", percentage=100)
    assert fan(hass).normalize(Target(on=True)).percentage == 100


async def test_fan_stale_generation_between_direction_and_on(hass):
    set_fan(hass)
    current = True

    async def invalidate(*args, **kwargs):
        nonlocal current
        current = False

    with patch.object(type(hass.services), "async_call", side_effect=invalidate) as call:
        assert not await fan(hass).async_apply(
            Target(percentage=100, direction="reverse"), lambda: current
        )
        assert call.await_count == 1
        assert call.await_args.args[1] == "set_direction"


@pytest.mark.parametrize(
    "target",
    [
        Target(),
        Target(on=False, percentage=10),
        Target(on=True, percentage=0),
        Target(direction="invalid"),
    ],
)
async def test_fan_invalid_target(hass, target):
    set_fan(hass)
    with pytest.raises(ValueError):
        fan(hass).normalize(target)


async def test_fan_unsupported_speed_direction_unknown_state_and_default(hass):
    adapter = fan(hass)
    assert adapter.supported_features == 0
    assert adapter.read_observation(0).target is None
    with pytest.raises(ValueError, match="speed"):
        adapter.normalize(Target(percentage=50))
    set_fan(hass, "unavailable")
    with pytest.raises(ValueError, match="known"):
        adapter.normalize(Target(direction="forward"))
    set_fan(hass)
    with pytest.raises(ValueError, match="default"):
        adapter.normalize(Target(on=True))
    set_fan(hass, supported_features=int(FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF))
    with pytest.raises(ValueError, match="direction"):
        adapter.normalize(Target(direction="forward"))
    assert adapter.normalize(Target(on=True)) == Target(on=True)
    assert adapter.normalize(Target(on=False)) == Target(on=False)
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        await adapter.async_apply(Target(on=True), lambda: True)
        assert "percentage" not in call.await_args.args[2]


@pytest.mark.parametrize("value", [None, True, "slow", float("nan"), -1, 101])
async def test_fan_invalid_feedback_not_used_as_percentage(hass, value):
    set_fan(hass, "on", percentage=value, direction="invalid")
    target = fan(hass).read_observation(0).target
    assert target.on is True
    assert target.percentage is None
    assert target.direction is None


@pytest.mark.parametrize("value", [None, True, "bad", 0, 101, float("nan")])
async def test_fan_invalid_step_is_rejected(hass, value):
    set_fan(hass, percentage_step=value)
    with pytest.raises(ValueError):
        fan(hass).normalize(Target(percentage=40))


async def test_relay_off_feedback_and_profile_normalization(hass, relay_config):
    adapter = RelayFanAdapter(hass, "r", relay_config)
    assert adapter.supported_features == FAN_FEATURES
    assert not adapter.read_observation(8).available
    assert adapter.read_observation(8).reported_at == 8
    set_relays(hass, relay_config)
    assert adapter.read_observation(0).target.profile == "off"
    assert adapter.normalize(Target(on=True)).profile == "low"
    assert adapter.normalize(Target(percentage=1)).profile == "low"
    assert adapter.normalize(Target(percentage=70)).profile == "high"
    assert adapter.normalize(Target(percentage=100, direction="reverse")).profile == "reverse"
    assert adapter.normalize(Target(on=False)).profile == "off"
    assert adapter.normalize(Target(percentage=0)).profile == "off"
    assert adapter.normalize(Target(direction="reverse")).profile == "off"
    set_relays(hass, relay_config, "high")
    assert adapter.normalize(Target(direction="forward")).profile == "high"
    assert adapter.normalize(Target(direction="reverse")).profile == "reverse"
    assert adapter.normalize(Target(profile="high")).profile == "high"
    hass.states.async_set("switch.low", "on")
    assert adapter.read_observation(0).target is None  # conflicting physical combination
    relay_config["default_target"] = "low"
    assert RelayFanAdapter(hass, "r", relay_config).default_profile == "low"


@pytest.mark.parametrize(
    "target",
    [
        Target(),
        Target(direction="sideways"),
        Target(profile="missing"),
        Target(profile="low", on=False),
        Target(profile="low", percentage=100),
        Target(profile="low", direction="reverse"),
        Target(on=True, percentage=0),
        Target(on=False, percentage=40),
        Target(position=40),
    ],
)
async def test_relay_rejects_invalid_targets(hass, relay_config, target):
    set_relays(hass, relay_config)
    with pytest.raises(ValueError):
        RelayFanAdapter(hass, "r", relay_config).normalize(target)


async def test_relay_unknown_direction_and_single_direction_capability(hass, relay_config):
    adapter = RelayFanAdapter(hass, "r", relay_config)
    with pytest.raises(ValueError, match="known"):
        adapter.normalize(Target(direction="forward"))
    relay_config["profiles"].pop("reverse")
    set_relays(hass, relay_config, "low")
    adapter = RelayFanAdapter(hass, "r", relay_config)
    assert not adapter.supported_features & FanEntityFeature.DIRECTION
    with pytest.raises(ValueError, match="direction"):
        adapter.normalize(Target(on=True, direction="reverse"))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(outputs=[]),
        lambda c: c["outputs"].append("switch.low"),
        lambda c: c.update(profiles={}),
        lambda c: c["profiles"]["low"]["outputs"].pop("switch.low"),
        lambda c: c["profiles"]["low"]["outputs"].update({"switch.low": 1}),
        lambda c: c["profiles"]["low"].update(percentage=0),
        lambda c: c["profiles"]["off"].update(percentage=50),
        lambda c: c["profiles"]["low"].update(direction="sideways"),
        lambda c: c["profiles"].pop("off"),
        lambda c: c["profiles"].update(off2=deepcopy(c["profiles"]["off"])),
        lambda c: c.update(reversal_dead_time=0),
        lambda c: c.update(reversal_dead_time=-1),
        lambda c: c.update(default_target={"profile": "off"}),
        lambda c: c.update(default_target={"profile": "missing"}),
    ],
)
async def test_relay_rejects_unsafe_profile_config(hass, relay_config, mutation):
    mutation(relay_config)
    with pytest.raises(ValueError):
        RelayFanAdapter(hass, "r", relay_config)


async def test_relay_confirmed_break_before_make(hass, relay_config):
    set_relays(hass, relay_config, "reverse")
    adapter = RelayFanAdapter(hass, "r", relay_config)
    journal = []

    async def command(domain, service, data, **kwargs):
        entity_id = data["entity_id"]
        if service == "turn_on":
            assert all(hass.states.get(output).state == "off" for output in relay_config["outputs"])
        journal.append((service, entity_id))
        hass.states.async_set(entity_id, "on" if service == "turn_on" else "off")

    with patch.object(type(hass.services), "async_call", side_effect=command):
        assert await adapter.async_apply(Target(profile="high"), lambda: True)
        assert journal == [("turn_off", entity_id) for entity_id in relay_config["outputs"]] + [
            ("turn_on", "switch.high")
        ]
        assert adapter.read_observation(0).target.profile == "high"
        journal.clear()
        assert not await adapter.async_apply(Target(profile="high"), lambda: True)
        assert journal == []
        assert await adapter.async_apply(Target(on=False), lambda: True)
        assert len(journal) == 3


@pytest.mark.parametrize(
    "state,attributes",
    [
        ("unknown", {}),
        ("unavailable", {}),
        ("off", {"assumed_state": True}),
        ("off", {"restored": True}),
        ("on", {}),
    ],
)
async def test_relay_service_success_without_off_confirmation_never_energizes(
    hass, relay_config, state, attributes
):
    set_relays(hass, relay_config, "reverse")
    hass.states.async_set("switch.reverse", state, attributes)
    adapter = RelayFanAdapter(hass, "r", relay_config)
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        with pytest.raises(HomeAssistantError, match="timed out"):
            await adapter.async_apply(Target(profile="high"), lambda: True)
        assert [item.args[1] for item in call.await_args_list] == ["turn_off"] * 3
        # Off remains allowed even when feedback is unknown.
        await adapter.async_apply(Target(on=False), lambda: True)


async def test_relay_waits_for_delayed_off_reports(hass, relay_config):
    relay_config["movement_timeout"] = 1
    set_relays(hass, relay_config, "reverse")
    adapter = RelayFanAdapter(hass, "r", relay_config)

    async def command(domain, service, data, **kwargs):
        if service == "turn_off":
            asyncio.get_running_loop().call_later(
                0.005, hass.states.async_set, data["entity_id"], "off"
            )
        else:
            assert adapter._feedback() == dict.fromkeys(relay_config["outputs"], False)

    with patch.object(type(hass.services), "async_call", side_effect=command) as call:
        assert await adapter.async_apply(Target(profile="high"), lambda: True)
        assert call.await_args.args[1] == "turn_on"


async def test_relay_stale_before_dispatch_and_between_off_calls(hass, relay_config):
    set_relays(hass, relay_config, "reverse")
    adapter = RelayFanAdapter(hass, "r", relay_config)
    current = True

    async def invalidate(*args, **kwargs):
        nonlocal current
        current = False

    with patch.object(type(hass.services), "async_call", side_effect=invalidate) as call:
        assert not await adapter.async_apply(Target(profile="high"), lambda: False)
        call.assert_not_awaited()
        assert not await adapter.async_apply(Target(profile="high"), lambda: current)
        assert call.await_count == 1


async def test_relay_stale_wait_is_bounded_without_feedback_event(hass, relay_config):
    relay_config["movement_timeout"] = 10
    set_relays(hass, relay_config, "reverse")
    current = True

    def invalidate():
        nonlocal current
        current = False

    adapter = RelayFanAdapter(hass, "r", relay_config)
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        asyncio.get_running_loop().call_later(0.005, invalidate)
        async with asyncio.timeout(0.3):
            assert not await adapter.async_apply(Target(profile="high"), lambda: current)
        assert call.await_count == 3


@pytest.mark.parametrize("stale", [True, False])
async def test_relay_rechecks_after_dead_time(hass, relay_config, stale):
    relay_config["reversal_dead_time"] = 0.01
    set_relays(hass, relay_config)
    adapter = RelayFanAdapter(hass, "r", relay_config)
    current = True

    def interfere():
        nonlocal current
        if stale:
            current = False
        else:
            hass.states.async_set("switch.reverse", "on")

    original_wait = adapter._wait_all_off

    async def confirmed_then_interfere(current):
        confirmed = await original_wait(current)
        interfere()
        return confirmed

    with (
        patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call,
        patch.object(adapter, "_wait_all_off", side_effect=confirmed_then_interfere),
    ):
        if stale:
            assert not await adapter.async_apply(Target(profile="high"), lambda: current)
        else:
            with pytest.raises(HomeAssistantError, match="dead time"):
                await adapter.async_apply(Target(profile="high"), lambda: current)
        assert [item.args[1] for item in call.await_args_list] == ["turn_off"] * 3


async def test_relay_final_dispatch_remains_recorded_when_generation_changes(hass, relay_config):
    set_relays(hass, relay_config)
    adapter = RelayFanAdapter(hass, "r", relay_config)
    current = True

    async def command(domain, service, data, **kwargs):
        nonlocal current
        if service == "turn_on":
            current = False

    with patch.object(type(hass.services), "async_call", side_effect=command):
        assert await adapter.async_apply(Target(profile="high"), lambda: current)


async def test_relay_wait_cancellation_unsubscribes(hass, relay_config):
    set_relays(hass, relay_config, "reverse")
    relay_config["movement_timeout"] = 10
    adapter = RelayFanAdapter(hass, "r", relay_config)
    before = hass.bus.async_listeners().copy()
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock):
        task = asyncio.create_task(adapter.async_apply(Target(profile="high"), lambda: True))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert hass.bus.async_listeners() == before


async def test_factory_and_base_contract(hass, relay_config):
    for kind, cls in [("cover", CoverAdapter), ("fan", FanAdapter), ("switch", SwitchAdapter)]:
        assert isinstance(create_adapter(hass, "r", {"kind": kind}), cls)
    assert isinstance(create_adapter(hass, "r", relay_config), RelayFanAdapter)
    with pytest.raises(ValueError):
        create_adapter(hass, "r", {"kind": "unsupported"})
    base = Adapter(hass, "r", {})
    with pytest.raises(NotImplementedError):
        base.read_observation(0)
    with pytest.raises(NotImplementedError):
        base.normalize(Target(on=False))
    with pytest.raises(NotImplementedError):
        await base.async_apply(Target(on=False), lambda: True)


async def test_native_fan_speed_count_and_direction_preserve_running_speed(hass):
    set_fan(hass, "on", percentage=100, direction="forward")
    adapter = fan(hass, default_target={"percentage": 50, "direction": "reverse"})
    assert adapter.speed_count == 2
    assert adapter.normalize(Target(direction="reverse")).percentage == 100
    assert adapter.normalize(Target(on=True)).direction == "reverse"
    assert adapter.normalize(Target(on=False)).percentage is None
    set_fan(hass, percentage_step="bad")
    assert adapter.speed_count == 1
    set_fan(hass, supported_features=0)
    assert adapter.speed_count == 1
    with pytest.raises(ValueError, match="turning on"):
        adapter.normalize(Target(on=True))
    with pytest.raises(ValueError, match="turning off"):
        adapter.normalize(Target(on=False))


async def test_fan_speed_only_capability_uses_set_percentage(hass):
    set_fan(hass, supported_features=int(FanEntityFeature.SET_SPEED))
    adapter = fan(hass, default_target={"percentage": 50})
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        await adapter.async_apply(Target(on=True), lambda: True)
        assert call.await_args.args == (
            "fan",
            "set_percentage",
            {"entity_id": "fan.raw", "percentage": 50},
        )
        await adapter.async_apply(Target(on=False), lambda: True)
        assert call.await_args.args == (
            "fan",
            "set_percentage",
            {"entity_id": "fan.raw", "percentage": 0},
        )
    with pytest.raises(ValueError, match="default direction"):
        fan(hass, default_target={"percentage": 50, "direction": "reverse"}).normalize(
            Target(on=True)
        )


async def test_relay_bare_on_uses_configured_profile_after_reverse(hass, relay_config):
    set_relays(hass, relay_config, "reverse")
    adapter = RelayFanAdapter(hass, "r", relay_config)
    assert adapter.normalize(Target(on=True)).profile == "low"
    assert not adapter.supports_stop


async def test_relay_stale_during_long_deadtime_is_cancelled_promptly(hass, relay_config):
    relay_config["reversal_dead_time"] = 10
    set_relays(hass, relay_config)
    adapter = RelayFanAdapter(hass, "r", relay_config)
    current = True

    def invalidate():
        nonlocal current
        current = False

    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as call:
        asyncio.get_running_loop().call_later(0.005, invalidate)
        async with asyncio.timeout(0.3):
            assert not await adapter.async_apply(Target(profile="high"), lambda: current)
        assert call.await_count == 3


async def test_relay_rechecks_conflicting_feedback_before_each_energize(hass, relay_config):
    relay_config["profiles"]["both"] = {
        "outputs": {"switch.low": True, "switch.high": True, "switch.reverse": False},
        "percentage": 100,
        "direction": "forward",
    }
    set_relays(hass, relay_config)
    adapter = RelayFanAdapter(hass, "r", relay_config)

    async def command(domain, service, data, **kwargs):
        if service == "turn_on":
            # Independent conflicting state change arrives after the first output.
            hass.states.async_set("switch.reverse", "on")

    with patch.object(type(hass.services), "async_call", side_effect=command) as call:
        with pytest.raises(HomeAssistantError, match="Conflicting"):
            await adapter.async_apply(Target(profile="both"), lambda: True)
        assert [
            item.args[2]["entity_id"] for item in call.await_args_list if item.args[1] == "turn_on"
        ] == ["switch.low"]


@pytest.mark.parametrize("unconfirmed", ["restored", "assumed_state"])
async def test_relay_all_off_unconfirmed_feedback_never_energizes_replacement(
    hass, relay_config, unconfirmed
):
    """All states saying off is insufficient when any channel disclaims feedback."""
    set_relays(hass, relay_config)
    hass.states.async_set("switch.high", "off", {unconfirmed: True})
    adapter = RelayFanAdapter(hass, "r", relay_config)
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as calls:
        with pytest.raises(HomeAssistantError, match="confirmed off"):
            await adapter.async_apply(Target(profile="reverse"), lambda: True)
        assert calls.await_count == len(relay_config["outputs"])
        assert {call.args[1] for call in calls.await_args_list} == {"turn_off"}
        assert adapter.read_observation(0).available is False


async def test_cover_sent_is_true_after_synchronous_physical_confirmation(hass):
    set_cover(hass)
    adapter = cover(hass)

    async def command(domain, service, data, **kwargs):
        set_cover(hass, "open", current_position=data["position"])

    with patch.object(type(hass.services), "async_call", side_effect=command) as calls:
        assert await adapter.async_apply(
            Target(position=100), lambda: adapter.read_observation(0).target.position != 100
        )
        assert calls.await_count == 1
        assert adapter.read_observation(0).target.position == 100


async def test_relay_off_sent_is_true_after_synchronous_physical_confirmation(hass, relay_config):
    set_relays(hass, relay_config, "reverse")
    adapter = RelayFanAdapter(hass, "r", relay_config)

    async def command(domain, service, data, **kwargs):
        hass.states.async_set(data["entity_id"], "off")

    with patch.object(type(hass.services), "async_call", side_effect=command) as calls:
        assert await adapter.async_apply(
            Target(on=False), lambda: adapter.read_observation(0).target.profile != "off"
        )
        assert calls.await_count == len(relay_config["outputs"])
        assert adapter.read_observation(0).target.profile == "off"


async def test_native_fan_stale_before_direction_dispatches_nothing(hass):
    set_fan(hass)
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as calls:
        assert not await fan(hass).async_apply(
            Target(percentage=100, direction="reverse"), lambda: False
        )
        calls.assert_not_awaited()


async def test_relay_stale_between_energized_outputs_skips_later_output(hass, relay_config):
    relay_config["profiles"]["both"] = {
        "outputs": {"switch.low": True, "switch.high": True, "switch.reverse": False},
        "percentage": 100,
        "direction": "forward",
    }
    set_relays(hass, relay_config)
    adapter = RelayFanAdapter(hass, "r", relay_config)
    current = True

    async def command(domain, service, data, **kwargs):
        nonlocal current
        if service == "turn_on":
            current = False
            hass.states.async_set(data["entity_id"], "on")

    with patch.object(type(hass.services), "async_call", side_effect=command) as calls:
        assert not await adapter.async_apply(Target(profile="both"), lambda: current)
        assert [
            call.args[2]["entity_id"] for call in calls.await_args_list if call.args[1] == "turn_on"
        ] == ["switch.low"]


async def test_native_stop_drains_threaded_transport_after_repeated_cancellation(hass):
    import threading

    set_cover(hass)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def physical_stop(call):
        started.set()
        release.wait(timeout=3)
        finished.set()

    hass.services.async_register("cover", "stop_cover", physical_stop)
    stopping = asyncio.create_task(cover(hass).async_stop())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        stopping.cancel()
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 1)
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_dispatch_audit_matches_native_service_context_and_keeps_feedback_separate(hass):
    set_cover(hass)
    adapter = cover(hass)
    audit, received, service_events = [], [], []
    adapter.audit_callback = audit.append

    async def command(call):
        received.append(call)

    hass.services.async_register("cover", "set_cover_position", command)
    hass.services.async_register("cover", "stop_cover", command)
    unsubscribe = hass.bus.async_listen("call_service", service_events.append)
    try:
        assert await adapter.async_apply(Target(position=72), lambda: True)
        await adapter.async_stop()
        await hass.async_block_till_done()
        assert [event["status"] for event in audit] == [
            "started",
            "completed",
            "started",
            "completed",
        ]
        assert [event["service"] for event in audit] == [
            "set_cover_position",
            "set_cover_position",
            "stop_cover",
            "stop_cover",
        ]
        assert audit[0]["data"] == {"entity_id": "cover.raw", "position": 72}
        assert audit[2]["data"] == {"entity_id": "cover.raw"}
        assert len({event["context_id"] for event in audit}) == 2
        assert [audit[0]["context_id"], audit[2]["context_id"]] == [
            call.context.id for call in received
        ]
        # Bus callbacks may be delivered after either awaited service returns.
        # Match both exact service/context pairs without imposing callback order.
        assert sorted(
            (event.data["service"], event.context.id) for event in service_events
        ) == sorted((call.service, call.context.id) for call in received)
        assert all(
            call.context.user_id is None and call.context.parent_id is None for call in received
        )
        assert all(
            event["resource_id"] == "resource" and event["domain"] == "cover" for event in audit
        )
        assert all(isinstance(event["at"], float) and event["at"] > 0 for event in audit)
        assert adapter.read_observation(0).target == Target(position=0)
        # An independently initiated legacy call receives no operator audit record.
        await hass.services.async_call(
            "cover", "set_cover_position", {"entity_id": "cover.raw", "position": 31}, blocking=True
        )
        assert len(audit) == 4
        assert received[-1].context.id not in {event["context_id"] for event in audit}
    finally:
        unsubscribe()


async def test_relay_dispatch_audit_includes_every_interlocked_substep(hass, relay_config):
    set_relays(hass, relay_config, "reverse")
    adapter = RelayFanAdapter(hass, "r", relay_config)
    audit, received = [], []
    adapter.audit_callback = audit.append

    async def command(call):
        received.append(call)
        if call.service == "turn_on":
            assert all(
                hass.states.get(entity_id).state == "off" for entity_id in relay_config["outputs"]
            )
        hass.states.async_set(call.data["entity_id"], "on" if call.service == "turn_on" else "off")

    hass.services.async_register("switch", "turn_off", command)
    hass.services.async_register("switch", "turn_on", command)
    assert await adapter.async_apply(Target(profile="high"), lambda: True)
    started = [event for event in audit if event["status"] == "started"]
    completed = [event for event in audit if event["status"] == "completed"]
    assert [(event["service"], event["data"]["entity_id"]) for event in started] == [
        ("turn_off", entity_id) for entity_id in relay_config["outputs"]
    ] + [("turn_on", "switch.high")]
    assert [event["context_id"] for event in started] == [call.context.id for call in received]
    assert [event["context_id"] for event in completed] == [call.context.id for call in received]
    assert len({call.context.id for call in received}) == 4


async def test_dispatch_audit_allowlist_is_independent_of_transport_payload(hass):
    adapter = Adapter(hass, "resource", {})
    events = []
    adapter.audit_callback = events.append
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as transport:
        await adapter._async_service(
            "fan",
            "turn_on",
            {
                "entity_id": "fan.raw",
                "percentage": 50,
                "direction": "forward",
                "position": float("nan"),
                "password": "secret-value",
                "arbitrary": {"private": "nested"},
            },
        )
        assert all(
            event["data"] == {"entity_id": "fan.raw", "percentage": 50, "direction": "forward"}
            for event in events
        )
        assert "secret-value" not in repr(events)
        events[0]["data"]["percentage"] = 99
        assert transport.await_args.args[2]["percentage"] == 50
        assert events[1]["data"]["percentage"] == 50


async def test_audit_failure_cannot_change_dispatch_and_warns_once_without_exception_text(
    hass, caplog
):
    set_cover(hass)
    adapter = cover(hass)
    invocations = []

    def broken_callback(event):
        invocations.append(event["status"])
        raise RuntimeError("private-credential")

    adapter.audit_callback = broken_callback
    with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as transport:
        assert await adapter.async_apply(Target(position=70), lambda: True)
        await adapter.async_stop()
        assert transport.await_count == 2
    assert invocations == ["started", "completed", "started", "completed"]
    assert caplog.text.count("Dispatch audit callback failed") == 1
    assert "private-credential" not in caplog.text


async def test_audit_dispatch_error_records_class_only_and_preserves_error(hass):
    set_cover(hass)
    adapter = cover(hass)
    events = []
    adapter.audit_callback = events.append
    with (
        patch.object(
            type(hass.services),
            "async_call",
            side_effect=HomeAssistantError("private-device-address"),
        ),
        pytest.raises(HomeAssistantError, match="private-device-address"),
    ):
        await adapter.async_apply(Target(position=70), lambda: True)
    assert [event["status"] for event in events] == ["started", "error"]
    assert events[-1]["error_type"] == "HomeAssistantError"
    assert events[0]["context_id"] == events[-1]["context_id"]
    assert "private-device-address" not in repr(events)


async def test_audit_service_cancellation_is_distinct_from_caller_cancellation(hass):
    set_cover(hass)
    adapter = cover(hass)
    events = []
    adapter.audit_callback = events.append

    async def cancelled_service(*args, **kwargs):
        raise asyncio.CancelledError

    with (
        patch.object(type(hass.services), "async_call", side_effect=cancelled_service),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter.async_apply(Target(position=70), lambda: True)
    assert [event["status"] for event in events] == ["started", "cancelled"]
    assert events[-1]["cancellation_scope"] == "service"


async def test_audit_caller_cancellation_records_completed_io_before_cancellation(hass):
    set_cover(hass)
    adapter = cover(hass)
    events = []
    adapter.audit_callback = events.append
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_service(call):
        entered.set()
        await release.wait()
        set_cover(hass, "open", current_position=70)

    hass.services.async_register("cover", "set_cover_position", delayed_service)
    applying = asyncio.create_task(adapter.async_apply(Target(position=70), lambda: True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        applying.cancel()
        await asyncio.sleep(0)
        applying.cancel()
        await asyncio.sleep(0)
        assert not applying.done()
        assert [event["status"] for event in events] == ["started"]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await applying
        assert [event["status"] for event in events] == ["started", "completed", "cancelled"]
        assert events[-1]["cancellation_scope"] == "caller"
        assert len({event["context_id"] for event in events}) == 1
        assert adapter.read_observation(0).target == Target(position=70)
    finally:
        release.set()
        await asyncio.gather(applying, return_exceptions=True)


async def test_stale_or_restricted_dispatch_has_no_physical_audit_record(hass):
    set_cover(hass)
    adapter = cover(hass, restriction_entity="binary_sensor.rain")
    events = []
    adapter.audit_callback = events.append
    assert not await adapter.async_apply(Target(position=70), lambda: False)
    with pytest.raises(HomeAssistantError):
        await adapter.async_apply(Target(position=70), lambda: True)
    assert events == []
