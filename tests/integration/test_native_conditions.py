"""Exercise real HA condition checkers without actuators or production services."""

import asyncio
import json
from contextvars import ContextVar
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers.trace import trace_get, trace_path

from custom_components.ha_operator.native_conditions import (
    NativeConditionList,
)


async def native(hass, configs):
    checker = NativeConditionList(hass, configs)
    await checker.async_setup()
    return checker


async def test_native_state_tracking_and_cleanup(hass):
    hass.states.async_set("binary_sensor.ready", "off")
    checker = await native(
        hass, [{"condition": "state", "entity_id": "binary_sensor.ready", "state": "on"}]
    )
    action = Mock()
    await checker.async_track_changes(action)
    assert not checker.needs_polling
    assert checker.evaluate().status == "failed"
    hass.states.async_set("binary_sensor.ready", "on")
    await hass.async_block_till_done()
    assert action.call_count == 1
    result = checker.evaluate()
    assert result.status == "passed"
    assert result.conditions[0]["sources"]["binary_sensor.ready"]["state"] == "on"
    checker.async_unload()
    hass.states.async_set("binary_sensor.ready", "off")
    await hass.async_block_till_done()
    assert action.call_count == 1
    assert checker.evaluate().status == "unknown"
    assert result.conditions[0]["sources"]["binary_sensor.ready"]["state"] == "on"


async def test_native_or_handles_missing_branch_and_reports_referenced_inputs(hass):
    hass.states.async_set("binary_sensor.optional", "off")
    checker = await native(
        hass,
        [
            {
                "condition": "or",
                "conditions": [
                    {"condition": "state", "entity_id": "binary_sensor.optional", "state": "off"},
                    {"condition": "numeric_state", "entity_id": "sensor.rate", "above": 1},
                ],
            }
        ],
    )
    result = checker.evaluate()
    assert result.status == "passed"
    assert result.conditions[0]["sources"]["sensor.rate"]["state"] is None
    assert not result.conditions[0]["sources"]["sensor.rate"]["available"]
    checker.async_unload()


async def test_nested_attribute_sources_are_factual_bounded_snapshots(hass):
    hass.states.async_set(
        "sensor.stream",
        "on",
        {"rate": 12, "friendly_name": "X" * 200, "unit_of_measurement": "fps", "private": "omit"},
    )
    checker = await native(
        hass,
        [
            {
                "condition": "or",
                "conditions": [
                    {
                        "condition": "numeric_state",
                        "entity_id": "sensor.stream",
                        "attribute": "rate",
                        "above": 5,
                    },
                    {"condition": "state", "entity_id": "binary_sensor.optional", "state": "off"},
                ],
            }
        ],
    )
    result = checker.evaluate()
    assert result.status == "passed"
    source = result.conditions[0]["sources"]["sensor.stream"]
    assert source["attributes"] == {"rate": 12}
    assert source["unit"] == "fps"
    assert len(source["name"]) == 128
    assert source["updated_at"] == hass.states.get("sensor.stream").last_updated.isoformat()
    hass.states.async_set("sensor.stream", "on", {"rate": 1})
    assert source["attributes"] == {"rate": 12}
    hass.states.async_set("sensor.stream", "on", {"rate": 12, "friendly_name": 123})
    numeric_name = checker.evaluate()
    assert numeric_name.status == "passed"
    assert numeric_name.conditions[0]["sources"]["sensor.stream"]["name"] == "sensor.stream"
    checker.async_unload()


async def test_native_numeric_error_is_unknown_and_known_failed_and_member_wins(hass):
    hass.states.async_set("sensor.rate", "unavailable")
    configs = [{"condition": "numeric_state", "entity_id": "sensor.rate", "above": 1}]
    checker = await native(hass, configs)
    result = checker.evaluate()
    assert result.status == "unknown"
    assert result.conditions[0]["error"] is None
    assert not result.conditions[0]["sources"]["sensor.rate"]["available"]
    assert not result.permits_command
    checker.async_unload()
    checker = await native(
        hass, [*configs, {"condition": "template", "value_template": "{{ false }}"}]
    )
    assert checker.evaluate().status == "failed"
    checker.async_unload()


@pytest.mark.parametrize(
    "configs",
    [
        [],
        [{"condition": "template", "value_template": "{{ true }}", "enabled": False}],
        [
            {
                "condition": "and",
                "conditions": [
                    {"condition": "template", "value_template": "{{ true }}", "enabled": False}
                ],
            }
        ],
        [
            {
                "condition": "and",
                "conditions": [
                    {
                        "condition": "template",
                        "value_template": "{{ true }}",
                        "enabled": "{{ false }}",
                    }
                ],
            }
        ],
    ],
)
async def test_empty_and_disabled_native_conditions_permit_command(hass, configs):
    before = json.dumps(configs)
    checker = await native(hass, configs)
    result = checker.evaluate()
    assert result.status in {"skipped", "passed"}
    assert result.permits_command
    assert json.dumps(configs) == before
    assert json.dumps(checker.configs) == before
    checker.async_unload()


