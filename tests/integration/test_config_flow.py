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
    assert invalid["errors"] == {"base": "entity_not_found"}
    assert "cover.missing" in invalid["description_placeholders"]["detail"]
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
        assert error["errors"] == {"base": "entity_not_found"}
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
    assert duplicate["errors"] == {"base": "duplicate_output"}


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
    ] == {"base": "overlapping_requirement"}


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
        assert defaults == {"trace_enabled": False, "shadow_lock": False, "trace_entities": []}
        created = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {"trace_enabled": True, "shadow_lock": True, "trace_entities": ["switch.cellar_power"]},
        )
        assert created["data"] == {}
        assert created["options"] == {
            "trace_enabled": True,
            "shadow_lock": True,
            "trace_entities": ["switch.cellar_power"],
        }
        assert created["result"].options == {
            "trace_enabled": True,
            "shadow_lock": True,
            "trace_entities": ["switch.cellar_power"],
        }


async def test_options_preserve_existing_settings_and_reload_once(hass, tmp_path):
    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    original = entry.runtime_data
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["step_id"] == "init"
    assert form["data_schema"]({}) == {
        "trace_enabled": False,
        "shadow_lock": False,
        "trace_entities": [],
    }
    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {"trace_enabled": True, "shadow_lock": True, "trace_entities": ["switch.cellar_power"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert original._closed and all(task.done() for task in original._tasks)
    assert entry.runtime_data is not original
    assert len(entry.update_listeners) == 1
    assert entry.options == {
        "trace_enabled": True,
        "shadow_lock": True,
        "trace_entities": ["switch.cellar_power"],
    }
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["data_schema"]({}) == {
        "trace_enabled": True,
        "shadow_lock": True,
        "trace_entities": ["switch.cellar_power"],
    }
    # The explicit false values must survive the native form submission.
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"trace_enabled": False, "shadow_lock": False, "trace_entities": []}
    )
    await hass.async_block_till_done()
    assert entry.options == {"trace_enabled": False, "shadow_lock": False, "trace_entities": []}
    assert len(entry.update_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_preserve_unrelated_entry_options(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={"future_setting": "preserved", "trace_entities": ["switch.cellar_power"]},
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["data_schema"]({})["trace_entities"] == ["switch.cellar_power"]
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"trace_enabled": True, "shadow_lock": True}
    )
    assert result["data"]["future_setting"] == "preserved"
    assert entry.options["future_setting"] == "preserved"
    assert entry.options["trace_entities"] == ["switch.cellar_power"]


async def _lock_options_without_reloading(hass, entry):
    """Expose the pre-reload options race while preserving the native listener."""
    with patch.object(entry, "update_listeners", []):
        hass.config_entries.async_update_entry(entry, options={"shadow_lock": True})
    assert entry.runtime_data.mode("roof") == "observe"


async def test_unlock_saves_observe_before_publishing_options(hass, tmp_path):
    import json

    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    runtime = entry.runtime_data
    await runtime.async_set_mode("roof", "live")
    await _lock_options_without_reloading(hass, entry)
    assert runtime.store.state["modes"]["roof"] == "live"
    form = await hass.config_entries.options.async_init(entry.entry_id)
    observed_at_option_update = []

    async def inspect_committed_state(hass, changed_entry):
        contents = await hass.async_add_executor_job(runtime.store._path.read_text)
        observed_at_option_update.append(json.loads(contents)["data"]["modes"]["roof"])

    with patch.object(entry, "update_listeners", [inspect_committed_state]):
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], {"shadow_lock": False}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["shadow_lock"] is False
    assert observed_at_option_update == ["observe"]
    assert runtime.store.state["modes"]["roof"] == "observe"
    assert runtime.mode("roof") == "observe"  # Old instance retains its latch until reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.mode("roof") == "observe"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_unlock_save_keeps_options_locked(hass, tmp_path, monkeypatch):
    from custom_components.ha_operator import storage

    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await entry.runtime_data.async_set_mode("roof", "live")
    await _lock_options_without_reloading(hass, entry)
    saved_options = dict(entry.options)
    form = await hass.config_entries.options.async_init(entry.entry_id)

    def failed_write(*_):
        raise OSError("simulated disk full")

    monkeypatch.setattr(storage, "_write_snapshot", failed_write)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"shadow_lock": False, "trace_enabled": True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unlock_persistence_failed"}
    assert result["data_schema"]({})["shadow_lock"] is True
    assert dict(entry.options) == saved_options
    assert entry.runtime_data.mode("roof") == "observe"
    assert entry.runtime_data.store.state["modes"]["roof"] == "live"
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
async def test_unlock_rechecks_runtime_after_save(hass, tmp_path, monkeypatch, change):
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


async def test_cancelled_unlock_waits_for_write_but_keeps_options_locked(
    hass, tmp_path, monkeypatch
):
    import asyncio
    import threading

    from custom_components.ha_operator import storage

    from .helpers import async_add_physical_cover, async_setup_operator

    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await entry.runtime_data.async_set_mode("roof", "live")
    await _lock_options_without_reloading(hass, entry)
    entered, release = threading.Event(), threading.Event()
    write = storage._write_snapshot

    def delayed_write(path, state):
        entered.set()
        assert release.wait(5)
        write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", delayed_write)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    pending = asyncio.create_task(
        hass.config_entries.options.async_configure(form["flow_id"], {"shadow_lock": False})
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
        pending.cancel()
        assert entry.options["shadow_lock"] is True
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert entry.options["shadow_lock"] is True
    assert entry.runtime_data.store.state["modes"]["roof"] == "observe"
    assert await hass.config_entries.async_unload(entry.entry_id)
