"""Approved native timer differences and callback context are observable behavior."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import pytest
from homeassistant.core import Context
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import trigger as native_trigger
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.ha_operator.runtime import OperatorRuntime
from tests.integration.test_native_policy_boundaries import ADMIT, CANCEL, fire, owned, policy
from tests.integration.test_runtime_reconciliation import (
    advance,  # noqa: F401
    reported,
    runtime_factory,  # noqa: F401
)


@pytest.fixture(autouse=True)
async def cleanup_native_timer(hass):
    yield
    if hass.services.has_service("timer", "cancel"):
        await timer_service(hass, "cancel")


async def timer_service(hass, service, **data):
    await hass.services.async_call(
        "timer", service, {"entity_id": "timer.native_request", **data}, blocking=True
    )
    await hass.async_block_till_done()


async def timer_runtime(hass, runtime_factory):
    assert await async_setup_component(
        hass, "timer", {"timer": {"native_request": {"duration": "01:00:00"}}}
    )
    reported(hass, position=100)
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "native": policy(
                triggers=[
                    {
                        "trigger": "state",
                        "entity_id": "timer.native_request",
                        "to": "active",
                        "for": "00:01:00",
                    }
                ],
                cancel_triggers=[
                    {
                        "trigger": "event",
                        "event_type": ["timer.paused", "timer.cancelled"],
                        "event_data": {"entity_id": "timer.native_request"},
                    }
                ],
                duration={"minutes": 30},
            )
        }
    )
    await hass.async_start()
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    return runtime


@pytest.mark.parametrize("ending", ["early_finish", "accepted_finish", "pause", "cancel"])
async def test_native_timer_qualification_and_lifecycle_withdrawal(
    hass, runtime_factory, freezer, ending
):
    runtime = await timer_runtime(hass, runtime_factory)
    await timer_service(hass, "start")
    await advance(hass, freezer, 59)
    assert not owned(runtime)
    if ending == "early_finish":
        await timer_service(hass, "finish")
        await advance(hass, freezer, 2)
        assert not owned(runtime)
        return
    await advance(hass, freezer, 1)
    accepted = owned(runtime)[0].copy()
    assert accepted["expires_at"] == pytest.approx(dt_util.utcnow().timestamp() + 1800)
    if ending == "accepted_finish":
        await timer_service(hass, "finish")
        assert owned(runtime) == [accepted]
        await advance(hass, freezer, 1800)
        assert not owned(runtime)
    else:
        await timer_service(hass, ending)
        assert not owned(runtime)


async def test_native_active_restart_does_not_reset_wait_and_resume_requalifies(
    hass, runtime_factory, freezer
):
    runtime = await timer_runtime(hass, runtime_factory)
    await timer_service(hass, "start")
    await advance(hass, freezer, 30)
    await timer_service(hass, "start", duration="00:20:00")
    await advance(hass, freezer, 30)
    assert len(owned(runtime)) == 1
    first = owned(runtime)[0]["occurrence_id"]
    await timer_service(hass, "pause")
    assert not owned(runtime)
    await timer_service(hass, "start")
    await advance(hass, freezer, 59)
    assert not owned(runtime)
    await advance(hass, freezer, 1)
    assert len(owned(runtime)) == 1
    assert owned(runtime)[0]["occurrence_id"] != first


async def test_pending_native_wait_is_lost_on_reload_but_accepted_shutdown_expiry_survives(
    hass, runtime_factory, freezer
):
    runtime = await timer_runtime(hass, runtime_factory)
    await timer_service(hass, "start")
    await advance(hass, freezer, 30)
    await runtime.async_close(withdraw_trigger_requests=True)
    entry = runtime.entry
    runtime = OperatorRuntime(hass, entry)
    entry.runtime_data = runtime
    try:
        await runtime.async_start()
        await advance(hass, freezer, 61)
        assert not owned(runtime)
        await timer_service(hass, "pause")
        await timer_service(hass, "start")
        await advance(hass, freezer, 60)
        accepted = owned(runtime)[0].copy()
        await runtime.async_close()
        await advance(hass, freezer, 30)
        runtime = OperatorRuntime(hass, entry)
        entry.runtime_data = runtime
        await runtime.async_start()
        await hass.async_block_till_done()
        assert owned(runtime) == [accepted]
        remaining = accepted["expires_at"] - dt_util.utcnow().timestamp()
        await advance(hass, freezer, remaining)
        assert not owned(runtime)
    finally:
        await runtime.async_close()


async def test_callback_conditions_and_publication_receive_real_trigger_context(
    hass, runtime_factory
):
    reported(hass, position=100)
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "native": policy(
                triggers=[
                    {"trigger": "event", "event_type": ADMIT, "id": "accepted"},
                    {"trigger": "event", "event_type": "operator_other_request", "id": "rejected"},
                ],
                conditions=[
                    {"condition": "trigger", "id": "accepted"},
                    {
                        "condition": "template",
                        "value_template": "{{ trigger.event.data.requested == 'quiet' }}",
                    },
                ],
            )
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await fire(hass, "operator_other_request")
    assert not owned(runtime)
    contexts = []
    remove = runtime.subscribe(lambda: contexts.append(runtime.publication_context("roof")))
    context = Context()
    try:
        hass.bus.async_fire(ADMIT, {"requested": "quiet"}, context=context)
        await hass.async_block_till_done()
        assert len(owned(runtime)) == 1
        assert any(candidate is context for candidate in contexts)
    finally:
        remove()


async def test_all_disabled_native_triggers_are_valid_and_attach_no_listeners(
    hass, runtime_factory
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory(
        policies={
            "native": policy(
                triggers=[{"trigger": "event", "event_type": ADMIT, "enabled": False}],
                cancel_triggers=[{"trigger": "event", "event_type": CANCEL, "enabled": False}],
            )
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert not hass.bus.async_listeners().get(ADMIT)
    assert not hass.bus.async_listeners().get(CANCEL)
    await fire(hass, ADMIT)
    assert not owned(runtime) and not commands
    assert not [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == "ha_operator" and issue.translation_key.startswith("policy_trigger")
    ]


async def test_native_pre_remover_exception_fences_leaked_callback_and_reports_cleanup(
    hass, runtime_factory, monkeypatch
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory(policies={"native": policy()})
    original = native_trigger.async_initialize_triggers
    harness_removers = []

    async def fail_before_return(*args, **kwargs):
        harness_removers.append(await original(*args, **kwargs))
        raise RuntimeError("Injected platform lookup failure before native remover return")

    monkeypatch.setattr(native_trigger, "async_initialize_triggers", fail_before_return)
    try:
        await runtime.async_set_mode("roof", "live")
        await hass.async_block_till_done()
        assert hass.bus.async_listeners().get(ADMIT) == 1
        await fire(hass, ADMIT)
        assert not commands and not owned(runtime)
        issues = [
            issue
            for (domain, _), issue in ir.async_get(hass).issues.items()
            if domain == "ha_operator"
        ]
        assert any(issue.translation_key == "policy_trigger_cleanup" for issue in issues)
        await runtime.async_close()
        await fire(hass, ADMIT)
        assert not commands and not owned(runtime)
    finally:
        # Only the fault-injection harness has the remover that production never received.
        for remove in harness_removers:
            if remove:
                remove()
