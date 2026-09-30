"""Durable return deadlines and warnings use independently reported raw feedback."""

# ruff: noqa: F811 - imported fixture intentionally injected by name

import asyncio
import threading
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.exceptions import HomeAssistantError

from custom_components.ha_operator import storage
from custom_components.ha_operator.runtime import OperatorRuntime
from tests.integration.test_runtime_reconciliation import (
    advance,  # noqa: F401
    cover_data,
    reported,
    runtime_factory,  # noqa: F401
)


@pytest.fixture
def notifications():
    with (
        patch.object(persistent_notification, "async_create") as create,
        patch.object(persistent_notification, "async_dismiss") as dismiss,
    ):
        yield create, dismiss


async def setup_monitor(
    hass, factory, *, policy=True, eligibility_entity=None, **resource_settings
):
    reported(hass, position=100)
    runtime = await factory(
        resources={
            "roof": cover_data(
                return_monitor={"target_at_most": 7, "warning_after_seconds": 300},
                **resource_settings,
            )
        },
        policies={
            "cold": {
                "name": "Cold",
                "kind": "state",
                "resource_id": "roof",
                "target": {"position": 7},
                **({"eligibility_entity": eligibility_entity} if eligibility_entity else {}),
            }
        }
        if policy
        else None,
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    return runtime


async def test_refusal_retries_and_unchanged_reports_keep_due_warn_once_then_raw_confirm(
    hass, runtime_factory, freezer, notifications
):
    factory, commands = runtime_factory
    create, dismiss = notifications
    runtime = await setup_monitor(hass, factory)
    due = runtime.return_monitor("roof").due_at
    revision = runtime.store.state["revision"]
    assert commands[0][1]["position"] == 7
    assert runtime.observations["roof"].target.position == 100
    for _ in range(4):
        reported(hass, position=100)
        await hass.async_block_till_done()
        await advance(hass, freezer, 30)
        assert runtime.return_monitor("roof").due_at == due
        assert runtime.store.state["revision"] == revision
    assert len(commands) > 1
    assert create.call_count == 0
    await advance(hass, freezer, 180)
    assert runtime.return_monitor("roof").overdue
    assert create.call_count == 1
    message = create.call_args.args[1]
    assert "raw position is 100%" in message
    assert "rain" not in message
    warning_id = create.call_args.kwargs["notification_id"]
    warned_revision = runtime.store.state["revision"]
    for _ in range(3):
        reported(hass, position=100)
        await advance(hass, freezer, 30)
    assert create.call_count == 1
    assert runtime.store.state["revision"] == warned_revision
    reported(hass, position=7)
    await hass.async_block_till_done()
    assert runtime.return_monitor("roof").phase == "idle"
    assert dismiss.call_args.args == (hass, warning_id)
    assert runtime.decisions["roof"].status == "satisfied"
    settled_revision = runtime.store.state["revision"]
    reported(hass, position=7)
    await hass.async_block_till_done()
    assert runtime.store.state["revision"] == settled_revision
    assert runtime._input_flush is None


@pytest.mark.parametrize(
    "raw_state,attributes",
    [
        ("unavailable", {}),
        ("open", {"restored": True}),
        ("open", {"current_position": None}),
    ],
)
async def test_unknown_missing_or_restored_raw_position_cannot_confirm(
    hass, runtime_factory, freezer, notifications, raw_state, attributes
):
    factory, _ = runtime_factory
    create, _ = notifications
    runtime = await setup_monitor(hass, factory)
    due = runtime.return_monitor("roof").due_at
    reported(hass, position=7, state=raw_state, **attributes)
    await hass.async_block_till_done()
    assert runtime.return_monitor("roof").due_at == due
    await advance(hass, freezer, 300)
    assert runtime.return_monitor("roof").overdue
    assert "unavailable" in create.call_args.args[1]
    reported(hass, position=7)
    await hass.async_block_till_done()
    assert runtime.return_monitor("roof").phase == "idle"


async def test_target_change_restarts_period_and_end_conditions_clear(
    hass, runtime_factory, freezer, notifications
):
    factory, _ = runtime_factory
    runtime = await setup_monitor(hass, factory)
    due = runtime.return_monitor("roof").due_at
    await advance(hass, freezer, 300)
    assert runtime.return_monitor("roof").overdue
    await runtime.async_request("roof", target={"position": 0}, indefinite=True)
    await hass.async_block_till_done()
    assert runtime.return_monitor("roof").target_position == 0
    assert runtime.return_monitor("roof").due_at == due + 300
    assert not runtime.return_monitor("roof").overdue
    await runtime.async_request("roof", mode="hands_off", indefinite=True)
    await hass.async_block_till_done()
    assert runtime.return_monitor("roof").phase == "idle"
    await runtime.async_release("roof")
    await hass.async_block_till_done()
    assert runtime.return_monitor("roof").phase == "waiting"
    await runtime.async_set_mode("roof", "observe")
    await hass.async_block_till_done()
    assert runtime.return_monitor("roof").phase == "idle"
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await runtime.async_set_policy_enabled("cold", False)
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].status == "idle"
    assert runtime.return_monitor("roof").phase == "idle"


