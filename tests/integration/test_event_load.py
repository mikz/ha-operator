"""Bound event processing through real HA input and entity publication boundaries."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import EVENT_STATE_CHANGED, callback
from homeassistant.helpers.event import async_track_state_report_event
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

from .helpers import managed_id, operator_entry


def report(hass, key="roof0", position=0, **attributes):
    hass.states.async_set(
        f"cover.raw_{key}",
        "closed" if position == 0 else "open",
        {"current_position": position, "supported_features": 15, **attributes},
    )


async def setup(hass, tmp_path, *, count=4, policies=None, requirements=None):
    hass.config.config_dir = str(tmp_path)
    resources = {
        f"roof{i}": {
            "name": f"Roof {i}",
            "kind": "cover",
            "entity_id": f"cover.raw_roof{i}",
            "retry_interval": 30,
            "command_interval": 1,
        }
        for i in range(count)
    }
    for key in resources:
        report(hass, key)
    entry = operator_entry(resources=resources, policies=policies, requirements=requirements)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def capture_publications(hass):
    events = []
    ids = {state.entity_id for state in hass.states.async_all()} - {
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id.startswith("cover.raw_")
    }

    @callback
    def changed(event):
        if event.data["entity_id"] in ids:
            events.append(event)

    remove_changed = hass.bus.async_listen(EVENT_STATE_CHANGED, changed)
    remove_reported = async_track_state_report_event(hass, ids, events.append)
    return events, lambda: (remove_changed(), remove_reported())


async def test_identical_reports_refresh_observation_without_evaluation_or_publication(
    hass, tmp_path, freezer
):
    entry = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    events, remove = capture_publications(hass)
    before = runtime.observations["roof0"].reported_at
    with patch.object(runtime, "_recompute", wraps=runtime._recompute) as evaluate:
        for _ in range(100):
            freezer.move_to(dt_util.utcnow() + timedelta(seconds=1))
            report(hass)
            await hass.async_block_till_done()
        assert runtime.observations["roof0"].reported_at > before
        assert evaluate.call_count == 0
    assert events == []
    remove()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_changed_burst_coalesces_and_only_changed_entities_publish(hass, tmp_path):
    entry = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    events, remove = capture_publications(hass)
    with patch.object(runtime, "_recompute", wraps=runtime._recompute) as evaluate:
        for position in range(1, 26):
            report(hass, position=position)
        await hass.async_block_till_done()
        assert evaluate.call_count == 1
    assert {event.data["entity_id"] for event in events} == {
        managed_id(hass, "cover", "roof0"),
        managed_id(hass, "sensor", "roof0", "observed"),
    }
    assert len(events) == 2
    assert hass.states.get(managed_id(hass, "cover", "roof0")).attributes["current_position"] == 25
    remove()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_changed_input_does_not_wake_unrelated_pending_worker(hass, tmp_path):
    entry = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    commands = []

    async def command(call):
        commands.append(call.data["entity_id"])

    hass.services.async_register("cover", "set_cover_position", command)
    for key in ("roof0", "roof1"):
        await runtime.async_set_mode(key, "live")
        await runtime.async_request(key, target={"position": 80}, indefinite=True)
    await hass.async_block_till_done()
    assert set(commands) == {"cover.raw_roof0", "cover.raw_roof1"}
    deadlines = dict(runtime.next_attempts)
    with (
        patch.object(runtime._wake["roof0"], "set", wraps=runtime._wake["roof0"].set) as own,
        patch.object(runtime._wake["roof1"], "set", wraps=runtime._wake["roof1"].set) as other,
    ):
        report(hass, position=10)
        await hass.async_block_till_done()
        own.assert_called_once()
        other.assert_not_called()
    assert runtime.next_attempts == deadlines
    assert len(commands) == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_capability_and_availability_only_changes_are_published(hass, tmp_path):
    entry = await setup(hass, tmp_path)
    managed = managed_id(hass, "cover", "roof0")
    assert hass.states.get(managed).attributes["supported_features"] & 8
    report(hass, supported_features=7)
    await hass.async_block_till_done()
    assert not hass.states.get(managed).attributes["supported_features"] & 8
    hass.states.async_set("cover.raw_roof0", "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(managed).state == "unavailable"
    report(hass)
    await hass.async_block_till_done()
    assert hass.states.get(managed).state == "closed"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reports_do_not_starve_retry_or_expiry(hass, tmp_path, freezer):
    entry = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    commands = []

    async def command(call):
        commands.append(call.data["position"])

    hass.services.async_register("cover", "set_cover_position", command)
    await runtime.async_set_mode("roof0", "live")
    await runtime.async_request("roof0", target={"position": 80}, duration=35)
    await hass.async_block_till_done()
    for _ in range(8):
        freezer.move_to(dt_util.utcnow() + timedelta(seconds=5))
        report(hass)
        async_fire_time_changed_exact(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert commands == [80, 80]
    assert runtime.manual("roof0") is None
    assert runtime.decisions["roof0"].status == "idle"
    assert "roof0" not in runtime.next_attempts
    await hass.config_entries.async_unload(entry.entry_id)


async def test_provider_group_wakes_together_without_unrelated_resource(hass, tmp_path):
    hass.states.async_set("binary_sensor.extraction", "off")
    requirement = {
        "name": "Incoming air",
        "activation_entities": ["binary_sensor.extraction"],
        "acquisition_timeout": 120,
        "providers": [
            {
                "id": f"route{i}",
                "resource_id": f"roof{i}",
                "target": {"position": 100},
                "evidence": [
                    {
                        "entity_id": f"cover.raw_roof{i}",
                        "attribute": "current_position",
                        "operator": "gte",
                        "value": 60,
                        "kind": "position",
                    }
                ],
            }
            for i in range(2)
        ],
    }
    entry = await setup(hass, tmp_path, requirements={"air": requirement})
    runtime = entry.runtime_data

    async def refuse(call):
        pass

    hass.services.async_register("cover", "set_cover_position", refuse)
    for key in ("roof0", "roof1", "roof2"):
        await runtime.async_set_mode(key, "live")
        await runtime.async_request(key, target={"position": 80}, indefinite=True)
    await hass.async_block_till_done()
    with (
        patch.object(runtime._wake["roof0"], "set", wraps=runtime._wake["roof0"].set) as first,
        patch.object(runtime._wake["roof1"], "set", wraps=runtime._wake["roof1"].set) as alternate,
        patch.object(runtime._wake["roof2"], "set", wraps=runtime._wake["roof2"].set) as unrelated,
    ):
        report(hass, position=10)
        await hass.async_block_till_done()
        first.assert_called_once()
        alternate.assert_called_once()
        unrelated.assert_not_called()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_dispatch_guard_reads_input_before_coalesced_flush(hass, tmp_path):
    hass.states.async_set("sensor.target", "80")
    entry = await setup(
        hass,
        tmp_path,
        policies={
            "follow": {
                "name": "Follow target",
                "resource_id": "roof0",
                "kind": "state",
                "target_entity": "sensor.target",
                "target_field": "position",
            }
        },
    )
    runtime = entry.runtime_data
    commands = []

    async def command(call):
        commands.append(call.data)

    hass.services.async_register("cover", "set_cover_position", command)
    adapter = runtime.adapter("roof0")
    original = adapter.async_apply

    async def supersede(target, still_current):
        # No event-loop yield: the pending batch must not hide this newer target.
        hass.states.async_set("sensor.target", "0")
        return await original(target, still_current)

    with patch.object(adapter, "async_apply", supersede):
        await runtime.async_set_mode("roof0", "live")
        await hass.async_block_till_done()
    assert commands == []
    assert runtime.decisions["roof0"].target.position == 0
    assert runtime.decisions["roof0"].status == "satisfied"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_cancels_pending_batch(hass, tmp_path):
    entry = await setup(hass, tmp_path)
    runtime = entry.runtime_data
    events, remove = capture_publications(hass)
    report(hass, position=40)
    pending = runtime._input_flush
    assert pending is not None
    await runtime.async_close()
    await hass.async_block_till_done()
    assert pending.cancelled()
    assert runtime._input_flush is None and not runtime._pending_resources
    assert events == []
    remove()
    await hass.config_entries.async_unload(entry.entry_id)
