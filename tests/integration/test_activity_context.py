"""Recorded native Activity keeps the automation that selected a target."""

from datetime import timedelta

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .helpers import async_activate, async_add_physical_cover, async_setup_operator, managed_id
from .test_provenance import policy


@pytest.fixture(autouse=True)
def _custom_integrations(recorder_db_url, enable_custom_integrations):
    """Prepare Recorder before the shared custom-integration fixture uses HA."""


async def test_automation_selection_is_attributed_in_recorded_activity(
    hass, tmp_path, recorder_mock
):
    from homeassistant.components.logbook.helpers import async_determine_event_types
    from homeassistant.components.logbook.processor import EventProcessor
    from pytest_homeassistant_custom_component.common import mock_component
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    raw = await async_add_physical_cover(hass)
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "baseline": policy("Background ventilation"),
            "morning": policy("Morning occurrence", 20, kind="occurrence"),
        },
    )
    await async_activate(hass)
    mock_component(hass, "frontend")  # No browser assets needed for native database lookup.
    assert await async_setup_component(hass, "logbook", {})
    expiry = dt_util.utcnow().timestamp() + 1800
    hass.states.async_set("sensor.room_co2", "900", {"unit_of_measurement": "ppm"})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "ventilation_cause",
                    "alias": "Ventilation cause",
                    "triggers": [
                        {"trigger": "numeric_state", "entity_id": "sensor.room_co2", "above": 1000}
                    ],
                    "actions": [
                        {
                            "action": "ha_operator.submit_occurrence",
                            "data": {
                                "policy_id": "morning",
                                "occurrence_id": "opaque:episode",
                                "expires_at": expiry,
                            },
                        }
                    ],
                }
            ]
        },
    )
    before = list(raw.commands)
    hass.states.async_set("sensor.room_co2", "1200", {"unit_of_measurement": "ppm"})
    await hass.async_block_till_done()
    reason_id = managed_id(hass, "sensor", key="reason")
    reason = hass.states.get(reason_id)
    assert reason.state == "Morning occurrence"
    for key in ("desired", "effective_expiry"):
        assert (
            hass.states.get(managed_id(hass, "sensor", key=key)).context.id
            == reason.context.parent_id
        )
    assert raw.commands == before  # Same target, different selected cause.
    await async_wait_recording_done(hass)
    processor = EventProcessor(
        hass, async_determine_event_types(hass, [reason_id], None), entity_ids=[reason_id]
    )
    events = await hass.async_add_executor_job(
        processor.get_events,
        dt_util.utcnow() - timedelta(minutes=1),
        dt_util.utcnow() + timedelta(minutes=1),
    )
    row = next(event for event in events if event.get("state") == "Morning occurrence")
    assert row["context_entity_id"] == reason_id
    assert row["context_event_type"] == "script_started"
    await hass.config_entries.async_unload(entry.entry_id)
