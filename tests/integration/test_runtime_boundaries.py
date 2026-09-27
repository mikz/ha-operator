"""Physical transport ordering at STOP and runtime teardown boundaries."""

# ruff: noqa: F811 - imported pytest fixture is intentionally injected by name

import asyncio
import threading

import pytest

from tests.integration.test_runtime_reconciliation import reported, runtime_factory  # noqa: F401


async def test_stop_fences_an_inflight_delayed_device_command(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    started, release = asyncio.Event(), asyncio.Event()
    stopped = asyncio.Event()
    journal = []

    async def delayed_open(call):
        started.set()
        await release.wait()
        journal.append("open")
        reported(hass, position=100)

    async def stop(call):
        journal.append("stop")
        reported(hass, position=0)
        stopped.set()

    hass.services.async_register("cover", "set_cover_position", delayed_open)
    hass.services.async_register("cover", "stop_cover", stop)
    await runtime.async_request("roof", target={"position": 100}, indefinite=True)
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(runtime.async_stop("roof"))
    try:
        await asyncio.wait_for(stopped.wait(), 1)
        assert journal == ["stop"]  # Emergency STOP is immediate, before transport drains.
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 1)
        await hass.async_block_till_done()
        assert journal == ["stop", "open", "stop"]
        assert runtime.manual("roof").mode == "hands_off"
        assert runtime.observations["roof"].target.position == 0
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_unload_waits_for_physical_executor_call_to_finish(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    journal = []

    def blocking_open(call):
        started.set()
        release.wait(timeout=3)
        journal.append("open")
        finished.set()

    hass.services.async_register("cover", "set_cover_position", blocking_open)
    await runtime.async_request("roof", target={"position": 100}, indefinite=True)
    assert await asyncio.to_thread(started.wait, 1)
    closing = asyncio.create_task(runtime.async_close())
    try:
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(closing, 1)
        assert finished.is_set()
        assert journal == ["open"]
        assert all(task.done() for task in runtime._tasks)
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("optimistic", [True, False])
async def test_requirement_evidence_rejects_declared_optimistic_state(
    hass, runtime_factory, optimistic
):
    factory, _ = runtime_factory
    hass.states.async_set("binary_sensor.extracting", "on")
    hass.states.async_set("binary_sensor.inlet", "on", {"optimistic": optimistic})
    runtime = await factory(
        resources={},
        requirements={
            "air": {
                "name": "Air",
                "activation_entities": ["binary_sensor.extracting"],
                "providers": [
                    {
                        "id": "passive",
                        "evidence": [
                            {
                                "entity_id": "binary_sensor.inlet",
                                "operator": "eq",
                                "value": "on",
                                "kind": "contact",
                            }
                        ],
                    }
                ],
            }
        },
    )
    assert (runtime.requirement_results["air"].status == "satisfied") is not optimistic


async def test_cancelled_stop_still_fences_the_original_inflight_command(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    journal = []

    async def delayed_open(call):
        started.set()
        await release.wait()
        journal.append("open")
        reported(hass, position=100)

    async def stop(call):
        journal.append("stop")
        reported(hass, position=0)
        stopped.set()

    hass.services.async_register("cover", "set_cover_position", delayed_open)
    hass.services.async_register("cover", "stop_cover", stop)
    await runtime.async_request("roof", target={"position": 100}, indefinite=True)
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(runtime.async_stop("roof"))
    try:
        await asyncio.wait_for(stopped.wait(), 1)
        # Wait for STOP persistence and its final barrier without draining the
        # intentionally held physical service through async_block_till_done.
        for _ in range(100):
            if runtime._state["manuals"].get("roof", {}).get("source") == "stop":
                break
            await asyncio.sleep(0.001)
        assert runtime._state["manuals"]["roof"]["source"] == "stop"
        stopping.cancel()
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 1)
        assert journal == ["stop", "open", "stop"]
        assert runtime.manual("roof").mode == "hands_off"
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_new_manual_lease_supersedes_old_stop_final_barrier(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    journal = []

    async def delayed_open(call):
        started.set()
        await release.wait()
        journal.append("open")
        reported(hass, position=100)

    async def stop(call):
        journal.append("stop")
        reported(hass, position=0)
        stopped.set()

    hass.services.async_register("cover", "set_cover_position", delayed_open)
    hass.services.async_register("cover", "stop_cover", stop)
    await runtime.async_request("roof", target={"position": 100}, indefinite=True)
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(runtime.async_stop("roof"))
    try:
        await asyncio.wait_for(stopped.wait(), 1)
        for _ in range(100):
            if runtime._state["manuals"].get("roof", {}).get("source") == "stop":
                break
            await asyncio.sleep(0.001)
        assert runtime._state["manuals"]["roof"]["source"] == "stop"
        receipt = await runtime.async_request("roof", target={"position": 40}, indefinite=True)
        release.set()
        await asyncio.wait_for(stopping, 1)
        assert journal == ["stop", "open"]
        assert runtime.manual("roof").request_id == receipt["request_id"]
        assert runtime.manual("roof").target.position == 40
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_unload_waits_for_an_inflight_stop_operation(hass, runtime_factory):
    factory, _ = runtime_factory
    reported(hass)
    runtime = await factory()
    await runtime.async_set_mode("roof", "live")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    journal = []

    def blocking_stop(call):
        started.set()
        release.wait(timeout=3)
        journal.append("stop")
        finished.set()

    hass.services.async_register("cover", "stop_cover", blocking_stop)
    stopping = asyncio.create_task(runtime.async_stop("roof"))
    closing = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        closing = asyncio.create_task(runtime.async_close())
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(stopping, closing), 1)
        assert finished.is_set()
        assert journal == ["stop"]
        await hass.async_block_till_done()
        assert journal == ["stop"]
        assert not runtime._stop_tasks
    finally:
        release.set()
        await asyncio.gather(stopping, *([closing] if closing else []), return_exceptions=True)
