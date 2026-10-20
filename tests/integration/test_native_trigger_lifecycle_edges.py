"""Native registration epochs settle correctly across validation and Repair edges."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import asyncio
import logging

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import trigger as native_trigger

from custom_components.ha_operator.const import DOMAIN
from tests.integration.test_native_policy_boundaries import ADMIT, CANCEL, fire, owned, policy
from tests.integration.test_runtime_reconciliation import reported, runtime_factory  # noqa: F401


def trigger_issues(hass):
    return [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key.startswith("policy_trigger")
    ]


async def test_cancelled_owner_close_drains_native_validation_without_attaching(
    hass, runtime_factory, monkeypatch
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory(policies={"native": policy()})
    original = native_trigger.async_validate_trigger_config
    validated, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        validated.set()
        await release.wait()
        return result

    monkeypatch.setattr(native_trigger, "async_validate_trigger_config", delayed)
    await runtime.async_set_mode("roof", "live")
    await asyncio.wait_for(validated.wait(), 1)
    owner = runtime._trigger_owners["native"]
    closing = asyncio.create_task(owner.async_close())
    try:
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await hass.async_block_till_done()
    assert owner.closed and not owner.ready
    assert not hass.bus.async_listeners().get(ADMIT)
    assert not hass.bus.async_listeners().get(CANCEL)
    await fire(hass, ADMIT)
    assert not commands and not owned(runtime)
    assert not trigger_issues(hass)


async def test_validation_failure_after_runtime_close_does_not_create_spurious_repair(
    hass, runtime_factory, monkeypatch
):
    reported(hass)
    factory, commands = runtime_factory
    runtime = await factory(policies={"native": policy()})
    original = native_trigger.async_validate_trigger_config
    validated, release = asyncio.Event(), asyncio.Event()

    async def delayed_failure(*args, **kwargs):
        await original(*args, **kwargs)
        validated.set()
        await release.wait()
        raise ValueError("Native validation failure reached a closing runtime")

    monkeypatch.setattr(native_trigger, "async_validate_trigger_config", delayed_failure)
    await runtime.async_set_mode("roof", "live")
    await asyncio.wait_for(validated.wait(), 1)
    closing = asyncio.create_task(runtime.async_close())
    try:
        await asyncio.sleep(0)
        assert runtime._closed and not closing.done()
    finally:
        release.set()
    assert await closing
    await hass.async_block_till_done()
    assert not trigger_issues(hass)
    assert not hass.bus.async_listeners().get(ADMIT)
    assert not commands


@pytest.mark.parametrize("incomplete_cleanup", [False, True])
async def test_successful_new_epoch_clears_only_transient_setup_repair(
    hass, runtime_factory, monkeypatch, incomplete_cleanup
):
    reported(hass, position=100)
    factory, _ = runtime_factory
    runtime = await factory(policies={"native": policy()})
    original = native_trigger.async_initialize_triggers
    harness_removers = []

    async def fail(*args, **kwargs):
        remove = await original(*args, **kwargs)
        if incomplete_cleanup:
            harness_removers.append(remove)
            raise RuntimeError("Native attachment failed before returning its remover")
        args[5](logging.ERROR, "Native attachment reported a recoverable setup failure")
        return remove

    monkeypatch.setattr(native_trigger, "async_initialize_triggers", fail)
    try:
        await runtime.async_set_mode("roof", "live")
        await hass.async_block_till_done()
        expected = "policy_trigger_cleanup" if incomplete_cleanup else "policy_trigger_setup"
        assert [issue.translation_key for issue in trigger_issues(hass)] == [expected]
        await fire(hass, ADMIT)
        assert not owned(runtime)
        monkeypatch.setattr(native_trigger, "async_initialize_triggers", original)
        await runtime.async_set_mode("roof", "observe")
        await runtime.async_set_mode("roof", "live")
        await hass.async_block_till_done()
        assert [issue.translation_key for issue in trigger_issues(hass)] == (
            ["policy_trigger_cleanup"] if incomplete_cleanup else []
        )
        await fire(hass, ADMIT)
        assert len(owned(runtime)) == 1
    finally:
        for remove in harness_removers:
            if remove:
                remove()
