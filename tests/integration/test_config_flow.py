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


@pytest.mark.parametrize("input_type", ["qualified_numeric", "timer_episode"])
async def test_native_policy_forms_create_then_reconfigure_type_and_legacy(
    hass, native_sources, input_type
):
    hass.states.async_set("sensor.temperature", "15", {"unit_of_measurement": "°C"})
    hass.states.async_set("timer.ventilation", "idle")
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    initial = await _start(hass, entry, "policy")
    schema = initial["data_schema"].schema
    assert schema["input_type"].config["options"] == [
        "legacy",
        "qualified_numeric",
        "timer_episode",
    ]
    assert next(key for key in schema if key == "input_type").default() == "legacy"
    base = {
        "name": "Native",
        "resource_id": resource.subentry_id,
        "kind": "state" if input_type == "qualified_numeric" else "occurrence",
        "target": {"position": 7},
        "priority": 42,
        "input_type": input_type,
    }
    second = await hass.config_entries.subentries.async_configure(initial["flow_id"], base)
    assert second["step_id"] == input_type
    assert len(entry.subentries) == 1
    fields = second["data_schema"].schema
    numeric = input_type == "qualified_numeric"
    assert set(fields) == (
        {"entity_id", "threshold", "unit", "qualification_seconds"}
        if numeric
        else {"entity_id", "qualification_seconds", "request_seconds"}
    )
    assert fields["entity_id"].config["filter"] == [{"domain": ["sensor" if numeric else "timer"]}]
    native = {
        "entity_id": "sensor.temperature" if numeric else "timer.ventilation",
        "qualification_seconds": 60,
    }
    native.update({"threshold": 16, "unit": "°C"} if numeric else {"request_seconds": 1800})
    created = await hass.config_entries.subentries.async_configure(second["flow_id"], native)
    assert created["type"] is FlowResultType.CREATE_ENTRY
    configured = next(iter(entry.get_subentries_of_type("policy")))
    identifier = configured.subentry_id
    assert configured.data["priority"] == 42 and "input_type" not in configured.data
    edit = await entry.start_subentry_reconfigure_flow(hass, identifier)
    suggested = next(key for key in edit["data_schema"].schema if key == "input_type")
    assert suggested.description["suggested_value"] == input_type
    second = await hass.config_entries.subentries.async_configure(edit["flow_id"], base)
    for key in second["data_schema"].schema:
        assert key.description["suggested_value"] == native[str(key)]
    changed = await hass.config_entries.subentries.async_configure(
        second["flow_id"], native | {"qualification_seconds": 90}
    )
    assert changed["reason"] == "reconfigure_successful"
    assert entry.subentries[identifier].data["input"]["qualification_seconds"] == 90
    other_type = "timer_episode" if numeric else "qualified_numeric"
    edit = await entry.start_subentry_reconfigure_flow(hass, identifier)
    second = await hass.config_entries.subentries.async_configure(
        edit["flow_id"],
        base | {"input_type": other_type, "kind": "occurrence" if numeric else "state"},
    )
    other = {
        "entity_id": "timer.ventilation" if numeric else "sensor.temperature",
        "qualification_seconds": 20,
    }
    other.update({"request_seconds": 100} if numeric else {"threshold": 17, "unit": "°C"})
    changed = await hass.config_entries.subentries.async_configure(second["flow_id"], other)
    assert changed["reason"] == "reconfigure_successful"
    assert set(entry.subentries[identifier].data["input"]) == set(other) | {"type"} | (
        {"comparison"} if not numeric else set()
    )
    edit = await entry.start_subentry_reconfigure_flow(hass, identifier)
    legacy = {key: value for key, value in base.items() if key != "input_type"}
    changed = await hass.config_entries.subentries.async_configure(edit["flow_id"], legacy)
    assert changed["reason"] == "reconfigure_successful"
    assert "input" not in entry.subentries[identifier].data


@pytest.mark.parametrize(
    "base_patch,source_patch,code",
    [
        ({}, {"entity_id": "sensor.missing"}, "config_entity_not_found"),
        ({}, {"entity_id": "timer.ventilation"}, "config_entity_domain"),
        ({}, {"unit": "°F"}, "config_input_unit_must_match_the_sensor_unit"),
        ({}, {"threshold": float("nan")}, "config_number_minimum"),
    ],
)
async def test_native_policy_final_atomic_validation_errors(
    hass, native_sources, base_patch, source_patch, code
):
    hass.states.async_set("sensor.temperature", "15", {"unit_of_measurement": "°C"})
    hass.states.async_set("timer.ventilation", "idle")
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    initial = await _start(hass, entry, "policy")
    form = await hass.config_entries.subentries.async_configure(
        initial["flow_id"],
        {
            "name": "Cold",
            "resource_id": resource.subentry_id,
            "kind": "state",
            "target": {"position": 7},
            "input_type": "qualified_numeric",
            **base_patch,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "entity_id": "sensor.temperature",
            "threshold": 16,
            "unit": "°C",
            "qualification_seconds": 60,
            **source_patch,
        },
    )
    assert result["step_id"] == "qualified_numeric" and result["errors"] == {"base": code}
    assert "detail" not in result["description_placeholders"]
    assert len(entry.subentries) == 1


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
            "input_type": "qualified_numeric",
        },
    )
    assert result["reason"] == "no_resources"


async def test_native_policy_aborts_when_resource_removed_before_final_submission(
    hass, native_sources
):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    second = await hass.config_entries.subentries.async_configure(
        form["flow_id"],
        {
            "name": "Vent",
            "resource_id": resource.subentry_id,
            "kind": "occurrence",
            "target": {"position": 100},
            "input_type": "timer_episode",
        },
    )
    hass.config_entries.async_remove_subentry(entry, resource.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        second["flow_id"],
        {
            "entity_id": "timer.ventilation",
            "qualification_seconds": 60,
            "request_seconds": 1800,
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


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"target": {"on": True}}, "config_target_fields_are_not_supported_by"),
        ({"eligibility_entity": "input_boolean.ready"}, "config_input_eligibility_conflict"),
        ({"priority": 0.5}, "config_priority_must_be_an_integer"),
    ],
)
async def test_native_common_errors_are_repairable_before_input_form(
    hass, native_sources, patch, code
):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    resource = await _resource(hass, entry)
    form = await _start(hass, entry, "policy")
    common = {
        "name": "Native",
        "resource_id": resource.subentry_id,
        "kind": "state",
        "target": {"position": 7},
        "input_type": "timer_episode",
    }
    failed = await hass.config_entries.subentries.async_configure(form["flow_id"], common | patch)
    assert failed["step_id"] == "user" and failed["errors"] == {"base": code}
    assert len(entry.subentries) == 1
    corrected = await hass.config_entries.subentries.async_configure(failed["flow_id"], common)
    assert corrected["step_id"] == "timer_episode"
    hass.states.async_set("timer.ventilation", "idle")
    created = await hass.config_entries.subentries.async_configure(
        corrected["flow_id"],
        {"entity_id": "timer.ventilation", "qualification_seconds": 60, "request_seconds": 1800},
    )
    assert created["data"]["kind"] == "occurrence"


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
