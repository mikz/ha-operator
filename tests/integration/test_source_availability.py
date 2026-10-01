"""Source availability is presentation state, never physical confirmation."""

from __future__ import annotations

import logging
from copy import deepcopy

import pytest
from homeassistant.components.fan import FanEntityFeature
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.ha_operator import adapters
from custom_components.ha_operator.const import DOMAIN

from .helpers import (
    async_activate,
    async_add_physical_cover,
    async_setup_operator,
    managed_id,
    operator_entry,
)


@pytest.mark.parametrize("kind", ["cover", "switch", "fan", "relay_fan"])
@pytest.mark.parametrize("source_state", ["unknown", "unavailable", None, "invalid"])
async def test_native_observation_entities_distinguish_unknown_and_unavailable(
    hass, tmp_path, kind, source_state
):
    source = "cover.raw" if kind == "cover" else "fan.raw" if kind == "fan" else "switch.raw"
    config = {"name": "Output", "kind": kind, "entity_id": source}
    if kind == "fan":
        config["default_target"] = {"on": True, "percentage": 50}
    if kind == "relay_fan":
        source = "switch.first"
        hass.states.async_set("switch.second", "off")
        config = {
            "name": "Output",
            "kind": kind,
            "outputs": [source, "switch.second"],
            "profiles": {
                "off": {"outputs": {source: False, "switch.second": False}},
                "on": {"outputs": {source: True, "switch.second": False}},
            },
            "default_target": {"profile": "on"},
            "reversal_dead_time": 1,
        }
    if source_state is not None:
        hass.states.async_set(source, source_state, {"supported_features": 53})
    entry = await async_setup_operator(hass, tmp_path, resources={"roof": config})
    reachable = source_state not in {None, "unavailable"}
    expected = "unknown" if reachable else "unavailable"
    domain = "fan" if kind == "relay_fan" else kind
    assert hass.states.get(managed_id(hass, domain)).state == expected
    assert hass.states.get(managed_id(hass, "sensor", key="observed")).state == expected
    assert entry.runtime_data.source_available("roof") is reachable
    assert entry.runtime_data.observations["roof"].target is None
    assert entry.runtime_data.manual("roof") is None
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not runtime.source_available("roof")


