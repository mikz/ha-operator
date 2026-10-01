"""Native history distinguishes selection, execution, and trusted observation."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact

from custom_components.ha_operator.core import Decision, ManualLease, Occurrence, Target
from custom_components.ha_operator.provenance import selection_for, target_label

from .helpers import async_activate, async_add_physical_cover, async_setup_operator, managed_id


def policy(name, priority=0, **extra):
    return {
        "name": name,
        "resource_id": "roof",
        "kind": "state",
        "priority": priority,
        "target": {"position": 40},
        **extra,
    }


async def test_same_target_changes_reason_without_dispatch_and_rename(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "baseline": policy("Background ventilation"),
            "evening": policy(
                "Evening ventilation", 10, eligibility_entity="input_boolean.evening"
            ),
        },
    )
    await async_activate(hass)
    desired_id = managed_id(hass, "sensor", key="desired")
    observed_id = managed_id(hass, "sensor", key="observed")
    reason_id = managed_id(hass, "sensor", key="reason")
    assert (
        float(hass.states.get(desired_id).state) == float(hass.states.get(observed_id).state) == 40
    )
    assert hass.states.get(desired_id).attributes["unit_of_measurement"] == "%"
    assert "state_class" not in hass.states.get(desired_id).attributes
    assert hass.states.get(observed_id).attributes["state_class"] == "measurement"
    events = []
    remove = hass.bus.async_listen("logbook_entry", events.append)
    before = list(raw.commands)
    hass.states.async_set("input_boolean.evening", "on")
    await hass.async_block_till_done()
    assert hass.states.get(reason_id).state == "Evening ventilation"
    assert hass.states.get(desired_id).attributes["source_id"] == "evening"
    assert hass.states.get(desired_id).attributes["selection_reason"] == "Evening ventilation"
    assert raw.commands == before
    assert len(events) == 1
    assert events[0].data["entity_id"] == managed_id(hass, "cover")
    assert "Evening ventilation" in events[0].data["message"]
    stable = hass.states.get(desired_id)
    before = deepcopy(entry.runtime_data.explain())
    await entry.runtime_data.async_reconcile()
    assert hass.states.get(desired_id).last_updated == stable.last_updated
    assert entry.runtime_data.explain() == before
    er.async_get(hass).async_update_entity(observed_id, new_entity_id="sensor.renamed_feedback")
    await hass.async_block_till_done()
    assert hass.states.get(desired_id).attributes["observed_entity"] == "sensor.renamed_feedback"
    remove()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_admission_context_expiry_and_recovery(hass, tmp_path, freezer):
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "baseline": policy("Background ventilation"),
            "morning": policy("Morning occurrence", 20, kind="occurrence"),
        },
    )
    await async_activate(hass)
    calls = []
    remove = hass.bus.async_listen("call_service", calls.append)
    origin = Context()
    await hass.services.async_call(
        "ha_operator",
        "request",
        {
            "resource_id": "roof",
            "target": {"position": 75},
            "duration": 5,
            "request_id": "demo-request",
        },
        blocking=True,
        context=origin,
    )
    await hass.async_block_till_done()
    desired = hass.states.get(managed_id(hass, "sensor", key="desired"))
    assert desired.attributes["request_id"] == "demo-request"
    assert float(desired.state) == 75
    assert float(hass.states.get(managed_id(hass, "sensor", key="observed")).state) == 0
    assert entry.runtime_data._selection_context("roof") is origin
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=31))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert (
        hass.states.get(managed_id(hass, "sensor", key="reason")).state == "Background ventilation"
    )
    assert entry.runtime_data._contexts == {}
    # A future command is linked to the supplied ingress, never raw feedback.
    await entry.runtime_data.async_request(
        "roof", target={"position": 60}, request_id="known-origin", context=origin
    )
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=31))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    dispatched = [event for event in calls if event.data.get("domain") == "cover"]
    assert dispatched[-1].context.parent_id == origin.id
    assert dispatched[-1].context.user_id is None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data._selection_context("roof") is None
    await entry.runtime_data.async_request(
        "roof", target={"position": 60}, request_id="known-origin", context=Context()
    )
    assert entry.runtime_data._selection_context("roof") is None
    await entry.runtime_data.async_release("roof")
    expiry = dt_util.utcnow().timestamp() + 5
    await entry.runtime_data.async_submit_occurrence(
        "morning", "day:fold:0", expiry, context=origin
    )
    selection = entry.runtime_data.selections["roof"]
    assert selection.occurrence_id == "day:fold:0" and selection.expires_at == expiry
    assert entry.runtime_data._selection_context("roof") is origin
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=6))
    async_fire_time_changed_exact(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert entry.runtime_data.selections["roof"].source_id == "baseline"
    stop_origin = Context()
    await hass.services.async_call(
        "cover",
        "stop_cover",
        {
            "entity_id": managed_id(hass, "cover"),
        },
        blocking=True,
        context=stop_origin,
    )
    await hass.async_block_till_done()
    assert entry.runtime_data._selection_context("roof").id == stop_origin.id
    physical_stops = [
        event
        for event in calls
        if event.data.get("service") == "stop_cover"
        and event.data.get("service_data", {}).get("entity_id") == raw.entity_id
    ]
    assert physical_stops[-1].context.parent_id == stop_origin.id
    remove()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("attribute", ["optimistic", "assumed_state", "restored", "unavailable"])
async def test_observed_rejects_untrusted_feedback(hass, tmp_path, attribute):
    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(hass, tmp_path)
    attrs = dict(hass.states.get(raw.entity_id).attributes)
    attrs[attribute] = True
    hass.states.async_set(
        raw.entity_id, "unavailable" if attribute == "unavailable" else "open", attrs
    )
    await hass.async_block_till_done()
    expected = "unavailable" if attribute == "unavailable" else "unknown"
    assert hass.states.get(managed_id(hass, "sensor", key="observed")).state == expected
    assert (
        hass.states.get(managed_id(hass, "sensor", key="observed")).attributes.get("target") is None
    )
    await hass.config_entries.async_unload(entry.entry_id)


async def test_group_cause_snapshot_changes_without_actuation(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    hass.states.async_set("fan.extractor", "on", {"friendly_name": "Extractor"})
    hass.states.async_set("fan.other", "off", {"friendly_name": "Other extractor"})
    hass.states.async_set(
        "binary_sensor.extraction",
        "on",
        {
            "entity_id": ["fan.extractor", "fan.other", "binary_sensor.extraction"],
        },
    )
    entry = await async_setup_operator(
        hass,
        tmp_path,
        requirements={
            "air": {
                "name": "Extraction inlet",
                "activation_entities": ["binary_sensor.extraction"],
                "providers": [
                    {
                        "id": "roof",
                        "resource_id": "roof",
                        "target": {"position": 40},
                        "evidence": [
                            {
                                "kind": "position",
                                "entity_id": raw.entity_id,
                                "attribute": "current_position",
                                "operator": "gte",
                                "value": 20,
                            }
                        ],
                    }
                ],
            }
        },
    )
    await async_activate(hass)
    assert entry.runtime_data.selections["roof"].active_inputs == ("fan.extractor",)
    before = list(raw.commands)
    old = entry.runtime_data.explain("roof")
    hass.states.async_set("fan.other", "on", {"friendly_name": "Other extractor"})
    await hass.async_block_till_done()
    assert entry.runtime_data.selections["roof"].active_inputs == ("fan.extractor", "fan.other")
    assert old["resources"]["roof"]["selection"]["active_inputs"] == ("fan.extractor",)
    assert raw.commands == before
    reason = hass.states.get(managed_id(hass, "sensor", key="reason")).state
    assert reason == "Airflow: Extraction inlet — Extractor, Other extractor"
    await hass.config_entries.async_unload(entry.entry_id)


def test_selection_identity_bounds_and_unknown():
    decision = Decision("r", None, "idle", None, "no target")
    assert selection_for(decision, None, {}, [], {}, {}).source_kind == "idle"
    assert (
        selection_for(replace(decision, source="unresolved"), None, {}, [], {}, {}).source_kind
        == "unknown"
    )

    manual = ManualLease("r", "hands_off", request_id="request", source="stop")
    assert (
        selection_for(
            replace(decision, source="manual:stop"), manual, {}, [], {}, {}
        ).selection_reason
        == "Hands off"
    )
    occurrence = Occurrence("policy", "date:fold:1", 100, target=Target(position=10))
    selected = selection_for(
        replace(decision, source="policy:policy:date:fold:1"),
        None,
        {"policy": {"name": "Timed", "target_entity": "sensor.target"}},
        [occurrence],
        {},
        {},
    )
    assert (
        selected.related_entities == ("sensor.target",) and selected.occurrence_id == "date:fold:1"
    )
    requirement = {
        "req": {
            "name": "R" * 200,
            "activation_entities": ["fan.raw"],
            "providers": [{"id": "other"}, {"id": "provider"}],
        }
    }
    selected = selection_for(
        replace(decision, source="requirement:req:provider"),
        None,
        {},
        [],
        requirement,
        {"req": (("fan.raw", "A" * 200),)},
    )
    assert len(selected.source_name) == 128 and len(selected.selection_reason) == 255
    assert selection_for(
        replace(decision, source="requirement:req:provider"), None, {}, [], requirement, {}
    ).selection_reason.startswith("Airflow:")


@pytest.mark.parametrize(
    "target,label",
    [
        (None, "none"),
        (Target(position=40), "40%"),
        (Target(profile="inward"), "inward"),
        (Target(on=False), "off"),
        (Target(percentage=50, direction="reverse"), "50% reverse"),
        (Target(percentage=50), "50%"),
        (Target(direction="forward"), "forward"),
        (Target(on=True), "on"),
    ],
)
def test_activity_target_label(target, label):
    assert target_label(target) == label
