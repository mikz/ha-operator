"""Exercise desired controls, follower permissions and migration through native HA."""

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.components.group.switch import SwitchGroup
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import Context, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import WriteError
from homeassistant.setup import async_setup_component

from custom_components.ha_operator.const import DOMAIN

from .helpers import managed_id, operator_entry


async def setup(hass, tmp_path, *, manual=False):
    hass.config.config_dir = str(tmp_path)
    calls = []

    class RawSwitch(SwitchEntity):
        _attr_should_poll = False
        _attr_is_on = False

        def __init__(self, key):
            self.entity_id = f"switch.raw_{key}"
            self._attr_unique_id = f"raw_{key}"

        async def async_turn_on(self, **kwargs):
            calls.append((self.entity_id, "turn_on"))
            self._attr_is_on = True
            self.async_write_ha_state()

        async def async_turn_off(self, **kwargs):
            calls.append((self.entity_id, "turn_off"))
            self._attr_is_on = False
            self.async_write_ha_state()

    assert await async_setup_component(hass, "switch", {})
    await hass.data["switch"].async_add_entities([RawSwitch(key) for key in ("central", "room")])
    entry = operator_entry(
        resources={
            key: {
                "name": f"{key} follower",
                "kind": "switch",
                "entity_id": f"switch.raw_{key}",
                "manual_control": manual,
                "command_interval": 0.001,
            }
            for key in ("central", "room")
        },
        intents={
            "global_mode": {
                "name": "Central sleep",
                "initial_value": False,
                "on_targets": ["room_mode"],
            },
            "room_mode": {"name": "Room sleep", "initial_value": False},
        },
        policies={
            f"follow_{key}": {
                "name": f"Follow {key}",
                "kind": "state",
                "resource_id": key,
                "intent_id": "global_mode" if key == "central" else "room_mode",
            }
            for key in ("central", "room")
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, calls


async def command(hass, key, on):
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": managed_id(hass, "switch", key, "desired")},
        blocking=True,
        context=Context(),
    )
    await hass.async_block_till_done()


def saved(entry, tmp_path):
    return deepcopy(entry.runtime_data._state)


async def test_native_desired_controls_and_followers(hass, tmp_path):
    entry, calls = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    for key in runtime.resources:
        for domain, suffix in (
            ("switch", "managed"),
            ("button", "release"),
            ("binary_sensor", "manual"),
            ("sensor", "expiry"),
        ):
            assert registry.async_get_entity_id(domain, DOMAIN, f"{key}_{suffix}") is None
        assert managed_id(hass, "sensor", key, "observed")
        await runtime.async_set_mode(key, "live")
    await command(hass, "global_mode", True)
    assert saved(entry, tmp_path)["intents"] == {"global_mode": True, "room_mode": True}
    assert runtime.observations["room"].target.on is True
    assert set(calls) == {("switch.raw_central", "turn_on"), ("switch.raw_room", "turn_on")}
    desired = hass.states.get(managed_id(hass, "sensor", "room", "desired"))
    assert desired.attributes["source_kind"] == "intent"
    assert desired.attributes["related_entities"] == (
        managed_id(hass, "switch", "room_mode", "desired"),
    )
    await command(hass, "room_mode", False)
    unchanged = saved(entry, tmp_path)
    await command(hass, "global_mode", True)
    assert saved(entry, tmp_path) == unchanged  # repeated ON does not change held intent
    assert runtime.desired_value("room_mode") is False
    await command(hass, "global_mode", False)
    assert runtime.desired_value("room_mode") is False
    await command(hass, "global_mode", True)
    assert runtime.desired_value("room_mode") is True
    await command(hass, "global_mode", False)
    assert runtime.desired_value("room_mode") is True
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("action", ["target", "hands_off", "release", "stop"])
async def test_follower_manual_admission_is_rejected(hass, tmp_path, action):
    entry, calls = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    await runtime.async_set_mode("room", "live")
    before = saved(entry, tmp_path)
    with pytest.raises(ServiceValidationError, match="manual control is disabled"):
        if action in {"target", "hands_off"}:
            await hass.services.async_call(
                DOMAIN,
                "request",
                {
                    "resource_id": "room",
                    "mode": action,
                    **({"target": {"on": True}} if action == "target" else {}),
                },
                blocking=True,
            )
        else:
            await getattr(runtime, f"async_{action}")("room")
    assert saved(entry, tmp_path) == before
    assert calls == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_raw_feedback_never_changes_desired_intent(hass, tmp_path):
    entry, calls = await setup(hass, tmp_path)
    before = saved(entry, tmp_path)
    for index in range(100):
        hass.states.async_set("switch.raw_room", "on" if index % 2 else "off")
    await hass.async_block_till_done()
    assert saved(entry, tmp_path) == before
    assert entry.runtime_data.desired_value("room_mode") is False
    assert calls == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_public_group_routes_only_to_desired_source(hass, tmp_path):
    entry, calls = await setup(hass, tmp_path)
    desired = managed_id(hass, "switch", "global_mode", "desired")
    group = SwitchGroup("public_sleep", "Public sleep", [desired], False)
    group.entity_id = "switch.public_sleep"
    await hass.data["switch"].async_add_entities([group])
    for key in entry.runtime_data.resources:
        await entry.runtime_data.async_set_mode(key, "live")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": group.entity_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert saved(entry, tmp_path)["intents"] == {"global_mode": True, "room_mode": True}
    assert set(calls) == {("switch.raw_central", "turn_on"), ("switch.raw_room", "turn_on")}
    assert hass.states.get(group.entity_id).state == "on"
    await command(hass, "room_mode", False)
    assert hass.states.get(group.entity_id).state == "on"
    assert saved(entry, tmp_path)["intents"]["room_mode"] is False
    await hass.config_entries.async_unload(entry.entry_id)


async def test_desired_controls_actuate_despite_native_save_failure(hass, tmp_path):
    entry, calls = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    for key in runtime.resources:
        await runtime.async_set_mode(key, "live")
    with patch("homeassistant.helpers.storage.write_utf8_file", side_effect=WriteError("failed")):
        await command(hass, "global_mode", True)
        await runtime._store.async_save(runtime._state)
    assert runtime.fault is None and runtime.desired_value("global_mode") is True
    assert set(calls) == {("switch.raw_central", "turn_on"), ("switch.raw_room", "turn_on")}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_concurrent_desired_commands_keep_loop_order_and_reload_without_edge(hass, tmp_path):
    entry, _ = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    await asyncio.gather(
        runtime.async_set_desired("global_mode", True),
        runtime.async_set_desired("room_mode", False),
    )
    assert saved(entry, tmp_path)["intents"] == {"global_mode": True, "room_mode": False}
    assert await hass.config_entries.async_reload(entry.entry_id)
    runtime = entry.runtime_data
    assert runtime.desired_value("global_mode") is True
    assert runtime.desired_value("room_mode") is False
    await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(HomeAssistantError, match="unloaded"):
        await runtime.async_set_desired("room_mode", True)


async def test_coupled_context_and_renamed_source_links(hass, tmp_path):
    entry, _ = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    for key in runtime.resources:
        await runtime.async_set_mode(key, "live")
    dispatched = []

    @callback
    def observe(event):
        if event.data.get("service_data", {}).get("entity_id") in {
            "switch.raw_central",
            "switch.raw_room",
        }:
            dispatched.append(event.context.parent_id)

    unsubscribe = hass.bus.async_listen("call_service", observe)
    context = Context()
    source = managed_id(hass, "switch", "global_mode", "desired")
    room = managed_id(hass, "switch", "room_mode", "desired")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": source}, blocking=True, context=context
    )
    await hass.async_block_till_done()
    assert hass.states.get(source).context.id == context.id
    assert hass.states.get(room).context.id == context.id
    assert dispatched == [context.id, context.id]
    registry = er.async_get(hass)
    registry.async_update_entity(room, new_entity_id="switch.renamed_room_desired")
    await hass.async_block_till_done()
    for suffix in ("desired", "reason"):
        state = hass.states.get(managed_id(hass, "sensor", "room", suffix))
        assert state.attributes["related_entities"] == ("switch.renamed_room_desired",)
    assert runtime.explain("room")["resources"]["room"]["selection"]["related_entities"] == (
        "switch.renamed_room_desired",
    )
    unsubscribe()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_manual_to_follower_conversion_retires_old_controls(hass, tmp_path):
    entry, _ = await setup(hass, tmp_path, manual=True)
    runtime = entry.runtime_data
    registry = er.async_get(hass)
    command_id = managed_id(hass, "switch", "room", "managed")
    registry.async_update_entity(command_id, name="Preserved customization")
    expiry_id = managed_id(hass, "sensor", "room", "expiry")
    registry.async_update_entity(expiry_id, disabled_by=er.RegistryEntryDisabler.USER)
    await runtime.async_set_mode("room", "live")
    await runtime.async_request("room", target={"on": True}, indefinite=True)
    await runtime.async_set_mode("room", "observe")
    await hass.config_entries.async_unload(entry.entry_id)
    resource = entry.subentries["room"]
    hass.config_entries.async_update_subentry(
        entry, resource, data={**resource.data, "manual_control": False}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert "room" not in saved(entry, tmp_path)["manuals"]
    assert hass.states.get(command_id) is None
    assert registry.async_get(command_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registry.async_get(expiry_id).disabled_by is er.RegistryEntryDisabler.USER
    await hass.config_entries.async_unload(entry.entry_id)
    resource = entry.subentries["room"]
    hass.config_entries.async_update_subentry(
        entry, resource, data={**resource.data, "manual_control": True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(command_id).disabled_by is None
    assert registry.async_get(command_id).name == "Preserved customization"
    assert hass.states.get(command_id) is not None
    assert registry.async_get(expiry_id).disabled_by is er.RegistryEntryDisabler.USER
    assert entry.runtime_data.manual("room") is None
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(("key", "value"), [("missing", True), ("global_mode", 1)])
async def test_invalid_desired_command_preserves_current_pair(hass, tmp_path, key, value):
    entry, _ = await setup(hass, tmp_path)
    before = deepcopy(entry.runtime_data._state)
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.async_set_desired(key, value)
    assert entry.runtime_data._state == before
    assert await hass.config_entries.async_unload(entry.entry_id)