@pytest.mark.parametrize("overdue", [False, True])
async def test_restart_preserves_deadline_and_reconstructs_unresolved_warning(
    hass, runtime_factory, freezer, notifications, overdue
):
    factory, _ = runtime_factory
    create, _ = notifications
    runtime = await setup_monitor(hass, factory)
    due = runtime.return_monitor("roof").due_at
    await advance(hass, freezer, 300 if overdue else 100)
    previous_id = create.call_args.kwargs["notification_id"] if overdue else None
    await runtime.async_close()
    create.reset_mock()
    fresh = OperatorRuntime(hass, runtime.entry)
    await fresh.async_start()
    try:
        await hass.async_block_till_done()
        assert fresh.return_monitor("roof").due_at == due
        if not overdue:
            assert create.call_count == 0
            await advance(hass, freezer, 200)
        assert fresh.return_monitor("roof").overdue
        assert create.call_count == 1
        if previous_id:
            assert create.call_args.kwargs["notification_id"] == previous_id
        await fresh.async_reconcile()
        await hass.async_block_till_done()
        assert create.call_count == 1
    finally:
        await fresh.async_close()


async def test_confirmed_before_restart_dismisses_durable_overdue_notification(
    hass, runtime_factory, freezer, notifications
):
    factory, _ = runtime_factory
    create, dismiss = notifications
    runtime = await setup_monitor(hass, factory)
    await advance(hass, freezer, 300)
    warning_id = create.call_args.kwargs["notification_id"]
    await runtime.async_close()
    reported(hass, position=7)
    create.reset_mock()
    fresh = OperatorRuntime(hass, runtime.entry)
    await fresh.async_start()
    try:
        await hass.async_block_till_done()
        assert fresh.return_monitor("roof").phase == "idle"
        assert create.call_count == 0
        assert dismiss.call_args.args == (hass, warning_id)
    finally:
        await fresh.async_close()


async def test_monitor_save_failure_inhibits_control_and_warning(
    hass, runtime_factory, freezer, notifications, monkeypatch
):
    factory, commands = runtime_factory
    create, _ = notifications
    runtime = await setup_monitor(hass, factory)
    prior = runtime.return_monitor("roof")

    def fail(path, state):
        raise OSError("injected monitor save failure")

    monkeypatch.setattr(storage, "_write_snapshot", fail)
    await advance(hass, freezer, 300)
    assert runtime.fault
    assert runtime.return_monitor("roof") == prior
    assert create.call_count == 0
    # An existing durable target may retry before the warning write reports failure.
    # Once the fault is published, further retries and admissions must be inhibited.
    count = len(commands)
    await advance(hass, freezer, 60)
    assert len(commands) == count
    with pytest.raises(HomeAssistantError):
        await runtime.async_request("roof", target={"position": 0})


@pytest.mark.parametrize("edge", ["confirmation", "target_end"])
async def test_queued_transition_during_warning_save_never_submits_stale_notification(
    hass, runtime_factory, freezer, notifications, monkeypatch, edge
):
    factory, _ = runtime_factory
    create, _ = notifications
    hass.states.async_set("input_boolean.cold", "on")
    runtime = await setup_monitor(hass, factory, eligibility_entity="input_boolean.cold")
    entered, release = threading.Event(), threading.Event()
    real_write = storage._write_snapshot

    def gate(path, state):
        if state["return_monitors"]["roof"]["state"]["overdue"]:
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", gate)
    advancing = asyncio.create_task(advance(hass, freezer, 300))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if edge == "confirmation":
            reported(hass, position=7)
        else:
            hass.states.async_set("input_boolean.cold", "off")
        await asyncio.sleep(0)
        release.set()
        await advancing
        await hass.async_block_till_done()
        assert runtime.return_monitor("roof").phase == "idle"
        assert create.call_count == 0
        assert runtime._input_flush is None
    finally:
        release.set()
        await advancing


