"""Run public services through HA validation and real durable request admission."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator.const import DOMAIN

from .helpers import async_activate, async_add_physical_cover, async_setup_operator, managed_id


@pytest.fixture
async def integration(hass, tmp_path):
    raw = await async_add_physical_cover(hass)
    raw.refuse = True
    entry = await async_setup_operator(
        hass,
        tmp_path,
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 90},
            }
        },
    )
    yield entry, raw
    if hasattr(entry, "runtime_data") and not entry.runtime_data._closed:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def call(hass, action, data=None, response=False):
    return await hass.services.async_call(
        DOMAIN,
        action,
        data or {},
        blocking=True,
        return_response=response,
    )


async def test_registration_and_missing_configuration(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    assert set(hass.services.async_services()[DOMAIN]) == {
        "request",
        "release",
        "submit_occurrence",
        "skip_occurrence",
        "reconcile",
        "explain",
    }
    assert hass.services.supports_response(DOMAIN, "explain") is SupportsResponse.ONLY
    assert hass.services.supports_response(DOMAIN, "request") is SupportsResponse.OPTIONAL
    with pytest.raises(ServiceValidationError, match="Configure HA Operator"):
        await call(hass, "explain", response=True)
    with pytest.raises(ServiceValidationError):
        await call(hass, "explain", {"config_entry_id": "missing"}, response=True)
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    with pytest.raises(ServiceValidationError):
        await call(hass, "explain", {"config_entry_id": other.entry_id}, response=True)


async def test_observe_rejects_commands_and_allows_explanation(hass, integration):
    entry, raw = integration
    before = entry.runtime_data.store.state
    with pytest.raises(ServiceValidationError, match="observe"):
        await call(hass, "request", {"resource_id": "roof", "target": {"position": 100}})
    assert entry.runtime_data.store.state == before and raw.commands == []
    result = await call(hass, "explain", response=True)
    assert result["resources"]["roof"]["mode"] == "observe"
    assert result["resources"]["roof"]["observation"]["target"]["position"] == 0
    await call(hass, "reconcile")
    assert raw.commands == []


async def test_request_receipt_follows_real_atomic_file_and_release(hass, tmp_path, integration):
    entry, raw = integration
    await async_activate(hass)
    data = {
        "entity_id": managed_id(hass, "cover"),
        "target": {"position": 70},
        "request_id": "manual-001",
        "duration": 180,
    }
    receipt = await call(hass, "request", data, response=True)
    assert receipt["accepted"] and receipt["request_id"] == "manual-001"
    assert receipt["resource_id"] == "roof"
    snapshot = json.loads(
        await hass.async_add_executor_job(
            (tmp_path / f"ha_operator.{entry.entry_id}.json").read_text,
        )
    )["data"]
    assert snapshot["manuals"]["roof"]["target"] == {"position": 70}
    assert snapshot["manuals"]["roof"]["expires_at"] == receipt["expires_at"]
    duplicate = await call(hass, "request", data, response=True)
    assert duplicate == receipt
    await hass.async_block_till_done()
    assert raw.commands == [("position", 70)]
    explanation = await call(hass, "explain", {"entity_id": data["entity_id"]}, response=True)
    resource = explanation["resources"]["roof"]
    assert resource["manual"]["target"]["position"] == 70
    assert resource["decision"]["target"]["position"] == 70
    assert resource["observation"]["target"]["position"] == 0
    await call(hass, "release", {"resource_id": "roof"})
    assert entry.runtime_data.manual("roof") is None
    await call(hass, "request", {"resource_id": "roof", "mode": "hands_off", "indefinite": True})
    assert entry.runtime_data.manual("roof").mode == "hands_off"
    assert entry.runtime_data.manual("roof").expires_at is None
    await hass.services.async_call(
        "button",
        "press",
        {
            "entity_id": managed_id(hass, "button", key="release"),
        },
        blocking=True,
    )
    assert entry.runtime_data.manual("roof") is None


@pytest.mark.parametrize(
    "data,pattern",
    [
        ({}, "Target a resource_id"),
        ({"resource_id": "missing"}, "Unknown resource"),
        ({"entity_id": ["cover.physical_roof", "cover.other"]}, "exactly one"),
        ({"entity_id": "cover.physical_roof"}, "not a managed"),
        ({"entity_id": "cover.does_not_exist"}, "not a managed"),
        ({"entity_id": "cover.physical_roof", "resource_id": "roof"}, "Choose resource_id"),
    ],
)
async def test_invalid_target_routing(hass, integration, data, pattern):
    with pytest.raises(ServiceValidationError, match=pattern):
        await call(hass, "release", data)


async def test_foreign_entry_and_policy_entities_are_not_resource_targets(hass, integration):
    entry, _ = integration
    foreign = MockConfigEntry(domain=DOMAIN)
    foreign.add_to_hass(hass)
    record = er.async_get(hass).async_get_or_create(
        "cover",
        DOMAIN,
        "foreign",
        config_entry=foreign,
    )
    with pytest.raises(ServiceValidationError, match="not a managed"):
        await call(
            hass,
            "release",
            {
                "config_entry_id": entry.entry_id,
                "entity_id": record.entity_id,
            },
        )
    with pytest.raises(ServiceValidationError, match="Unknown resource"):
        await call(
            hass,
            "release",
            {
                "config_entry_id": entry.entry_id,
                "entity_id": managed_id(hass, "switch", "morning", "enabled"),
            },
        )
    with pytest.raises(ServiceValidationError, match="Configure HA Operator"):
        await call(hass, "explain", response=True)


async def test_occurrence_submit_response_suppression_and_no_catchup(hass, integration):
    entry, raw = integration
    await async_activate(hass)
    expires = dt_util.utcnow().timestamp() + 1200
    occurrence = {"policy_id": "morning", "occurrence_id": "2026-09-27", "expires_at": expires}
    await call(hass, "skip_occurrence", occurrence)
    result = await call(hass, "submit_occurrence", occurrence, response=True)
    assert result["skipped"] is True
    assert result["occurrence_id"] == "2026-09-27"
    await hass.async_block_till_done()
    assert raw.commands == []
    await call(hass, "submit_occurrence", {**occurrence, "occurrence_id": "2026-09-28"})
    await hass.async_block_till_done()
    assert raw.commands == [("position", 90)]
    await call(hass, "reconcile", {"resource_id": "roof"})
    assert entry.runtime_data.manual("roof") is None
    with pytest.raises(ServiceValidationError, match="Unknown occurrence"):
        await call(hass, "submit_occurrence", {**occurrence, "policy_id": "missing"})


async def test_action_rejects_unloaded_entry(hass, integration):
    entry, _ = integration
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(HomeAssistantError):
        await call(hass, "explain", {"config_entry_id": entry.entry_id}, response=True)