async def test_source_outage_logs_once_and_preserves_durable_intent(hass, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.ha_operator.runtime")
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    entry = await async_setup_operator(hass, tmp_path)
    runtime = entry.runtime_data
    await async_activate(hass)
    await runtime.async_request("roof", target={"position": 65}, duration=60)
    await hass.async_block_till_done()
    saved = deepcopy(runtime._state)
    lease = runtime.manual("roof")
    observed = managed_id(hass, "sensor", key="observed")
    desired = managed_id(hass, "sensor", key="desired")
    attrs = {"supported_features": 15}
    for _ in range(5):
        hass.states.async_set("cover.physical_roof", "unavailable", attrs)
        await hass.async_block_till_done()
        await runtime.async_reconcile()
    assert hass.states.get(observed).state == "unavailable"
    assert float(hass.states.get(desired).state) == 65
    assert deepcopy(runtime._state) == saved and runtime.manual("roof") == lease
    attempts = dict(runtime.next_attempts)
    for _ in range(5):
        hass.states.async_set("cover.physical_roof", "unknown", attrs)
        await hass.async_block_till_done()
        await runtime.async_reconcile()
    assert hass.states.get(observed).state == "unknown"
    assert hass.states.get(managed_id(hass, "cover")).state == "unknown"
    assert runtime.next_attempts == attempts
    assert deepcopy(runtime._state) == saved and runtime.manual("roof") == lease
    assert raw.commands == [("position", 65)]
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert messages.count("Resource roof source is unavailable") == 1
    assert messages.count("Resource roof source is available again") == 1
    # An unknown source is reachable, but cannot confirm or dispatch a target.
    assert runtime.observations["roof"].available is False
    raw.async_write_ha_state()
    await hass.async_block_till_done()
    assert float(hass.states.get(observed).state) == 0
    assert deepcopy(runtime._state) == saved
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_initial_missing_source_logs_one_loss_until_return(hass, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.ha_operator.runtime")
    entry = await async_setup_operator(hass, tmp_path)
    for _ in range(5):
        await entry.runtime_data.async_reconcile()
    hass.states.async_set("cover.physical_roof", "unknown")
    await hass.async_block_till_done()
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert messages.count("Resource roof source is unavailable") == 1
    assert messages.count("Resource roof source is available again") == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_source_presence_is_not_confirmation(hass):
    adapter = adapters.SwitchAdapter(hass, "plug", {"entity_id": "switch.plug"})
    assert not adapter.source_available
    hass.states.async_set("switch.plug", "unknown")
    assert adapter.source_available and adapter.read_observation(0).target is None


@pytest.mark.parametrize(
    "attributes",
    [
        {"restored": True, "current_position": 50},
        {"optimistic": True, "current_position": 50},
        {"assumed_state": True, "current_position": 50},
        {},
    ],
)
async def test_reachable_cover_without_usable_feedback_is_unknown(hass, tmp_path, attributes):
    hass.states.async_set("cover.physical_roof", "open", {"supported_features": 15, **attributes})
    entry = await async_setup_operator(hass, tmp_path)
    runtime = entry.runtime_data
    assert runtime.source_available("roof")
    assert runtime.observations["roof"].target is None
    assert hass.states.get(managed_id(hass, "cover")).state == "unknown"
    assert hass.states.get(managed_id(hass, "sensor", key="observed")).state == "unknown"
    assert not runtime.last_commands and not runtime.manual("roof")
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "case",
    [
        "cover_open",
        "cover_unknown",
        "fan",
        "confirmation",
        "policy_target",
    ],
)
async def test_native_oversized_feedback_clears_stale_state_and_recovers(hass, tmp_path, case):
    """Unusable raw scalars cannot abort setup or retain usable stale evidence."""
    raw = await async_add_physical_cover(hass)
    resources = requirements = policies = None
    source, state, field, valid = "cover.physical_roof", "open", "current_position", 30
    attrs = {"supported_features": 15, field: 10**399}
    if case == "cover_unknown":
        state = "unknown"
    elif case == "fan":
        assert await async_setup_component(hass, "fan", {})
        source, state, field, valid = "fan.raw", "on", "percentage", 50
        attrs = {
            "supported_features": int(
                FanEntityFeature.SET_SPEED
                | FanEntityFeature.DIRECTION
                | FanEntityFeature.TURN_ON
                | FanEntityFeature.TURN_OFF
            ),
            "percentage_step": 50,
            "direction": "forward",
            field: 10**399,
        }
        resources = {
            "roof": {
                "name": "Fan",
                "kind": "fan",
                "entity_id": source,
                "default_target": {"percentage": 50},
            }
        }
    elif case == "confirmation":
        source, state, field, valid = "sensor.raw_airflow", "ready", "airflow", 2
        attrs = {field: 10**399}
        hass.states.async_set("switch.extractor", "on")
        requirements = {
            "air": {
                "name": "Air",
                "activation_entities": ["switch.extractor"],
                "providers": [
                    {
                        "id": "passive",
                        "evidence": [
                            {
                                "entity_id": source,
                                "attribute": field,
                                "kind": "airflow",
                                "operator": "gte",
                                "value": 1,
                            }
                        ],
                    }
                ],
            }
        }
    elif case == "policy_target":
        source, state, field, valid = "schedule.day", "on", "position", 30
        attrs = {field: 10**399}
        policies = {
            "day": {
                "name": "Day",
                "kind": "state",
                "resource_id": "roof",
                "target_entity": source,
                "target_attribute": field,
                "target_field": field,
                "eligibility_entity": source,
            }
        }
    hass.states.async_set(source, state, attrs)
    outputs = []

    @callback
    def audit(event):
        if event.data.get("domain") in {"cover", "fan", "switch"}:
            outputs.append(event.data)

    remove_audit = hass.bus.async_listen("call_service", audit)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(resources=resources, requirements=requirements, policies=policies)
    entry.add_to_hass(hass)
    runtime = None
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        saved = deepcopy(runtime._state)
        # Initial malformed setup and valid -> malformed -> valid state events.
        for value in (10**399, valid, 10**399, valid):
            usable = value == valid
            hass.states.async_set(
                source,
                "open" if case.startswith("cover") and usable else state,
                {**attrs, field: value},
            )
            await hass.async_block_till_done()
            managed = hass.states.get(managed_id(hass, "fan" if case == "fan" else "cover"))
            observed = hass.states.get(managed_id(hass, "sensor", key="observed"))
            if case.startswith("cover"):
                assert managed.state == ("open" if usable else "unknown")
                assert managed.attributes.get("current_position") == (30 if usable else None)
                assert observed.state == ("30.0" if usable else "unknown")
                assert runtime.source_available("roof") is True
            elif case == "fan":
                assert managed.state == "on"
                assert managed.attributes.get("percentage") == (50 if usable else None)
                assert observed.state == ("50" if usable else "on")
                assert observed.attributes["target"]["on"] is True
                assert observed.attributes["target"].get("percentage") == (50 if usable else None)
            else:
                assert managed.state == "closed" and observed.state == "0.0"
                if case == "confirmation":
                    detail = runtime._explain_requirements()["air"]
                    assert detail["providers"]["passive"]["confirmed"] is (True if usable else None)
                    assert (runtime.requirement_results["air"].status == "satisfied") is usable
                    assert (
                        hass.states.get(managed_id(hass, "sensor", "air", "status")).state
                        == runtime.requirement_results["air"].status
                    )
                    assert hass.states.get("switch.extractor").state == "on"
                else:
                    desired = hass.states.get(managed_id(hass, "sensor", key="desired"))
                    assert desired.state == ("30.0" if usable else "unknown")
                    assert (runtime.decisions["roof"].target is not None) is usable
            assert deepcopy(runtime._state) == saved
            assert runtime.fault is None
            assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is None
            assert outputs == raw.commands == []
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        remove_audit()
        if runtime is not None:
            assert runtime._closed and all(task.done() for task in runtime._tasks)
            assert not runtime._listeners and not runtime._timers and not runtime._unsubscribers
            assert runtime._deadline_timer is None
        assert outputs == raw.commands == []