@pytest.mark.parametrize("quality", ["restored", "optimistic", "assumed_state"])
async def test_untrusted_raw_return_after_restart_does_not_clear_saved_warning(
    hass, runtime_factory, freezer, notifications, quality
):
    factory, _ = runtime_factory
    create, dismiss = notifications
    runtime = await setup_monitor(hass, factory)
    await advance(hass, freezer, 300)
    due = runtime.return_monitor("roof").due_at
    await runtime.async_close()
    reported(hass, position=7, **{quality: True})
    create.reset_mock()
    dismiss.reset_mock()
    fresh = OperatorRuntime(hass, runtime.entry)
    await fresh.async_start()
    try:
        await hass.async_block_till_done()
        assert fresh.return_monitor("roof").overdue
        assert fresh.return_monitor("roof").due_at == due
        assert create.call_count == 1
        assert "unavailable" in create.call_args.args[1]
        assert dismiss.call_count == 0
        reported(hass, position=7)
        await hass.async_block_till_done()
        assert fresh.return_monitor("roof").phase == "idle"
        assert dismiss.call_count == 1
    finally:
        await fresh.async_close()


async def test_removed_monitor_dismisses_existing_stable_warning(
    hass, runtime_factory, freezer, notifications
):
    factory, _ = runtime_factory
    create, dismiss = notifications
    runtime = await setup_monitor(hass, factory)
    await advance(hass, freezer, 300)
    warning_id = create.call_args.kwargs["notification_id"]
    await runtime.async_close()
    subentry = runtime.entry.subentries["roof"]
    data = dict(subentry.data)
    data.pop("return_monitor")
    hass.config_entries.async_update_subentry(runtime.entry, subentry, data=data)
    create.reset_mock()
    dismiss.reset_mock()
    fresh = OperatorRuntime(hass, runtime.entry)
    await fresh.async_start()
    try:
        await hass.async_block_till_done()
        assert fresh.store.state["return_monitors"] == {}
        assert create.call_count == 0
        assert dismiss.call_args.args == (hass, warning_id)
    finally:
        await fresh.async_close()


async def test_changed_monitor_settings_start_fresh_period_and_dismiss_old_warning(
    hass, runtime_factory, freezer, notifications
):
    from homeassistant.util import dt as dt_util

    factory, _ = runtime_factory
    create, dismiss = notifications
    runtime = await setup_monitor(hass, factory)
    await advance(hass, freezer, 300)
    old_due = runtime.return_monitor("roof").due_at
    warning_id = create.call_args.kwargs["notification_id"]
    await runtime.async_close()
    subentry = runtime.entry.subentries["roof"]
    settings = {**subentry.data["return_monitor"], "warning_after_seconds": 600}
    hass.config_entries.async_update_subentry(
        runtime.entry, subentry, data={**subentry.data, "return_monitor": settings}
    )
    fresh = OperatorRuntime(hass, runtime.entry)
    create.reset_mock()
    dismiss.reset_mock()
    await fresh.async_start()
    try:
        await hass.async_block_till_done()
        assert fresh.return_monitor("roof").due_at == dt_util.utcnow().timestamp() + 600
        assert fresh.return_monitor("roof").due_at != old_due
        assert not fresh.return_monitor("roof").overdue
        assert create.call_count == 0
        assert dismiss.call_args.args == (hass, warning_id)
    finally:
        await fresh.async_close()


async def test_restricted_return_stays_monitored_and_usable_store_device_fault_ends_it(
    hass, runtime_factory, freezer, notifications
):
    factory, _ = runtime_factory
    create, dismiss = notifications
    hass.states.async_set("input_boolean.restriction", "off")
    hass.states.async_set("input_boolean.device_fault", "off")
    runtime = await setup_monitor(
        hass,
        factory,
        restriction_entity="input_boolean.restriction",
        fault_entity="input_boolean.device_fault",
    )
    due = runtime.return_monitor("roof").due_at
    hass.states.async_set("input_boolean.restriction", "on")
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].status == "restricted"
    assert runtime.return_monitor("roof").due_at == due
    await advance(hass, freezer, 300)
    assert runtime.return_monitor("roof").overdue
    warning_id = create.call_args.kwargs["notification_id"]
    assert "rain" not in create.call_args.args[1]
    hass.states.async_set("input_boolean.device_fault", "on")
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].status == "fault"
    assert runtime.return_monitor("roof").phase == "idle"
    assert dismiss.call_args.args == (hass, warning_id)
