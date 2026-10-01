"""Fan commands compose current intent while independent feedback remains unchanged."""

from copy import deepcopy

# ruff: noqa: F811 - pytest fixtures are injected by name
import pytest

from custom_components.ha_operator.fan import OperatorFan
from custom_components.ha_operator.runtime import OperatorRuntime, _validate_saved_state
from tests.integration.test_runtime_reconciliation import runtime_factory  # noqa: F401
from tests.integration.test_runtime_requests import (
    clock,  # noqa: F401
    current_state,
)


@pytest.fixture(params=["fan", "relay_fan"])
async def fan_runtime(request, hass, runtime_factory, clock):
    hass.states.async_set("binary_sensor.guard", "on")
    config = {
        "name": "Test fan",
        "kind": request.param,
        "restriction_entity": "binary_sensor.guard",
        "manual_duration": 1800,
    }
    if request.param == "fan":
        hass.states.async_set(
            "fan.raw", "off", {"supported_features": 53, "percentage": 0, "direction": "forward"}
        )
        config.update(
            entity_id="fan.raw",
            default_target={"on": True, "percentage": 100, "direction": "forward"},
        )
    else:
        outputs = ["switch.power", "switch.inward", "switch.outward"]
        for output in outputs:
            hass.states.async_set(output, "off")
        config.update(
            outputs=outputs,
            profiles={
                "off": {"outputs": dict.fromkeys(outputs, False), "percentage": 0},
                "forward": {
                    "outputs": dict(zip(outputs, [True, True, False], strict=True)),
                    "percentage": 100,
                    "direction": "forward",
                },
                "reverse": {
                    "outputs": dict(zip(outputs, [True, False, True], strict=True)),
                    "percentage": 100,
                    "direction": "reverse",
                },
            },
            default_target={"profile": "forward"},
            reversal_dead_time=0.001,
        )
    factory, _ = runtime_factory
    runtime = await factory(resources={"vent": config})
    await runtime.async_set_mode("vent", "live")
    return runtime, OperatorFan(runtime, "vent")


@pytest.mark.parametrize("direction_first", [False, True])
async def test_on_and_direction_compose_before_raw_feedback(fan_runtime, direction_first):
    runtime, fan = fan_runtime
    if direction_first:
        await fan.async_set_direction("reverse")
        assert runtime.manual("vent").target.on is False
        await fan.async_turn_on()
    else:
        await fan.async_turn_on()
        await fan.async_set_direction("reverse")
    target = runtime.manual("vent").target
    assert target.on is True
    assert target.direction == "reverse"
    assert target.percentage == 100
    assert current_state(runtime)["manuals"]["vent"]["target"] == target.to_dict()
    assert fan.is_on is False
    assert runtime.attempts == {}


async def test_off_then_direction_never_restarts_pending_fan(fan_runtime):
    runtime, fan = fan_runtime
    await fan.async_turn_on()
    await fan.async_turn_off()
    await fan.async_set_direction("reverse")
    assert runtime.manual("vent").target.on is False
    assert fan.is_on is False
    await fan.async_set_percentage(100)
    assert runtime.manual("vent").target.on is True
    assert runtime.manual("vent").target.direction == "reverse"


async def test_expired_or_released_direction_is_not_inherited(fan_runtime, clock):
    runtime, fan = fan_runtime
    await fan.async_set_direction("reverse")
    clock[0] += 1801
    await fan.async_turn_on()
    assert runtime.manual("vent").target.direction == "forward"
    await fan.async_set_direction("reverse")
    await runtime.async_release("vent")
    await fan.async_turn_on()
    assert runtime.manual("vent").target.direction == "forward"


async def test_off_direction_survives_restart_as_intent_only(fan_runtime):
    runtime, fan = fan_runtime
    await fan.async_set_direction("reverse")
    await runtime.async_close()
    restored = OperatorRuntime(runtime.hass, runtime.entry)
    try:
        await restored.async_start()
        assert restored.fault is None
        assert restored.manual("vent").target.on is False
        assert restored.observations["vent"].target.on is False
        await OperatorFan(restored, "vent").async_turn_on()
        assert restored.manual("vent").target.direction == "reverse"
    finally:
        await restored.async_close()


async def test_partial_request_id_retry_does_not_recompose(fan_runtime):
    runtime, _ = fan_runtime
    await runtime.async_request("vent", target={"on": True})
    original = await runtime.async_request(
        "vent", target={"direction": "reverse"}, request_id="direction"
    )
    await runtime.async_request("vent", target={"on": False})
    retried = await runtime.async_request(
        "vent", target={"direction": "reverse"}, request_id="direction"
    )
    assert retried == original
    assert runtime.manual("vent").target.on is False


@pytest.mark.parametrize(
    "target", [{}, {"position": 10}, {"direction": "invalid"}, {"on": False, "percentage": 100}]
)
async def test_invalid_fan_command_never_changes_accepted_intent(fan_runtime, target):
    from homeassistant.exceptions import ServiceValidationError

    runtime, fan = fan_runtime
    await fan.async_turn_off()
    original = deepcopy(runtime._state)
    with pytest.raises(ServiceValidationError):
        await runtime.async_request("vent", target=target)
    assert deepcopy(runtime._state) == original


@pytest.mark.parametrize("settings", [{"profile": "reverse"}, {"direction": "sideways"}])
async def test_invalid_saved_fan_settings_are_rejected(fan_runtime, settings):
    runtime, fan = fan_runtime
    await fan.async_turn_off()
    state = current_state(runtime)
    state["manuals"]["vent"]["fan_settings"] = settings
    with pytest.raises(ValueError, match="Saved fan"):
        _validate_saved_state(state)
