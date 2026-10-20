"""Cancelled integration startup retains native attachment until it can be removed."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import asyncio

import pytest
from homeassistant.helpers import trigger as native_trigger

from custom_components.ha_operator import async_setup_entry
from tests.integration.test_native_policy_boundaries import ADMIT, CANCEL, fire, owned, policy
from tests.integration.test_native_trigger_lifecycle_edges import trigger_issues
from tests.integration.test_runtime_reconciliation import reported, runtime_factory  # noqa: F401


@pytest.mark.parametrize("cancel_startup", [False, True])
async def test_startup_retains_delayed_native_registration_until_completion(
    hass, runtime_factory, monkeypatch, cancel_startup
):
    reported(hass)
    factory, commands = runtime_factory
    previous = await factory(policies={"native": policy()})
    await previous.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await previous.async_close()
    original = native_trigger.async_initialize_triggers
    attached, release = asyncio.Event(), asyncio.Event()
    registration_tasks = []

    async def delayed(*args, **kwargs):
        remove = await original(*args, **kwargs)
        if not attached.is_set():
            registration_tasks.append(asyncio.current_task())
            attached.set()
            await release.wait()
        return remove

    monkeypatch.setattr(native_trigger, "async_initialize_triggers", delayed)
    starting = asyncio.create_task(async_setup_entry(hass, previous.entry))
    try:
        await asyncio.wait_for(attached.wait(), 2)
        runtime = previous.entry.runtime_data
        assert hass.bus.async_listeners().get(ADMIT) == 1
        if cancel_startup:
            starting.cancel()
            await asyncio.sleep(0)
            assert runtime._closed
            # Repeated setup cancellation cannot cancel the registration's owned task.
            starting.cancel()
            await asyncio.sleep(0)
            assert not starting.done()
            assert not registration_tasks[0].done()
            hass.bus.async_fire(ADMIT)
            await asyncio.sleep(0)
            assert not owned(runtime) and not commands
        release.set()
        if cancel_startup:
            with pytest.raises(asyncio.CancelledError):
                await starting
            assert runtime._closed
            assert not hass.bus.async_listeners().get(ADMIT)
            assert not hass.bus.async_listeners().get(CANCEL)
            assert not runtime._trigger_epochs
            assert not runtime._unsubscribers and not runtime._tasks
            assert not trigger_issues(hass)
            await fire(hass, ADMIT)
            assert not owned(runtime) and not commands
        else:
            assert await starting
            assert runtime._trigger_owners["native"].ready
            await fire(hass, ADMIT)
            assert len(owned(runtime)) == 1
    finally:
        release.set()
        if not starting.done():
            await starting
        await previous.entry.runtime_data.async_close()
