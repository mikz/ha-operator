"""Native occurrence changes fence selected execution without interrupting manual work."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import asyncio

import pytest

from tests.integration.test_native_policy_boundaries import ADMIT, CANCEL, fire, owned, policy
from tests.integration.test_runtime_completion_pacing import relay_bundle  # noqa: F401
from tests.integration.test_runtime_reconciliation import reported, runtime_factory  # noqa: F401


@pytest.mark.parametrize("event_type", [ADMIT, CANCEL])
@pytest.mark.parametrize("manual_profile", ["reverse", "off"])
async def test_unselected_occurrence_reentry_does_not_interrupt_manual_relay_commands(
    hass, runtime_factory, relay_bundle, event_type, manual_profile
):
    factory, _ = runtime_factory
    bundle = {**relay_bundle, "retry_interval": 30}
    initial = "reverse" if manual_profile == "off" else "off"
    for entity_id, on in bundle["profiles"][initial]["outputs"].items():
        hass.states.async_set(entity_id, "on" if on else "off")
    runtime = await factory(
        resources={"relay": bundle},
        policies={"p": policy("relay", target={"profile": initial})},
    )
    await runtime.async_set_mode("relay", "live")
    await hass.async_block_till_done()
    if event_type == CANCEL:
        await fire(hass, ADMIT)
        assert len(owned(runtime)) == 1
    expected = [("turn_off", output) for output in bundle["outputs"]]
    if manual_profile == "reverse":
        expected.extend([("turn_on", "switch.high"), ("turn_on", "switch.outward")])
    journal = []
    finished = asyncio.Event()

    async def command(call):
        journal.append((call.service, call.data["entity_id"]))
        generation = runtime._generation["relay"]
        # Real native callback reentry occurs at every physical command await.
        hass.bus.async_fire(event_type)
        await asyncio.sleep(0)
        assert runtime._generation["relay"] == generation
        assert runtime.decisions["relay"].source == "manual:service"
        hass.states.async_set(call.data["entity_id"], "on" if call.service == "turn_on" else "off")
        if len(journal) == len(expected):
            finished.set()

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    await runtime.async_request("relay", target={"profile": manual_profile}, indefinite=True)
    await asyncio.wait_for(finished.wait(), 2)
    await hass.async_block_till_done()
    assert journal == expected
    assert runtime.attempts["relay"] == 1
    assert runtime.decisions["relay"].status == "satisfied"
    assert all(
        hass.states.is_state(entity_id, "on" if on else "off")
        for entity_id, on in bundle["profiles"][manual_profile]["outputs"].items()
    )
    assert bool(owned(runtime)) is (event_type == ADMIT)


async def test_selected_same_target_replacement_and_cancellation_fence_occurrence_identity(
    hass, runtime_factory
):
    reported(hass, position=100)
    factory, commands = runtime_factory
    runtime = await factory(policies={"p": policy()})
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    await fire(hass, ADMIT)
    first = owned(runtime)[0]
    generation = runtime._generation["roof"]
    await fire(hass, ADMIT)
    replacement = owned(runtime)[0]
    assert replacement["target"] == first["target"]
    assert replacement["occurrence_id"] != first["occurrence_id"]
    assert runtime._generation["roof"] == generation + 1
    await fire(hass, CANCEL)
    assert not owned(runtime)
    assert runtime._generation["roof"] == generation + 2
    assert not commands
