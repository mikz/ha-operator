"""Exercise native HA flow managers, subentry identity, and reconfiguration."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator.config_flow import OperatorSubentryFlow
from custom_components.ha_operator.const import DOMAIN


@pytest.fixture
def native_sources(hass):
    hass.states.async_set("cover.raw", "closed", {"supported_features": 15, "current_position": 0})
    hass.states.async_set("binary_sensor.extraction", "on")
    hass.states.async_set("binary_sensor.inlet", "on")


async def _start(hass, entry, kind):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": "user"}
    )


async def _resource(hass, entry):
    flow = await _start(hass, entry, "resource")
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"name": "Skylight", "kind": "cover", "entity_id": "cover.raw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return next(item for item in entry.subentries.values() if item.subentry_type == "resource")


async def test_singleton_setup_and_abort(hass):
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_setup", new=AsyncMock(return_value=True)
    ):
        form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert form["type"] is FlowResultType.FORM
        created = await hass.config_entries.flow.async_configure(form["flow_id"], {})
        assert created["type"] is FlowResultType.CREATE_ENTRY
        assert created["title"] == "HA Operator"
        assert created["data"] == {}
        duplicate = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert duplicate["reason"] == "single_instance_allowed"


async def test_resource_errors_then_creation_and_reconfigure(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    invalid = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {"name": "Bad", "kind": "cover", "entity_id": "cover.missing"}
    )
    assert invalid["errors"] == {"base": "config_entity_not_found"}
    assert "cover.missing" in invalid["description_placeholders"]["value"]
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "name": "Window",
            "kind": "cover",
            "entity_id": "cover.raw",
            "restriction_entity": "binary_sensor.extraction",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    original = next(iter(entry.subentries.values()))
    assert original.data["retry_interval"] == 300
    assert original.data["manual_duration"] == 1800
    # A listener is allowed; reload is owned by the parent, not the flow.
    with patch.object(entry, "update_listeners", [AsyncMock()]):
        edit = await entry.start_subentry_reconfigure_flow(hass, original.subentry_id)
        assert edit["step_id"] == "reconfigure"
        error = await hass.config_entries.subentries.async_configure(
            edit["flow_id"], {"name": "Window", "kind": "cover", "entity_id": "cover.missing"}
        )
        assert error["errors"] == {"base": "config_entity_not_found"}
        changed = await hass.config_entries.subentries.async_configure(
            edit["flow_id"], {"name": "Renamed", "kind": "cover", "entity_id": "cover.raw"}
        )
        assert changed["reason"] == "reconfigure_successful"
    updated = entry.subentries[original.subentry_id]
    assert updated.title == "Renamed"
    assert "restriction_entity" not in updated.data
    duplicate = await _start(hass, entry, "resource")
    duplicate = await hass.config_entries.subentries.async_configure(
        duplicate["flow_id"], {"name": "Duplicate", "kind": "cover", "entity_id": "cover.raw"}
    )
    assert duplicate["errors"] == {"base": "config_an_output_is_already_owned_by_another_resource"}


async def test_native_check_ui_add_edit_remove_preserves_resource(hass, native_sources):
    from homeassistant.helpers import selector

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    fields = {key.schema: value for key, value in form["data_schema"].schema.items()}
    command = fields["command_conditions"]
    assert isinstance(
        command.config["fields"]["set_cover_position"]["selector"], selector.ConditionSelector
    )
    data = {
        "name": "Window",
        "kind": "cover",
        "entity_id": "cover.raw",
        "command_conditions": {
            "set_cover_position": [
                {"condition": "state", "entity_id": "binary_sensor.inlet", "state": "on"}
            ]
        },
    }
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    original = next(iter(entry.subentries.values()))
    edit = await entry.start_subentry_reconfigure_flow(hass, original.subentry_id)
    data["command_conditions"]["set_cover_position"][0]["state"] = "off"
    result = await hass.config_entries.subentries.async_configure(edit["flow_id"], data)
    assert result["reason"] == "reconfigure_successful"
    assert (
        entry.subentries[original.subentry_id].data["command_conditions"]["set_cover_position"][0][
            "state"
        ]
        == "off"
    )
    edit = await entry.start_subentry_reconfigure_flow(hass, original.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        edit["flow_id"], {"name": "Window", "kind": "cover", "entity_id": "cover.raw"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert "command_conditions" not in entry.subentries[original.subentry_id].data


async def test_policy_needs_resource_then_creates(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert (await _start(hass, entry, "policy"))["reason"] == "no_resources"
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "name": "Morning",
            "resource_id": resource.subentry_id,
            "kind": "state",
            "target": {"position": 50},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["resource_id"] == resource.subentry_id
    assert result["data"]["priority"] == 0


async def test_typed_return_monitor_resource_form(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    selector = form["data_schema"].schema["return_monitor"]
    assert set(selector.config["fields"]) == {"target_at_most", "warning_after_seconds"}
    assert all(field["required"] for field in selector.config["fields"].values())
    assert selector.config["fields"]["target_at_most"]["selector"]["number"]["max"] == 100
    data = {
        "name": "Roof",
        "kind": "cover",
        "entity_id": "cover.raw",
        "return_monitor": {"target_at_most": 7, "warning_after_seconds": 300},
    }
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["return_monitor"] == data["return_monitor"]


async def test_native_policy_aborts_when_resource_removed_before_submission(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    hass.config_entries.async_remove_subentry(entry, resource.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "name": "Cold",
            "resource_id": resource.subentry_id,
            "kind": "state",
            "target": {"position": 7},
        },
    )
    assert result["reason"] == "no_resources"


async def test_reconfigure_policy_without_resource_aborts(hass):
    from tests.integration.helpers import operator_entry

    entry = operator_entry(
        resources={},
        policies={
            "orphan": {
                "name": "Orphan",
                "resource_id": "missing",
                "kind": "state",
                "target": {"position": 7},
            }
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_subentry_reconfigure_flow(hass, "orphan")
    assert result["reason"] == "no_resources"


async def test_desired_controls_create_reconfigure_and_follow(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    flow = await _start(hass, entry, "intent")
    child = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"name": "Room mode", "initial_value": False}
    )
    assert child["type"] is FlowResultType.CREATE_ENTRY
    child_id = next(iter(entry.subentries))
    parent = await _start(hass, entry, "intent")
    parent = await hass.config_entries.subentries.async_configure(
        parent["flow_id"],
        {
            "name": "Central mode",
            "initial_value": True,
            "on_targets": [child_id],
        },
    )
    assert parent["type"] is FlowResultType.CREATE_ENTRY
    parent_id = next(key for key in entry.subentries if key != child_id)
    edit = await entry.start_subentry_reconfigure_flow(hass, child_id)
    error = await hass.config_entries.subentries.async_configure(
        edit["flow_id"],
        {
            "name": "Room mode",
            "initial_value": False,
            "on_targets": [parent_id],
        },
    )
    assert error["errors"] == {"base": "config_intent_cycle"}
    result = await hass.config_entries.subentries.async_configure(
        edit["flow_id"],
        {
            "name": "Room mode renamed",
            "initial_value": False,
            "on_targets": [],
        },
    )
    assert result["reason"] == "reconfigure_successful"
    hass.states.async_set("switch.raw", "off")
    resource = await _start(hass, entry, "resource")
    resource = await hass.config_entries.subentries.async_configure(
        resource["flow_id"],
        {
            "name": "Follower",
            "kind": "switch",
            "entity_id": "switch.raw",
            "manual_control": False,
        },
    )
    assert resource["type"] is FlowResultType.CREATE_ENTRY
    resource_id = entry.get_subentries_of_type("resource")[0].subentry_id
    policy = await _start(hass, entry, "policy")
    policy = await hass.config_entries.subentries.async_configure(
        policy["flow_id"],
        {
            "name": "Follow room",
            "resource_id": resource_id,
            "kind": "state",
            "intent_id": child_id,
        },
    )
    assert policy["type"] is FlowResultType.CREATE_ENTRY
    assert policy["data"]["intent_id"] == child_id


async def test_requirement_passive_and_overlap(hass, native_sources):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    data = {
        "name": "Ventilation",
        "activation_entities": ["binary_sensor.extraction"],
        "providers": [
            {
                "id": "passive",
                "evidence": [
                    {
                        "entity_id": "binary_sensor.inlet",
                        "kind": "contact",
                        "operator": "eq",
                        "value": "on",
                    }
                ],
            }
        ],
    }
    form = await _start(hass, entry, "requirement")
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["acquisition_timeout"] == 120
    resource = await _resource(hass, entry)
    data["providers"][0].update(resource_id=resource.subentry_id, target={"position": 50})
    form = await _start(hass, entry, "requirement")
    assert (await hass.config_entries.subentries.async_configure(form["flow_id"], data))[
        "type"
    ] is FlowResultType.CREATE_ENTRY
    form = await _start(hass, entry, "requirement")
    assert (await hass.config_entries.subentries.async_configure(form["flow_id"], data))[
        "errors"
    ] == {"base": "config_a_resource_cannot_belong_to_multiple_airflow_control_groups"}


def test_base_flow_requires_a_schema():
    with pytest.raises(NotImplementedError):
        OperatorSubentryFlow()._schema()


async def test_parent_flow_checks_existing_entries_at_submit(hass):
    """An entry added while the form was open cannot create another owner."""
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert result["reason"] == "single_instance_allowed"


async def test_initial_shadow_options_are_explicit_and_saved_separately(hass):
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_setup", new=AsyncMock(return_value=True)
    ):
        form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        defaults = form["data_schema"]({})
        assert defaults == {
            "shadow_lock": False,
        }
        created = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                "shadow_lock": True,
            },
        )
        assert created["data"] == {}
        assert created["options"] == {
            "shadow_lock": True,
        }
        assert created["result"].options == {
            "shadow_lock": True,
        }


async def test_options_preserve_existing_settings_and_reload_once(hass, tmp_path):
    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    original = entry.runtime_data
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["step_id"] == "init"
    assert form["data_schema"]({}) == {
        "shadow_lock": False,
    }
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "shadow_lock": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert original._closed and all(task.done() for task in original._tasks)
    assert entry.runtime_data is not original
    assert len(entry.update_listeners) == 1
    assert entry.options == {
        "shadow_lock": True,
    }
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["data_schema"]({}) == {
        "shadow_lock": True,
    }
    # The explicit false values must survive the native form submission.
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "shadow_lock": False,
        },
    )
    await hass.async_block_till_done()
    assert entry.options == {
        "shadow_lock": False,
    }
    assert len(entry.update_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_preserve_unrelated_entry_options(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            "future_setting": "preserved",
        },
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["data_schema"]({}) == {"shadow_lock": False}
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"shadow_lock": True}
    )
    assert result["data"]["future_setting"] == "preserved"
    assert entry.options["future_setting"] == "preserved"


async def _lock_options_without_reloading(hass, entry):
    """Expose the pre-reload options race while preserving the native listener."""
    with patch.object(entry, "update_listeners", []):
        hass.config_entries.async_update_entry(entry, options={"shadow_lock": True})
    assert entry.runtime_data.mode("roof") == "observe"


async def test_unlock_sets_observe_before_publishing_options(hass, tmp_path):
    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    runtime = entry.runtime_data
    await runtime.async_set_mode("roof", "live")
    await _lock_options_without_reloading(hass, entry)
    assert runtime._state["modes"]["roof"] == "live"
    form = await hass.config_entries.options.async_init(entry.entry_id)
    observed_at_option_update = []

    async def inspect_committed_state(hass, changed_entry):
        observed_at_option_update.append(runtime._state["modes"]["roof"])

    with patch.object(entry, "update_listeners", [inspect_committed_state]):
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"shadow_lock": False}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["shadow_lock"] is False
    assert observed_at_option_update == ["observe"]
    assert runtime._state["modes"]["roof"] == "observe"
    assert runtime.mode("roof") == "observe"  # Old instance retains its latch until reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.mode("roof") == "observe"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unlock_uses_observe_even_when_native_save_fails(hass, tmp_path):
    from homeassistant.helpers.storage import WriteError

    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await entry.runtime_data.async_set_mode("roof", "live")
    await _lock_options_without_reloading(hass, entry)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(
        "homeassistant.helpers.storage.write_utf8_file", side_effect=WriteError("disk full")
    ):
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"shadow_lock": False}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["shadow_lock"] is False
    assert entry.runtime_data.mode("roof") == "observe"
    assert entry.runtime_data.fault is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("loaded", [False, True])
async def test_unlock_without_available_runtime_keeps_lock(hass, loaded):
    from homeassistant.config_entries import ConfigEntryState

    entry = MockConfigEntry(domain=DOMAIN, options={"shadow_lock": True})
    entry.add_to_hass(hass)
    if loaded:
        entry.mock_state(hass, ConfigEntryState.LOADED)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"shadow_lock": False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unlock_unavailable"}
    assert entry.options["shadow_lock"] is True
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


@pytest.mark.parametrize("change", ["state", "runtime"])
async def test_unlock_rechecks_runtime_after_preparation(hass, tmp_path, monkeypatch, change):
    from homeassistant.config_entries import ConfigEntryState

    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    runtime = entry.runtime_data
    await _lock_options_without_reloading(hass, entry)
    prepare = runtime.async_prepare_unlock

    async def prepare_then_change_runtime():
        await prepare()
        if change == "state":
            entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        else:
            entry.runtime_data = object()

    monkeypatch.setattr(runtime, "async_prepare_unlock", prepare_then_change_runtime)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"shadow_lock": False}
    )
    assert result["errors"] == {"base": "unlock_unavailable"}
    assert entry.options["shadow_lock"] is True
    entry.runtime_data = runtime
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unlock_closed_runtime_keeps_lock(hass, tmp_path):
    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await _lock_options_without_reloading(hass, entry)
    await entry.runtime_data.async_close()
    form = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"shadow_lock": False}
    )
    assert result["errors"] == {"base": "unlock_unavailable"}
    assert entry.options["shadow_lock"] is True
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["resource", "policy"])
async def test_native_condition_compile_error_remains_in_native_form(hass, native_sources, kind):
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    if kind == "policy":
        resource = await _resource(hass, entry)
        data = {
            "name": "Bad template",
            "kind": "state",
            "resource_id": resource.subentry_id,
            "target": {"position": 7},
        }
    else:
        data = {"name": "Bad template", "kind": "cover", "entity_id": "cover.raw"}
    condition = [
        {"condition": "template", "value_template": "{{ true }}", "enabled": "{{ 1 / 0 }}"}
    ]
    data.update(
        {"conditions": condition}
        if kind == "policy"
        else {"command_conditions": {"set_cover_position": condition}}
    )
    form = await _start(hass, entry, kind)
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "config_invalid_conditions"}
    assert "ZeroDivisionError" in result["description_placeholders"]["error"]


async def test_native_resource_selector_output_roundtrips_as_json(hass, native_sources):
    import json

    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    data = {
        "name": "Native guards",
        "kind": "cover",
        "entity_id": "cover.raw",
        "command_conditions": {
            "set_cover_position": [
                {"condition": "time", "after": "06:00:00"},
                {"condition": "template", "value_template": "{{ true }}"},
            ]
        },
    }
    validated = form["data_schema"](data)
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], validated)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    saved = json.loads(json.dumps(result["data"]))
    assert saved["command_conditions"] == data["command_conditions"]
