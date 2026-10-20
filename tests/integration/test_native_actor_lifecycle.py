"""Independently configured Template actors retain native action and state ownership."""

import asyncio

import pytest
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from tests.integration.helpers import async_setup_operator

STARTED = "native_actor_action_started"
COMPLETED = "native_actor_action_completed"
RELEASE = "native_actor_action_release"


async def native_actor(hass, kind):
    """Configure a real Template actor, native wait action, and independent reader."""
    feedback = "sensor.independent_actor_feedback"
    initial, first, second = ("idle", "quiet", "boost") if kind == "select" else ("0", 10, 20)
    hass.states.async_set(feedback, initial)
    variable = "option" if kind == "select" else "value"
    actor = {
        "name": "Independent native actor",
        "unique_id": "independent_native_actor",
        "state": "{{ states('sensor.independent_actor_feedback') }}",
        "availability": "{{ has_value('sensor.independent_actor_feedback') }}",
        "select_option" if kind == "select" else "set_value": [
            {"event": STARTED, "event_data": {"requested": "{{ " + variable + " }}"}},
            {
                "wait_for_trigger": [{"trigger": "event", "event_type": RELEASE}],
                "timeout": "00:00:03",
                "continue_on_timeout": False,
            },
            {"event": COMPLETED, "event_data": {"requested": "{{ " + variable + " }}"}},
        ],
    }
    if kind == "select":
        actor["options"] = "{{ ['idle', 'quiet', 'boost'] }}"
    else:
        actor.update(min="0", max="100", step="1")
    entity_id = f"{kind}.independent_native_actor"
    assert await async_setup_component(
        hass,
        "template",
        {
            "template": [
                {
                    kind: [actor],
                    "sensor": [
                        {
                            "name": "Independent actor consumer",
                            "unique_id": "independent_actor_consumer",
                            "state": "{{ states('" + entity_id + "') }}",
                        }
                    ],
                }
            ]
        },
    )
    await hass.async_start()
    await hass.async_block_till_done()
    if kind == "select":
        assert hass.states.get(entity_id).state == initial
    else:
        assert float(hass.states.get(entity_id).state) == float(initial)
    resource = {
        "name": "Managed native actor",
        "kind": kind,
        "entity_id": entity_id,
        "retry_interval": 0.05,
        "command_interval": 0.01,
    }
    if kind == "number":
        resource.update(unit_of_measurement=None, tolerance=0)
    return resource, feedback, first, second


def target(kind, value):
    return {"option" if kind == "select" else "value": value}


@pytest.mark.parametrize("kind", ["select", "number"])
async def test_stalled_native_action_drains_before_superseded_request_and_reload(
    hass, tmp_path, kind
):
    resource, feedback, first, second = await native_actor(hass, kind)
    starts, completions = asyncio.Queue(), asyncio.Queue()
    journal = []

    @callback
    def started(event):
        journal.append(("start", event.data["requested"]))
        starts.put_nowait(event.data["requested"])

    @callback
    def completed(event):
        journal.append(("end", event.data["requested"]))
        completions.put_nowait(event.data["requested"])

    hass.bus.async_listen(STARTED, started)
    hass.bus.async_listen(COMPLETED, completed)
    entry = await async_setup_operator(hass, tmp_path, resources={"actor": resource})
    runtime = entry.runtime_data
    await runtime.async_set_mode("actor", "live")
    await runtime.async_request("actor", target=target(kind, first), indefinite=True)
    assert await asyncio.wait_for(starts.get(), 1) == first
    await asyncio.sleep(resource["retry_interval"] * 2)
    assert runtime.attempts["actor"] == 1
    assert journal == [("start", first)]
    await runtime.async_request("actor", target=target(kind, second), indefinite=True)
    assert runtime.attempts["actor"] == 1
    reloading = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
    try:
        await asyncio.sleep(resource["command_interval"] * 2)
        assert not reloading.done()
        assert journal == [("start", first)]
        assert starts.empty()
        hass.bus.async_fire(RELEASE)
        assert await asyncio.wait_for(completions.get(), 1) == first
        assert await asyncio.wait_for(reloading, 2)
        assert runtime._closed
        assert await asyncio.wait_for(starts.get(), 1) == second
        assert journal == [("start", first), ("end", first), ("start", second)]
        hass.bus.async_fire(RELEASE)
        assert await asyncio.wait_for(completions.get(), 1) == second
        # Native action completion has not changed the separately reported observation.
        assert hass.states.get(resource["entity_id"]).state not in {str(first), str(second)}
        hass.states.async_set(feedback, str(second))
        await hass.async_block_till_done()
        assert entry.runtime_data.decisions["actor"].status == "satisfied"
        assert journal == [
            ("start", first),
            ("end", first),
            ("start", second),
            ("end", second),
        ]
    finally:
        hass.bus.async_fire(RELEASE)
        await asyncio.gather(reloading, return_exceptions=True)
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["select", "number"])
async def test_native_service_completion_cannot_confirm_unavailable_feedback(hass, tmp_path, kind):
    resource, feedback, first, _ = await native_actor(hass, kind)
    started, completed = asyncio.Event(), asyncio.Event()
    unavailable = asyncio.Event()

    @callback
    def observed(event):
        if (
            event.data["entity_id"] == resource["entity_id"]
            and event.data["new_state"] is not None
            and event.data["new_state"].state == "unavailable"
        ):
            unavailable.set()

    remove = hass.bus.async_listen("state_changed", observed)
    hass.bus.async_listen(STARTED, callback(lambda _: started.set()))
    hass.bus.async_listen(COMPLETED, callback(lambda _: completed.set()))
    entry = await async_setup_operator(hass, tmp_path, resources={"actor": resource})
    runtime = entry.runtime_data
    await runtime.async_set_mode("actor", "live")
    await runtime.async_request("actor", target=target(kind, first), indefinite=True)
    try:
        await asyncio.wait_for(started.wait(), 1)
        hass.states.async_set(feedback, "unavailable")
        await asyncio.wait_for(unavailable.wait(), 1)
        hass.bus.async_fire(RELEASE)
        await asyncio.wait_for(completed.wait(), 1)
        await hass.async_block_till_done()
        assert hass.states.get(resource["entity_id"]).state == "unavailable"
        assert not runtime.observations["actor"].available
        assert runtime.decisions["actor"].status != "satisfied"
        assert runtime.manual("actor").target.to_dict() == target(kind, first)
        assert runtime.attempts["actor"] == 1
    finally:
        remove()
        hass.bus.async_fire(RELEASE)
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["select", "number"])
async def test_operator_reload_and_resource_removal_preserve_actor_and_other_consumer(
    hass, tmp_path, kind
):
    resource, feedback, first, _ = await native_actor(hass, kind)
    actor_id = resource["entity_id"]
    consumer_id = "sensor.independent_actor_consumer"
    registry = er.async_get(hass)
    actor_record = registry.async_get(actor_id)
    consumer_record = registry.async_get(consumer_id)
    entry = await async_setup_operator(hass, tmp_path, resources={"actor": resource})
    try:
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert hass.config_entries.async_remove_subentry(entry, "actor")
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(actor_id) == actor_record
        assert registry.async_get(consumer_id) == consumer_record
        hass.states.async_set(feedback, str(first))
        await hass.async_block_till_done()
        if kind == "select":
            assert hass.states.get(actor_id).state == str(first)
        else:
            assert float(hass.states.get(actor_id).state) == first
        assert hass.states.get(consumer_id).state == hass.states.get(actor_id).state
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
