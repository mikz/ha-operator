"""Exercise actual config-entry lifecycle, registry links and fault visibility."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

import custom_components.ha_operator as integration
from custom_components.ha_operator import (
    async_migrate_entry,
    async_remove_config_entry_device,
    async_setup_entry,
    storage,
)
from custom_components.ha_operator import runtime as runtime_module
from custom_components.ha_operator.const import DOMAIN

from .helpers import (
    async_activate,
    async_add_physical_cover,
    async_setup_operator,
    managed_id,
    operator_entry,
)


async def test_real_setup_observe_registry_and_reload(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    registry = er.async_get(hass)
    cover_id = managed_id(hass, "cover")
    assert entry.state is ConfigEntryState.LOADED and entry.data["initialized"]
    assert hass.states.get(cover_id).attributes["current_position"] == 0
    assert hass.states.get(managed_id(hass, "select", key="mode")).state == "observe"
    records = [
        item for item in registry.entities.values() if item.config_entry_id == entry.entry_id
    ]
    assert len(records) == 13
    assert {item.config_subentry_id for item in records} == {"roof"}
    assert len({item.device_id for item in records}) == 1
    assert raw.commands == []
    assert registry.async_get(managed_id(hass, "sensor", key="attempts")).disabled_by is not None
    assert (
        registry.async_get(managed_id(hass, "sensor", key="next_attempt")).disabled_by is not None
    )
    assert len(entry.runtime_data._tasks) == 1 and len(entry.runtime_data._listeners) == 11
    for _ in range(3):
        previous = entry.runtime_data
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert previous._closed and not previous._listeners
        assert all(task.done() for task in previous._tasks)
        assert entry.runtime_data is not previous
        assert len(entry.runtime_data._tasks) == 1
        assert not entry.runtime_data._tasks[0].done()
        assert managed_id(hass, "cover") == cover_id
    active = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert active._closed and all(task.done() for task in active._tasks)
    assert not active._listeners
    assert hass.states.get(cover_id).state == "unavailable"
    assert raw.commands == []


async def test_live_native_request_roundtrip_survives_reload(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    await hass.services.async_call(
        "cover",
        "set_cover_position",
        {
            "entity_id": managed_id(hass, "cover"),
            "position": 67,
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert raw.commands == [("position", 67)]
    assert hass.states.get(managed_id(hass, "cover")).attributes["current_position"] == 0
    assert float(hass.states.get(managed_id(hass, "sensor", key="desired")).state) == 67
    expiry = entry.runtime_data.manual("roof").expires_at
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.mode("roof") == "live"
    assert entry.runtime_data.manual("roof").expires_at == expiry
    assert entry.runtime_data.manual("roof").target.position == 67
    assert len(raw.commands) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_real_policy_and_requirement_entities(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    hass.states.async_set("binary_sensor.extraction", "off")
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 80},
            }
        },
        requirements={
            "air": {
                "name": "Air",
                "activation_entities": ["binary_sensor.extraction"],
                "providers": [
                    {
                        "id": "window",
                        "resource_id": "roof",
                        "target": {"position": 60},
                        "evidence": [
                            {
                                "entity_id": raw.entity_id,
                                "attribute": "current_position",
                                "operator": "gte",
                                "value": 20,
                                "kind": "position",
                            }
                        ],
                    }
                ],
            }
        },
    )
    assert hass.states.get(managed_id(hass, "switch", "morning", "enabled")).state == "on"
    assert hass.states.get(managed_id(hass, "sensor", "air", "status")).state == "inactive"
    assert hass.states.get(managed_id(hass, "binary_sensor", "air", "unmet")).state == "off"
    await hass.services.async_call(
        "switch",
        "turn_off",
        {
            "entity_id": managed_id(hass, "switch", "morning", "enabled"),
        },
        blocking=True,
    )
    assert not entry.runtime_data.policy_enabled("morning")
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [("request_id", {}), ("request_id", []), ("source", {}), ("source", [])],
)
async def test_saved_manual_metadata_fails_readiness_before_workers(hass, tmp_path, field, value):
    """Invalid saved metadata must use the storage Repair startup boundary."""
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    saved = storage._empty_state()
    saved["manuals"]["roof"] = {
        "mode": "hands_off",
        "target": None,
        "expires_at": None,
        field: value,
    }
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    contents = json.dumps({"version": 3, "data": saved})
    await hass.async_add_executor_job(path.write_text, contents)
    try:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
        runtime = entry.runtime_data
        assert runtime._closed and not runtime._tasks and not runtime._unsubscribers
        assert not runtime._timers and not runtime._listeners
        assert path.read_text() == contents and raw.commands == []
    finally:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("record_kind", ["manual", "target", "occurrence", "receipt"])
async def test_oversized_saved_manual_expiry_fails_readiness_before_workers(
    hass, tmp_path, monkeypatch, record_kind
):
    """An unrepresentable saved deadline must use the native storage Repair boundary."""
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    saved = storage._empty_state()
    if record_kind == "manual":
        saved["manuals"]["roof"] = {"mode": "hands_off", "target": None, "expires_at": 10**399}
    elif record_kind == "target":
        saved["manuals"]["roof"] = {
            "mode": "target",
            "target": {"position": 10**399},
            "expires_at": None,
        }
    elif record_kind == "occurrence":
        saved["occurrences"][json.dumps(["legacy", "episode"])] = {
            "policy_id": "legacy",
            "occurrence_id": "episode",
            "expires_at": 10**399,
            "skipped": True,
            "target": None,
        }
    else:
        saved["requests"]["legacy"] = {
            "fingerprint": "0" * 64,
            "receipt": {"request_id": "legacy", "resource_id": "roof", "expires_at": 10**399},
        }
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    contents = json.dumps({"version": 3, "data": saved})
    await hass.async_add_executor_job(path.write_text, contents)
    setup_errors = []
    native_setup = integration.async_setup_entry

    async def capture_setup_error(hass, entry):
        try:
            return await native_setup(hass, entry)
        except Exception as error:
            setup_errors.append(error)
            raise

    monkeypatch.setattr(integration, "async_setup_entry", capture_setup_error)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    runtime = entry.runtime_data
    assert runtime._closed and not runtime._tasks and not runtime._unsubscribers
    assert not runtime._timers and not runtime._listeners and runtime._deadline_timer is None
    assert path.read_text() == contents and raw.commands == []
    assert not any(entity.platform == DOMAIN for entity in er.async_get(hass).entities.values())
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}")
    assert len(setup_errors) == 1 and isinstance(setup_errors[0], ConfigEntryError), (
        f"storage Repair={issue!r}; runtime fault={runtime.fault!r}"
    )
    assert setup_errors[0].translation_domain == DOMAIN
    assert setup_errors[0].translation_key == "storage_error"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None


@pytest.mark.parametrize("field", ["duration", "expires_at", "position"])
async def test_oversized_native_request_rejects_with_translated_validation_before_save(
    hass, tmp_path, field
):
    """Reject oversized action numbers before durable admission with native validation."""
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    await async_activate(hass)
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    before = path.read_bytes()
    data = {"resource_id": "roof", "request_id": "oversized-action"}
    if field == "position":
        data.update(mode="target", target={"position": 10**399})
    else:
        data.update(mode="hands_off", **{field: 10**399})
    try:
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN, "request", data, blocking=True, return_response=True
            )
        error = raised.value
        assert error.translation_domain == DOMAIN and error.translation_key == "finite_number"
        assert error.translation_placeholders == {"field": field}
        await hass.async_block_till_done()
        assert path.read_bytes() == before
        assert entry.runtime_data.fault is None and raw.commands == []
        assert len(entry.runtime_data._tasks) == 1
        assert all(not task.done() for task in entry.runtime_data._tasks)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("deadline", [1e100, 1.8e12])
async def test_far_future_deadline_keeps_native_request_and_timestamp_presentation_safe(
    hass, tmp_path, deadline
):
    """Unrepresentable dates must not interrupt durable native requests."""
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    registry = er.async_get(hass)
    next_attempt_id = managed_id(hass, "sensor", key="next_attempt")
    registry.async_update_entity(next_attempt_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await async_activate(hass)
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    before = json.loads(path.read_text())
    try:
        response = await hass.services.async_call(
            DOMAIN,
            "request",
            {
                "resource_id": "roof",
                "mode": "hands_off",
                "expires_at": deadline,
                "request_id": "far-future-expiry",
            },
            blocking=True,
            return_response=True,
        )
        await hass.async_block_till_done()
        after = json.loads(path.read_text())
        assert response["expires_at"] == deadline
        assert response["accepted"] is True
        assert after["data"]["revision"] == before["data"]["revision"] + 1
        assert after["data"]["manuals"]["roof"]["expires_at"] == deadline
        assert after["data"]["requests"]["far-future-expiry"]["receipt"] == {
            "request_id": "far-future-expiry",
            "resource_id": "roof",
            "expires_at": deadline,
        }
        for key in ("expiry", "effective_expiry", "next_attempt"):
            state = hass.states.get(managed_id(hass, "sensor", key=key))
            assert state is not None and state.state == "unknown"
        assert entry.runtime_data.fault is None and raw.commands == []
        assert len(entry.runtime_data._tasks) == 1
        assert all(not task.done() for task in entry.runtime_data._tasks)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_saved_finite_far_future_expiry_preserves_native_timestamp_entities(hass, tmp_path):
    """Valid finite saved intent must not prevent timestamp entities from being added."""
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    saved = storage._empty_state()
    saved["manuals"]["roof"] = {"mode": "hands_off", "target": None, "expires_at": 1e100}
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    contents = json.dumps({"version": 3, "data": saved})
    await hass.async_add_executor_job(path.write_text, contents)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        for key in ("expiry", "effective_expiry"):
            state = hass.states.get(managed_id(hass, "sensor", key=key))
            assert state is not None and state.state == "unknown"
        assert path.read_text() == contents
        assert entry.runtime_data.manual("roof").expires_at == 1e100
        assert entry.runtime_data.fault is None and raw.commands == []
        assert len(entry.runtime_data._tasks) == 1
        assert all(not task.done() for task in entry.runtime_data._tasks)
    finally:
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize("target", [None, False, 0, "", [], {}])
@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"request_id": None},
        {"request_id": None, "source": None},
        {"request_id": "", "source": ""},
        {"request_id": "legacy" * 40, "source": "legacy" * 40},
    ],
)
async def test_legacy_manual_metadata_and_falsey_hands_off_targets_load_unchanged(
    hass, tmp_path, target, metadata
):
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    saved = storage._empty_state()
    saved["manuals"]["roof"] = {
        "mode": "hands_off",
        "target": target,
        "expires_at": None,
        **metadata,
    }
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    contents = json.dumps({"version": 3, "data": saved})
    await hass.async_add_executor_job(path.write_text, contents)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is None
        assert path.read_text() == contents
        lease = entry.runtime_data.manual("roof")
        assert lease.mode == "hands_off" and lease.target is None
        assert lease.request_id == metadata.get("request_id")
        assert lease.source == metadata.get("source", "service")
        assert raw.commands == []
    finally:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_corrupt_storage_fails_setup_with_repair_and_public_diagnostics(
    hass, tmp_path, hass_client
):
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    await hass.async_add_executor_job(path.write_text, "not json")
    assert await async_setup_component(hass, "diagnostics", {})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    runtime = entry.runtime_data
    assert runtime._closed and not runtime._tasks and not runtime._unsubscribers
    assert not any(entity.platform == DOMAIN for entity in er.async_get(hass).entities.values())
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == 200
    diagnostics = (await response.json())["data"]
    assert diagnostics["faulted"] and "physical_roof" not in json.dumps(diagnostics)
    assert diagnostics["runtime_state"] == "closed"
    del entry.runtime_data
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == 200
    absent = (await response.json())["data"]
    assert absent["runtime_state"] == "absent" and absent["faulted"]
    assert path.read_text() == "not json"
    assert raw.commands == []
    # Recovery is explicit; the retained Repair clears only on a healthy reload.
    await hass.async_add_executor_job(
        path.write_text, json.dumps({"version": 3, "data": storage._empty_state()})
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is None
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_registry_failure_after_start_quiesces_and_preserves_original_error(
    hass, tmp_path, monkeypatch, cleanup_error
):
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    saved = storage._empty_state()
    saved["modes"]["roof"] = "live"
    saved["manuals"]["roof"] = {
        "mode": "target",
        "target": {"position": 80},
        "expires_at": None,
    }
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    await hass.async_add_executor_job(path.write_text, json.dumps({"version": 3, "data": saved}))
    registry = er.async_get(hass)
    monkeypatch.setattr(
        registry, "async_get_entity_id", lambda *args: (_ for _ in ()).throw(ValueError("registry"))
    )
    close = runtime_module.OperatorRuntime.async_close

    async def failing_close(runtime):
        await close(runtime)
        raise RuntimeError("cleanup")

    if cleanup_error:
        monkeypatch.setattr(runtime_module.OperatorRuntime, "async_close", failing_close)
    with pytest.raises(ValueError, match="registry"):
        await async_setup_entry(hass, entry)
    runtime = entry.runtime_data
    assert runtime._closed and all(task.done() for task in runtime._tasks)
    assert not runtime._unsubscribers and not runtime._listeners and not runtime._timers
    assert runtime._deadline_timer is None
    await hass.async_block_till_done()
    assert raw.commands == []


@pytest.mark.parametrize("source_state", [None, "unknown", "unavailable"])
async def test_unusable_downstream_feedback_does_not_fail_setup(hass, tmp_path, source_state):
    raw = await async_add_physical_cover(hass)
    if source_state is None:
        hass.states.async_remove("cover.physical_roof")
    else:
        hass.states.async_set("cover.physical_roof", source_state)
    entry = await async_setup_operator(hass, tmp_path)
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.fault is None
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("failure", ["missing", "records", "write"])
async def test_authoritative_initialization_failure_preserves_snapshot_and_quiesces(
    hass, tmp_path, monkeypatch, failure
):
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": failure != "write"})
    entry.add_to_hass(hass)
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    contents = None
    if failure == "records":
        state = storage._empty_state()
        state["manuals"]["roof"] = {"mode": "target", "target": {}, "expires_at": None}
        contents = json.dumps({"version": 3, "data": state})
        await hass.async_add_executor_job(path.write_text, contents)
    elif failure == "write":

        def fail_write(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(storage, "_write_snapshot", fail_write)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    runtime = entry.runtime_data
    assert runtime._closed and not runtime._tasks and not runtime._unsubscribers
    assert runtime.fault == "storage_error"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is not None
    assert path.read_text() == contents if contents is not None else not path.exists()
    assert raw.commands == []
    assert not any(entity.platform == DOMAIN for entity in er.async_get(hass).entities.values())


async def test_programming_error_during_initialization_is_not_storage_fault(
    hass, tmp_path, monkeypatch
):
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry()
    entry.add_to_hass(hass)

    async def bug(runtime):
        raise KeyError("programming error")

    monkeypatch.setattr(runtime_module.OperatorRuntime, "_recover_policy_inputs", bug)
    with pytest.raises(KeyError, match="programming error"):
        await async_setup_entry(hass, entry)
    assert entry.runtime_data._closed and entry.runtime_data.fault is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"storage_{entry.entry_id}") is None
    assert raw.commands == []


async def test_failed_shadow_locked_start_does_not_attempt_abort_persistence(
    hass, tmp_path, monkeypatch
):
    raw = await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(data={"initialized": True})
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"shadow_lock": True})
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    await hass.async_add_executor_job(path.write_text, "corrupt")

    async def forbidden_write(*args, **kwargs):
        pytest.fail("Failed authoritative startup must not attempt cleanup persistence")

    monkeypatch.setattr(storage.IntentStore, "async_update", forbidden_write)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert path.read_text() == "corrupt" and raw.commands == []
    assert entry.runtime_data._closed


async def test_bad_config_fails_setup_without_background_workers(hass, tmp_path):
    await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(
        resources={
            "roof": {
                "name": "Roof",
                "kind": "cover",
                "entity_id": "switch.wrong_domain",
            }
        }
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(entry, "runtime_data")


async def test_forwarding_failure_quiesces_started_runtime(hass, tmp_path):
    await async_add_physical_cover(hass)
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry()
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await async_setup_entry(hass, entry)
    assert entry.runtime_data._closed
    assert all(task.done() for task in entry.runtime_data._tasks)


async def test_migration_and_device_removal_contract(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    record = er.async_get(hass).async_get(managed_id(hass, "cover"))
    device = dr.async_get(hass).async_get(record.device_id)
    assert not await async_remove_config_entry_device(hass, entry, device)
    assert await async_remove_config_entry_device(
        hass, entry, SimpleNamespace(config_entry_id="other")
    )
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert await async_migrate_entry(hass, operator_entry(version=2))
    assert not await async_migrate_entry(hass, operator_entry(version=3))
    assert raw.commands == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_input_config_migration_preserves_all_settings_and_ids(hass):
    entry = operator_entry(
        version=1,
        data={"initialized": True, "custom": "preserved"},
        policies={
            "cold": {
                "name": "Cold",
                "resource_id": "roof",
                "kind": "state",
                "target": {"position": 7},
                "eligibility_entity": "input_boolean.ready",
            }
        },
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"shadow_lock": True})
    data, options, subentries = entry.data, entry.options, entry.subentries
    identifiers = {key: item.subentry_id for key, item in subentries.items()}
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2 and entry.minor_version == 1
    assert entry.data is data and entry.options is options and entry.subentries is subentries
    assert {key: item.subentry_id for key, item in entry.subentries.items()} == identifiers
    assert "return_monitor" not in entry.subentries["roof"].data
    assert "input" not in entry.subentries["cold"].data
    assert entry.subentries["cold"].data["eligibility_entity"] == "input_boolean.ready"
    assert await async_migrate_entry(hass, entry)
    assert entry.subentries is subentries


async def test_entry_update_listener_reloads_exactly_once(hass, tmp_path):
    await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    old = entry.runtime_data
    hass.config_entries.async_update_entry(entry, title="Renamed operator")
    await hass.async_block_till_done()
    assert old._closed and all(task.done() for task in old._tasks)
    assert entry.runtime_data is not old
    assert len(entry.runtime_data._tasks) == 1
    assert len(entry.update_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_request_composes_legacy_indefinite_fan_without_expiry(hass, tmp_path):
    """An omitted optional expiry remains an indefinite saved fan lease."""
    hass.states.async_set(
        "fan.raw", "off", {"supported_features": 53, "percentage": 0, "direction": "forward"}
    )
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(
        resources={
            "vent": {
                "name": "Vent",
                "kind": "fan",
                "entity_id": "fan.raw",
                "default_target": {"on": True, "percentage": 100, "direction": "forward"},
            }
        },
        data={"initialized": True},
    )
    entry.add_to_hass(hass)
    saved = storage._empty_state()
    saved["modes"]["vent"] = "live"
    saved["manuals"]["vent"] = {
        "mode": "target",
        "target": {"on": True, "percentage": 75, "direction": "forward"},
        "request_id": "legacy",
    }
    path = tmp_path / f"ha_operator.{entry.entry_id}.json"
    await hass.async_add_executor_job(path.write_text, json.dumps({"version": 3, "data": saved}))
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.manual("vent").expires_at is None
        receipt = await hass.services.async_call(
            DOMAIN,
            "request",
            {"resource_id": "vent", "target": {"direction": "reverse"}, "indefinite": True},
            blocking=True,
            return_response=True,
        )
        assert receipt["expires_at"] is None
        assert entry.runtime_data.manual("vent").target.percentage == 75
        assert entry.runtime_data.manual("vent").target.direction == "reverse"
    finally:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
