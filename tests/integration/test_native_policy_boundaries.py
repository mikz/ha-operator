"""Native admission boundaries protect accepted work and registration ownership."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import asyncio
import logging

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import trigger as native_trigger
from homeassistant.util import dt as dt_util

from custom_components.ha_operator.runtime import OperatorRuntime
from tests.integration.test_runtime_completion_pacing import relay_bundle  # noqa: F401
from tests.integration.test_runtime_reconciliation import (
    advance,  # noqa: F401
    reported,
    runtime_factory,  # noqa: F401
)

ADMIT = "operator_boundary_request"
CANCEL = "operator_boundary_cancel"


def policy(resource_id="roof", **overrides):
    return {
        "name": "Native boundary request",
        "kind": "occurrence",
        "resource_id": resource_id,
        "target": {"position": 100},
        "triggers": [{"trigger": "event", "event_type": ADMIT}],
        "cancel_triggers": [{"trigger": "event", "event_type": CANCEL}],
        "duration": {"seconds": 10},
        **overrides,
    }


async def fire(hass, event_type):
    hass.bus.async_fire(event_type)
    await hass.async_block_till_done()


def owned(runtime):
    return [item for item in runtime._state["occurrences"].values() if item.get("trigger_owned")]


async def test_rejected_admission_and_empty_cancel_do_not_interrupt_manual_relay(
    hass, runtime_factory, relay_bundle
):
    factory, _ = runtime_factory
    hass.states.async_set("binary_sensor.admit", "off")
    runtime = await factory(
        resources={"relay": relay_bundle},
        policies={
            "native": policy(
                "relay",
                target={"profile": "reverse"},
                conditions=[
                    {"condition": "state", "entity_id": "binary_sensor.admit", "state": "on"}
                ],
            )
        },
    )
    entered, release, confirmed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    journal = []

    async def command(call):
        journal.append((call.service, call.data["entity_id"]))
        if not entered.is_set():
            entered.set()
            await release.wait()
        hass.states.async_set(call.data["entity_id"], "on" if call.service == "turn_on" else "off")
        if call.service == "turn_on" and call.data["entity_id"] == "switch.outward":
            confirmed.set()

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    await runtime.async_set_mode("relay", "live")
    await hass.async_block_till_done()
    receipt = await runtime.async_request("relay", target={"profile": "reverse"}, indefinite=True)
    await asyncio.wait_for(entered.wait(), 1)
    try:
        hass.bus.async_fire(ADMIT)
        hass.bus.async_fire(CANCEL)
        # Deliver native callbacks while the independent physical service remains blocked.
        await asyncio.sleep(0)
        assert runtime.manual("relay").request_id == receipt["request_id"]
        assert not owned(runtime)
    finally:
        release.set()
    await asyncio.wait_for(confirmed.wait(), 2)
    await hass.async_block_till_done()
    assert journal == [
        *(("turn_off", output) for output in relay_bundle["outputs"]),
        ("turn_on", "switch.high"),
        ("turn_on", "switch.outward"),
    ]
    assert runtime.decisions["relay"].status == "satisfied"
    assert runtime.attempts["relay"] == 1


async def test_native_replacement_cancel_and_expiry_keep_one_slot_and_external_id(
    hass, runtime_factory, freezer
):
    reported(hass, position=100)
    factory, _ = runtime_factory
    runtime = await factory(policies={"native": policy()})
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    external = await runtime.async_submit_occurrence(
        "native", "external-id", dt_util.utcnow().timestamp() + 300
    )
    for index in range(15):
        await fire(hass, ADMIT)
        assert len(owned(runtime)) == 1
        assert len(runtime._state["occurrences"]) == 2
        if index % 3 == 0:
            await fire(hass, CANCEL)
            assert not owned(runtime)
            assert len(runtime._state["occurrences"]) == 1
    await advance(hass, freezer, 11)
    assert not owned(runtime)
    assert list(runtime._state["occurrences"].values()) == [external]


@pytest.mark.parametrize("boundary", ["observe", "disable", "reload"])
async def test_inactive_native_events_never_replay_on_return_to_live(
    hass, runtime_factory, boundary
):
    reported(hass, position=100)
    factory, commands = runtime_factory
    runtime = await factory(policies={"native": policy()})
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await fire(hass, ADMIT)
    assert len(owned(runtime)) == 1
    if boundary == "observe":
        await runtime.async_set_mode("roof", "observe")
    elif boundary == "disable":
        await runtime.async_set_policy_enabled("native", False)
    else:
        await runtime.async_close(withdraw_trigger_requests=True)
    await fire(hass, ADMIT)
    if boundary == "observe":
        await runtime.async_set_mode("roof", "live")
    elif boundary == "disable":
        await runtime.async_set_policy_enabled("native", True)
    else:
        runtime = OperatorRuntime(hass, runtime.entry)
        runtime.entry.runtime_data = runtime
        await runtime.async_start()
    try:
        await hass.async_block_till_done()
        assert not owned(runtime)
        assert not commands
        await fire(hass, ADMIT)
        assert len(owned(runtime)) == 1
    finally:
        if boundary == "reload":
            await runtime.async_close()


async def test_external_occurrence_conditions_are_admission_only(hass, runtime_factory, freezer):
    reported(hass)
    hass.states.async_set("binary_sensor.admit", "on")
    factory, commands = runtime_factory
    runtime = await factory(
        policies={
            "external": {
                "name": "Externally admitted",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 100},
                "conditions": [
                    {"condition": "state", "entity_id": "binary_sensor.admit", "state": "on"}
                ],
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    expiry = dt_util.utcnow().timestamp() + 300
    accepted = await runtime.async_submit_occurrence("external", "accepted", expiry)
    await hass.async_block_till_done()
    assert not accepted["skipped"]
    hass.states.async_set("binary_sensor.admit", "off")
    await advance(hass, freezer, 31)
    assert [call[1]["position"] for call in commands] == [100, 100]
    assert runtime.decisions["roof"].target.position == 100
    rejected = await runtime.async_submit_occurrence("external", "rejected", expiry)
    assert rejected["skipped"]
    assert runtime._state["occurrences"]['["external", "accepted"]']["expires_at"] == expiry


async def test_logged_partial_registration_detaches_real_native_listeners(
    hass, runtime_factory, monkeypatch
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory(policies={"native": policy()})
    original = native_trigger.async_initialize_triggers

    async def partial(*args, **kwargs):
        remove = await original(*args, **kwargs)
        args[5](logging.ERROR, "Injected attachment error after native listener registration")
        return remove

    monkeypatch.setattr(native_trigger, "async_initialize_triggers", partial)
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert not hass.bus.async_listeners().get(ADMIT)
    assert not hass.bus.async_listeners().get(CANCEL)
    await fire(hass, ADMIT)
    assert not commands and not owned(runtime)


async def test_cancelled_cleanup_drains_delayed_real_registration_and_removes_callbacks(
    hass, runtime_factory, monkeypatch
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory(policies={"native": policy()})
    original = native_trigger.async_initialize_triggers
    attached, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        remove = await original(*args, **kwargs)
        attached.set()
        await release.wait()
        return remove

    monkeypatch.setattr(native_trigger, "async_initialize_triggers", delayed)
    await runtime.async_set_mode("roof", "live")
    await asyncio.wait_for(attached.wait(), 1)
    closing = asyncio.create_task(runtime.async_close())
    try:
        await asyncio.sleep(0)
        closing.cancel()
        hass.bus.async_fire(ADMIT)
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await hass.async_block_till_done()
    assert not hass.bus.async_listeners().get(ADMIT)
    assert not hass.bus.async_listeners().get(CANCEL)
    assert not commands and not owned(runtime)


@pytest.mark.parametrize("boundary", ["observe", "disable"])
async def test_normal_lifecycle_close_during_attach_cleans_without_repair(
    hass, runtime_factory, monkeypatch, boundary
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory(policies={"native": policy()})
    original = native_trigger.async_initialize_triggers
    attached, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        remove = await original(*args, **kwargs)
        attached.set()
        await release.wait()
        return remove

    monkeypatch.setattr(native_trigger, "async_initialize_triggers", delayed)
    await runtime.async_set_mode("roof", "live")
    await asyncio.wait_for(attached.wait(), 1)
    try:
        if boundary == "observe":
            await runtime.async_set_mode("roof", "observe")
        else:
            await runtime.async_set_policy_enabled("native", False)
        hass.bus.async_fire(ADMIT)
        await asyncio.sleep(0)
    finally:
        release.set()
    await hass.async_block_till_done()
    assert not hass.bus.async_listeners().get(ADMIT)
    assert not hass.bus.async_listeners().get(CANCEL)
    assert not commands and not owned(runtime)
    assert not [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == "ha_operator" and issue.translation_key.startswith("policy_trigger")
    ]


@pytest.mark.parametrize("registration_fails", [False, True])
async def test_restored_request_waits_for_both_registrations_and_stays_inhibited_on_failure(
    hass, runtime_factory, monkeypatch, registration_fails
):
    reported(hass, position=100)
    factory, commands = runtime_factory
    previous = await factory(policies={"native": policy()})
    await previous.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await fire(hass, ADMIT)
    accepted = owned(previous)[0].copy()
    await previous.async_close()
    reported(hass, position=0)
    original = native_trigger.async_initialize_triggers
    attached, release = asyncio.Event(), asyncio.Event()
    registrations = 0

    async def delayed_cancel_registration(*args, **kwargs):
        nonlocal registrations
        remove = await original(*args, **kwargs)
        registrations += 1
        if registrations == 2:
            attached.set()
            await release.wait()
            if registration_fails:
                args[5](logging.ERROR, "Injected cancellation registration failure")
        return remove

    monkeypatch.setattr(native_trigger, "async_initialize_triggers", delayed_cancel_registration)
    recovered = OperatorRuntime(hass, previous.entry)
    previous.entry.runtime_data = recovered
    starting = asyncio.create_task(recovered.async_start())
    try:
        await asyncio.wait_for(attached.wait(), 1)
        assert owned(recovered) == [accepted]
        assert recovered.decisions["roof"].target is None
        assert not commands
        # Admission is registered already, but neither callback may run before full readiness.
        hass.bus.async_fire(ADMIT)
        hass.bus.async_fire(CANCEL)
        await asyncio.sleep(0)
        assert owned(recovered) == [accepted]
        assert not commands
        release.set()
        await starting
        await hass.async_block_till_done()
        if registration_fails:
            await recovered.async_reconcile("roof")
            await fire(hass, ADMIT)
            assert recovered.decisions["roof"].target is None
            assert not commands
            assert not hass.bus.async_listeners().get(ADMIT)
            assert not hass.bus.async_listeners().get(CANCEL)
        else:
            assert [call[1]["position"] for call in commands] == [100]
            assert owned(recovered) == [accepted]
    finally:
        release.set()
        await starting
        await recovered.async_close()


@pytest.mark.parametrize("inactive", ["shadow", "observe", "disabled", "no_triggers"])
async def test_cold_start_retires_inactive_saved_request_before_an_owner_exists(
    hass, runtime_factory, inactive
):
    reported(hass, position=100)
    factory, commands = runtime_factory
    previous = await factory(policies={"native": policy()})
    await previous.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await fire(hass, ADMIT)
    accepted = owned(previous)[0].copy()
    await previous.async_close()
    saved = await previous._store.async_load()
    assert accepted in saved["occurrences"].values()
    entry = previous.entry
    if inactive == "shadow":
        hass.config_entries.async_update_entry(entry, options={"shadow_lock": True})
    elif inactive in {"observe", "disabled"}:
        # Modify only a lifecycle flag in the actual runtime-generated Store snapshot.
        if inactive == "observe":
            saved["modes"]["roof"] = "observe"
        else:
            saved["policy_enabled"]["native"] = False
        await previous._store.async_save(saved)
    else:
        external_only = {
            key: value
            for key, value in entry.subentries["native"].data.items()
            if key not in {"triggers", "cancel_triggers", "duration"}
        }
        hass.config_entries.async_update_subentry(
            entry, entry.subentries["native"], data=external_only
        )
    reported(hass, position=0)
    recovered = OperatorRuntime(hass, entry)
    entry.runtime_data = recovered
    try:
        await recovered.async_start()
        await hass.async_block_till_done()
        assert not owned(recovered)
        assert not commands
        assert not hass.bus.async_listeners().get(ADMIT)
        await fire(hass, ADMIT)
        assert not owned(recovered)
        if inactive == "disabled":
            await recovered.async_set_policy_enabled("native", True)
        elif inactive == "observe":
            await recovered.async_set_mode("roof", "live")
        else:
            await recovered.async_close()
            if inactive == "shadow":
                hass.config_entries.async_update_entry(entry, options={"shadow_lock": False})
            else:
                hass.config_entries.async_update_subentry(
                    entry, entry.subentries["native"], data=policy()
                )
            recovered = OperatorRuntime(hass, entry)
            entry.runtime_data = recovered
            await recovered.async_start()
            await recovered.async_set_mode("roof", "live")
        await hass.async_block_till_done()
        assert not commands and not owned(recovered)
        await fire(hass, ADMIT)
        assert len(owned(recovered)) == 1
        assert owned(recovered)[0]["occurrence_id"] != accepted["occurrence_id"]
        assert recovered.decisions["roof"].target.position == 100
    finally:
        await recovered.async_close()
