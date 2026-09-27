"""Long actuator transactions leave time for independent physical feedback."""

# ruff: noqa: F811 - imported pytest fixture is intentionally injected by name

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError

from tests.integration.test_runtime_reconciliation import runtime_factory  # noqa: F401


@pytest.fixture
def relay_bundle(hass):
    outputs = [f"switch.{name}" for name in ("low", "high", "inward", "outward")]
    for output in outputs:
        hass.states.async_set(output, "off")

    def profile(active, percentage, direction):
        return {
            "outputs": {output: output in active for output in outputs},
            "percentage": percentage,
            "direction": direction,
        }

    return {
        "name": "Independent relay bundle",
        "kind": "relay_fan",
        "outputs": outputs,
        "profiles": {
            "off": profile((), 0, "forward"),
            "forward": profile(("switch.low", "switch.inward"), 50, "forward"),
            "reverse": profile(("switch.high", "switch.outward"), 100, "reverse"),
        },
        "default_target": {"profile": "forward"},
        "retry_interval": 0.1,
        "command_interval": 0.02,
        "reversal_dead_time": 0.15,
        "movement_timeout": 1,
    }


async def test_long_relay_sequence_keeps_complete_vector_until_delayed_feedback(
    hass, runtime_factory, relay_bundle
):
    factory, _ = runtime_factory
    runtime = await factory(resources={"relay": relay_bundle})
    physical = dict.fromkeys(relay_bundle["outputs"], False)
    expected = relay_bundle["profiles"]["reverse"]["outputs"]
    journal, reporters = [], []
    feedback = asyncio.Event()

    async def report_physical():
        await asyncio.sleep(0.02)
        for output, on in physical.items():
            hass.states.async_set(output, "on" if on else "off")
        feedback.set()

    async def command(call):
        output = call.data["entity_id"]
        journal.append((call.service, output))
        physical[output] = call.service == "turn_on"
        if call.service == "turn_off":
            hass.states.async_set(output, "off")
        elif physical == expected and not reporters:
            # Actual physical state is sampled later, independently of receipts.
            reporters.append(asyncio.create_task(report_physical()))

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    await runtime.async_set_mode("relay", "live")
    await runtime.async_request("relay", target={"profile": "reverse"}, indefinite=True)
    try:
        await asyncio.wait_for(feedback.wait(), 2)
        await hass.async_block_till_done()
        assert physical == expected
        assert runtime.decisions["relay"].status == "satisfied"
        assert runtime.attempts["relay"] == 1
        assert journal == [
            *(("turn_off", output) for output in relay_bundle["outputs"]),
            ("turn_on", "switch.high"),
            ("turn_on", "switch.outward"),
        ]
        assert "relay" not in runtime.next_attempts
        assert "relay" not in runtime._timers
    finally:
        await asyncio.gather(*reporters)


async def test_partial_relay_error_waits_after_completion_then_recovers(
    hass, runtime_factory, relay_bundle
):
    factory, _ = runtime_factory
    runtime = await factory(resources={"relay": relay_bundle})
    physical = dict.fromkeys(relay_bundle["outputs"], False)
    expected = relay_bundle["profiles"]["reverse"]["outputs"]
    journal = []
    failed, recovered = asyncio.Event(), asyncio.Event()
    failure_time = None

    async def command(call):
        nonlocal failure_time
        output = call.data["entity_id"]
        now = asyncio.get_running_loop().time()
        journal.append((call.service, output, now))
        if call.service == "turn_on" and output == "switch.outward" and not failed.is_set():
            failure_time = now
            failed.set()
            raise HomeAssistantError("Independent transport failure")
        physical[output] = call.service == "turn_on"
        # This path confirms each subsequent physical effect synchronously.
        hass.states.async_set(output, "on" if physical[output] else "off")
        if physical == expected:
            recovered.set()

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    await runtime.async_set_mode("relay", "live")
    await runtime.async_request("relay", target={"profile": "reverse"}, indefinite=True)
    await asyncio.wait_for(failed.wait(), 2)
    await asyncio.wait_for(recovered.wait(), 2)
    await hass.async_block_till_done()
    first_retry = next(item for item in journal if item[0] == "turn_off" and item[2] > failure_time)
    assert first_retry[2] - failure_time >= relay_bundle["retry_interval"]
    assert runtime.decisions["relay"].status == "satisfied"
    assert runtime.attempts["relay"] == 2
    assert physical == expected


async def test_obsolete_completion_paces_new_intent_without_replaying_old_target(
    hass, runtime_factory, relay_bundle
):
    factory, _ = runtime_factory
    relay_bundle.update(retry_interval=0.02, command_interval=0.05, reversal_dead_time=0.001)
    runtime = await factory(resources={"relay": relay_bundle})
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    journal = []
    original_completed = None

    async def command(call):
        nonlocal original_completed
        output = call.data["entity_id"]
        if not entered.is_set():
            entered.set()
            await release.wait()
            original_completed = asyncio.get_running_loop().time()
        journal.append((call.service, output, asyncio.get_running_loop().time()))
        hass.states.async_set(output, "on" if call.service == "turn_on" else "off")
        if output == "switch.inward" and call.service == "turn_on":
            finished.set()

    hass.services.async_register("switch", "turn_on", command)
    hass.services.async_register("switch", "turn_off", command)
    await runtime.async_set_mode("relay", "live")
    await runtime.async_request("relay", target={"profile": "reverse"}, indefinite=True)
    await asyncio.wait_for(entered.wait(), 2)
    # The old transport consumes its original pacing budget while a new intent waits.
    await asyncio.sleep(0.06)
    await runtime.async_request("relay", target={"profile": "forward"}, indefinite=True)
    release.set()
    await asyncio.wait_for(finished.wait(), 2)
    await hass.async_block_till_done()
    assert journal[1][2] - original_completed >= relay_bundle["command_interval"]
    assert not any(
        service == "turn_on" and output in {"switch.high", "switch.outward"}
        for service, output, _ in journal
    )
    assert runtime.decisions["relay"].status == "satisfied"
    assert runtime.manual("relay").target.profile == "forward"


async def test_cancelled_slow_relay_sequence_stays_cancelled_after_transport_drains(
    hass, runtime_factory, relay_bundle
):
    factory, _ = runtime_factory
    runtime = await factory(resources={"relay": relay_bundle})
    entered, release = asyncio.Event(), asyncio.Event()
    journal = []

    async def command(call):
        entered.set()
        await release.wait()
        journal.append((call.service, call.data["entity_id"]))

    hass.services.async_register("switch", "turn_off", command)
    hass.services.async_register("switch", "turn_on", command)
    await runtime.async_set_mode("relay", "live")
    await runtime.async_request("relay", target={"profile": "reverse"}, indefinite=True)
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(runtime.async_close())
    try:
        await asyncio.sleep(0.01)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 2)
        assert runtime._tasks[0].cancelled()
        assert journal == [("turn_off", "switch.low")]
        assert runtime._timers == {}
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