@pytest.mark.parametrize(
    "children", [[], [{"condition": "template", "value_template": "{{ false }}", "enabled": False}]]
)
async def test_enabled_native_or_false_blocks_even_without_enabled_leaves(hass, children):
    checker = await native(hass, [{"condition": "or", "conditions": children}])
    assert checker._checkers[0].async_check() is False
    result = checker.evaluate()
    assert result.status == "failed"
    assert not result.permits_command
    checker.async_unload()


@pytest.mark.parametrize("state", [None, "unavailable", "on", "off"])
@pytest.mark.parametrize("behavior", ["any", "all"])
async def test_native_switch_target_behavior_preserves_ha_truth_and_source_evidence(
    hass, state, behavior
):
    if state is not None:
        hass.states.async_set("switch.permit", state)
    checker = await native(
        hass,
        [
            {
                "condition": "switch.is_on",
                "target": {"entity_id": "switch.permit"},
                "options": {"behavior": behavior},
            }
        ],
    )
    assert checker.setup_error is None
    expected = state == "on" or (behavior == "all" and state in {None, "unavailable"})
    assert checker._checkers[0].async_check() is expected
    result = checker.evaluate()
    assert result.permits_command is expected
    assert result.status == ("passed" if expected else "failed")
    source = result.conditions[0]["sources"]["switch.permit"]
    assert source["state"] == state
    assert source["available"] is (state not in {None, "unavailable"})
    assert result.conditions[0]["source_scope"] == "native_static_references"
    checker.async_unload()


async def test_template_polling_and_caller_context_isolation(hass):
    marker = ContextVar("test_marker", default="isolated")
    marker.set("caller")
    checker = await native(hass, [{"condition": "template", "value_template": "{{ true }}"}])
    assert checker.needs_polling
    with trace_path("caller"):
        original = trace_get(clear=False)
        result = checker.evaluate()
        assert trace_get(clear=False) is original
    assert result.status == "passed"
    assert result.conditions[0]["source_scope"] == "native_static_references"
    with patch.object(
        checker._checkers[0], "_async_check", side_effect=lambda **_: marker.get() == "isolated"
    ):
        assert checker.evaluate().status == "passed"
    checker.async_unload()


async def test_compile_failure_and_uninitialized_list_fail_closed(hass):
    checker = NativeConditionList(hass, [{"condition": "not_a_native_condition"}])
    assert checker.evaluate().status == "unknown"
    await checker.async_setup()
    assert checker.setup_error
    assert checker.evaluate().status == "unknown"
    assert not checker.evaluate().permits_command
    checker.async_unload()


async def test_native_template_evaluation_error_remains_explicit(hass):
    checker = await native(hass, [{"condition": "template", "value_template": "{{ 1 / 0 }}"}])
    result = checker.evaluate()
    assert result.status == "unknown"
    assert "ConditionError" in result.conditions[0]["error"]
    checker.async_unload()


async def test_partial_setup_failure_unloads_compiled_checker(hass):
    checker = NativeConditionList(
        hass, [{"condition": "template", "value_template": "{{ true }}"}] * 2
    )
    from homeassistant.helpers import condition

    compile_native = condition.async_from_config
    first = None

    async def compile_one(hass, config):
        nonlocal first
        if first is not None:
            raise ValueError("compile failed")
        first = await compile_native(hass, config)
        return first

    with patch.object(condition, "async_from_config", side_effect=compile_one):
        await checker.async_setup()
    assert first._unloaded
    assert checker.evaluate().status == "unknown"
    assert "compile failed" in checker.setup_error
    checker.async_unload()


async def test_setup_cancellation_and_tracking_failure_release_resources(hass):
    checker = NativeConditionList(hass, [{"condition": "template", "value_template": "{{ true }}"}])
    with patch(
        "homeassistant.helpers.condition.async_validate_condition_config",
        side_effect=asyncio.CancelledError,
    ):
        with pytest.raises(asyncio.CancelledError):
            await checker.async_setup()
    assert checker.evaluate().status == "unknown"
    await checker.async_setup()
    compiled = checker._checkers[0]
    with patch.object(
        compiled, "async_track_changes", new=AsyncMock(side_effect=RuntimeError("track failed"))
    ):
        with pytest.raises(RuntimeError, match="track failed"):
            await checker.async_track_changes(Mock())
    assert compiled._unloaded
    assert checker.evaluate().status == "unknown"
