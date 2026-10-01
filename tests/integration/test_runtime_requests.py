"""Admission and native persistence through a real HA runtime, states, services and files."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import storage as ha_storage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator import runtime as runtime_module
from custom_components.ha_operator.core import Target
from custom_components.ha_operator.runtime import OperatorRuntime


@pytest.fixture
def clock(monkeypatch):
    now = [2_000_000_000.0]
    monkeypatch.setattr(runtime_module, "_now", lambda: now[0])
    return now


@pytest.fixture
async def runtime_factory(hass, tmp_path, clock):
    """Start actual workers and real disk writes; raw devices do not echo commands."""
    hass.config.config_dir = str(tmp_path)
    hass.states.async_set("cover.raw", "closed", {"supported_features": 15, "current_position": 0})
    hass.states.async_set("binary_sensor.schedule", "on")
    hass.states.async_set("sensor.target", "60", {"position": 60})
    calls = []

    async def capture(call):
        calls.append({"service": call.service, "data": dict(call.data)})

    hass.services.async_register("cover", "set_cover_position", capture)
    hass.services.async_register("cover", "stop_cover", capture)
    instances = []

    async def create(
        *, resource=None, policies=None, initialized=False, before_start=None, setup_error=False
    ):
        resource_data = {
            "name": "Window",
            "kind": "cover",
            "entity_id": "cover.raw",
            **(resource or {}),
        }
        subentries = [
            {
                "subentry_id": "roof",
                "subentry_type": "resource",
                "title": "Window",
                "data": resource_data,
            }
        ]
        for key, data in (policies or {}).items():
            subentries.append(
                {
                    "subentry_id": key,
                    "subentry_type": "policy",
                    "title": data["name"],
                    "data": {"resource_id": "roof", **data},
                }
            )
        entry = MockConfigEntry(
            domain="ha_operator",
            data={"initialized": initialized},
            subentries_data=subentries,
        )
        entry.add_to_hass(hass)
        runtime = OperatorRuntime(hass, entry)
        entry.runtime_data = runtime
        instances.append(runtime)
        if before_start:
            await before_start(runtime)
        if setup_error:
            with pytest.raises(ConfigEntryError) as error:
                await runtime.async_start()
            assert error.value.translation_key == "storage_error"
        else:
            await runtime.async_start()
        await hass.async_block_till_done()
        return runtime, calls

    yield create
    for runtime in instances:
        if not runtime._closed:
            await runtime.async_close()


def current_state(runtime):
    return deepcopy(runtime._state)


async def test_observe_rejects_request_without_saving_or_actuating(runtime_factory, hass):
    runtime, calls = await runtime_factory()
    assert runtime.mode("roof") == "observe"
    before = current_state(runtime)
    with pytest.raises(ServiceValidationError, match="observe"):
        await runtime.async_request("roof", target={"position": 100})
    await hass.async_block_till_done()
    assert calls == []
    assert current_state(runtime) == before
    assert deepcopy(runtime._state)["manuals"] == {}
    assert deepcopy(runtime._state)["requests"] == {}


async def test_request_updates_runtime_without_waiting_for_save(runtime_factory, hass, monkeypatch):
    runtime, calls = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    saves = []
    monkeypatch.setattr(runtime._store, "async_delay_save", lambda data, delay: saves.append(data))
    receipt = await runtime.async_request("roof", target={"position": 70}, request_id="opening")
    await hass.async_block_till_done()
    assert receipt["accepted"] and runtime.manual("roof").target.position == 70
    assert calls[-1]["data"]["position"] == 70
    assert saves[-1]()["manuals"]["roof"]["request_id"] == "opening"
    assert "revision" not in runtime.explain()


async def test_request_retry_is_idempotent_and_conflict_is_validation_error(
    runtime_factory, hass, clock
):
    runtime, calls = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    first = await runtime.async_request(
        "roof", target={"position": 70}, duration=600, request_id="one"
    )
    await hass.async_block_till_done()
    clock[0] += 200
    retry = await runtime.async_request(
        "roof", target={"position": 70}, duration=600, request_id="one"
    )
    assert retry == first
    assert runtime.manual("roof").expires_at == first["expires_at"]
    with pytest.raises(ServiceValidationError, match="different intent"):
        await runtime.async_request("roof", target={"position": 30}, request_id="one")
    assert runtime.fault is None
    assert len(deepcopy(runtime._state)["requests"]) == 1
    assert runtime.manual("roof").target.position == 70
    assert len(calls) == 1
    await runtime.async_release("roof")
    await runtime.async_request("roof", target={"position": 70}, duration=600, request_id="one")
    assert runtime.manual("roof") is None  # an acknowledged retry cannot recreate a released lease


async def test_default_explicit_and_indefinite_leases(runtime_factory, clock):
    runtime, _ = await runtime_factory(resource={"manual_duration": 1800})
    await runtime.async_set_mode("roof", "live")
    default = await runtime.async_request("roof", target={"position": 30})
    assert default["expires_at"] == clock[0] + 1800
    finite = await runtime.async_request("roof", target={"position": 40}, duration=90)
    assert finite["expires_at"] == clock[0] + 90
    absolute = await runtime.async_request(
        "roof", target={"position": 50}, expires_at=clock[0] + 30
    )
    assert absolute["expires_at"] == clock[0] + 30
    indefinite = await runtime.async_request("roof", target={"position": 60}, indefinite=True)
    assert indefinite["expires_at"] is None
    assert current_state(runtime)["manuals"]["roof"]["expires_at"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"mode": "bogus"},
        {"mode": "hands_off", "target": {"position": 50}},
        {"duration": 1, "indefinite": True},
        {"duration": 1, "expires_at": 2_000_000_030},
        {"duration": 0},
        {"duration": -1},
        {"duration": True},
        {"duration": "30"},
        {"duration": float("nan")},
        {"expires_at": 1},
        {"expires_at": float("inf")},
        {"target": {}},
        {"target": {"position": 101}},
        {"target": {"on": True}},
        {"request_id": 123},
        {"request_id": "x" * 129},
    ],
)
async def test_invalid_request_never_persists(runtime_factory, arguments):
    runtime, _ = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    before = deepcopy(runtime._state)
    request = {"target": {"position": 70}, **arguments}
    with pytest.raises(ServiceValidationError):
        await runtime.async_request("roof", **request)
    assert deepcopy(runtime._state) == before
    assert runtime.fault is None


async def test_hands_off_release_and_resource_validation(runtime_factory, hass, clock):
    runtime, calls = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", mode="hands_off", duration=60)
    await hass.async_block_till_done()
    assert runtime.manual("roof").mode == "hands_off"
    assert calls == []
    assert runtime.decisions["roof"].status == "hands_off"
    await runtime.async_release("roof")
    assert runtime.manual("roof") is None
    assert runtime.decisions["roof"].status == "idle"
    await runtime.async_request("roof", mode="hands_off", duration=60)
    clock[0] += 60
    await runtime.async_reconcile("roof")
    assert runtime.manual("roof") is None
    assert runtime.decisions["roof"].status == "idle"
    with pytest.raises(ServiceValidationError, match="Unknown resource"):
        await runtime.async_release("other")
    with pytest.raises(ServiceValidationError, match="observe or live"):
        await runtime.async_set_mode("roof", "active")
    with pytest.raises(ServiceValidationError, match="Unknown resource"):
        await runtime.async_reconcile("other")
    await runtime.async_close()
    with pytest.raises(HomeAssistantError, match="unloaded"):
        await runtime.async_request("roof", target={"position": 0})


async def test_native_write_failure_does_not_inhibit_request_or_stop(
    runtime_factory, hass, monkeypatch
):
    runtime, calls = await runtime_factory()
    await runtime.async_set_mode("roof", "live")

    def fail(*args, **kwargs):
        raise ha_storage.WriteError("disk full")

    monkeypatch.setattr(ha_storage, "write_utf8_file", fail)
    await runtime.async_request("roof", target={"position": 80})
    await runtime._store.async_save(runtime._state)
    await hass.async_block_till_done()
    assert runtime.fault is None and runtime.manual("roof").target.position == 80
    assert calls[-1]["data"]["position"] == 80
    assert not ir.async_get(hass).async_get_issue(
        "ha_operator", f"storage_{runtime.entry.entry_id}"
    )
    await runtime.async_stop("roof")
    await runtime._store.async_save(runtime._state)
    assert runtime.manual("roof").mode == "hands_off"
    assert calls[-1]["service"] == "stop_cover"
    await runtime.async_request("roof", target={"position": 0})
    assert runtime.manual("roof").target.position == 0 and runtime.fault is None


async def test_missing_store_uses_configured_observe_defaults(runtime_factory):
    runtime, calls = await runtime_factory(initialized=True)
    assert runtime.fault is None and runtime.mode("roof") == "observe"
    assert runtime.manual("roof") is None and calls == []


@pytest.mark.parametrize(
    "manual",
    [
        {"mode": "target", "target": {"position": 75}, "expires_at": 1},
        {"mode": "hands_off", "expires_at": 1},
    ],
)
async def test_start_prunes_expired_saved_lease(runtime_factory, manual):
    async def prepare(runtime):
        runtime._mutate_state(lambda state: state["manuals"].update(roof=manual))
        await runtime._store.async_save(runtime._state)

    runtime, calls = await runtime_factory(initialized=True, before_start=prepare)
    assert runtime.fault is None
    assert runtime.manual("roof") is None
    assert current_state(runtime)["manuals"] == {}
    assert calls == []


async def test_valid_saved_intent_waits_for_non_restored_observation(runtime_factory, hass, clock):
    async def prepare(runtime):

        def saved(state):
            state["modes"]["roof"] = "live"
            state["manuals"]["roof"] = {
                "mode": "target",
                "target": {"position": 65},
                "expires_at": clock[0] + 60,
            }

        runtime._mutate_state(saved)
        await runtime._store.async_save(runtime._state)
        hass.states.async_set(
            "cover.raw",
            "closed",
            {"current_position": 0, "supported_features": 15, "restored": True},
        )

    runtime, calls = await runtime_factory(initialized=True, before_start=prepare)
    assert calls == []
    assert runtime.decisions["roof"].status == "unavailable"
    hass.states.async_set("cover.raw", "closed", {"current_position": 0, "supported_features": 15})
    await hass.async_block_till_done()
    assert calls[-1]["data"]["position"] == 65


async def test_stop_establishes_hands_off_and_bypasses_pacing(runtime_factory, hass):
    runtime, calls = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request("roof", target={"position": 80})
    await hass.async_block_till_done()
    await runtime.async_stop("roof")
    assert runtime.manual("roof").mode == "hands_off"
    assert [call["service"] for call in calls] == ["set_cover_position", "stop_cover"]


async def test_unsupported_stop_still_persists_hands_off(runtime_factory, hass):
    runtime, calls = await runtime_factory()
    hass.states.async_set("cover.raw", "closed", {"supported_features": 7, "current_position": 0})
    await runtime.async_set_mode("roof", "live")
    with pytest.raises(HomeAssistantError, match="does not support physical STOP"):
        await runtime.async_stop("roof")
    assert runtime.manual("roof").mode == "hands_off"
    assert current_state(runtime)["manuals"]["roof"]["mode"] == "hands_off"
    assert calls == []


async def test_occurrence_skip_before_submit_and_disabled_no_replay(runtime_factory, hass, clock):
    runtime, calls = await runtime_factory(
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "target": {"position": 60},
            }
        }
    )
    await runtime.async_skip_occurrence("morning", "sleep-in", clock[0] + 100)
    await runtime.async_set_mode("roof", "live")
    skipped = await runtime.async_submit_occurrence("morning", "sleep-in", clock[0] + 100)
    assert skipped["skipped"] is True
    await hass.async_block_till_done()
    assert calls == []
    admitted = await runtime.async_submit_occurrence("morning", "ordinary", clock[0] + 100)
    assert admitted["skipped"] is False
    await runtime.async_set_policy_enabled("morning", False)
    await runtime.async_set_policy_enabled("morning", True)
    assert all(item["skipped"] for item in current_state(runtime)["occurrences"].values())
    assert runtime.decisions["roof"].status == "idle"
    disabled = await runtime.async_submit_occurrence("morning", "new", clock[0] + 100)
    assert disabled["skipped"] is False
    assert runtime.decisions["roof"].target == Target(position=60)
    await runtime.async_set_policy_enabled("morning", False)
    disabled = await runtime.async_submit_occurrence("morning", "while-disabled", clock[0] + 100)
    assert disabled["skipped"] is True
    await runtime.async_set_policy_enabled("morning", True)
    assert runtime.decisions["roof"].status == "idle"
    assert runtime.fault is None


async def test_occurrence_dynamic_target_frozen_and_duplicate_cannot_extend(
    runtime_factory, hass, clock
):
    runtime, _ = await runtime_factory(
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "target_entity": "sensor.target",
                "target_attribute": "position",
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    admitted = await runtime.async_submit_occurrence("morning", "2026-10-25T01:00Z", clock[0] + 100)
    assert admitted["target"] == {"position": 60}
    hass.states.async_set("sensor.target", "60", {"position": 90})
    await hass.async_block_till_done()
    assert runtime.decisions["roof"].target == Target(position=60)
    duplicate = await runtime.async_submit_occurrence(
        "morning", "2026-10-25T01:00Z", clock[0] + 200
    )
    assert duplicate == admitted
    clock[0] += 100
    await runtime.async_reconcile("roof")
    assert runtime.decisions["roof"].status == "idle"
    duplicate = await runtime.async_submit_occurrence(
        "morning", "2026-10-25T01:00Z", clock[0] + 200
    )
    assert duplicate == admitted
    assert runtime.decisions["roof"].status == "idle"


async def test_occurrence_eligibility_and_unavailable_target(runtime_factory, hass, clock):
    runtime, calls = await runtime_factory(
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "target_entity": "sensor.target",
                "eligibility_entity": "binary_sensor.schedule",
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    hass.states.async_set("binary_sensor.schedule", "off")
    skipped = await runtime.async_submit_occurrence("morning", "off", clock[0] + 100)
    assert skipped["skipped"] is True
    hass.states.async_set("binary_sensor.schedule", "on")
    await hass.async_block_till_done()
    assert calls == []
    hass.states.async_set("sensor.target", "unavailable")
    with pytest.raises(ServiceValidationError, match="target is unavailable"):
        await runtime.async_submit_occurrence("morning", "missing-target", clock[0] + 100)
    assert len(deepcopy(runtime._state)["occurrences"]) == 1


@pytest.mark.parametrize(
    "policy_id,occurrence_id,expiry",
    [
        ("other", "day", 2_000_000_060),
        ("morning", "", 2_000_000_060),
        ("morning", 12, 2_000_000_060),
        ("morning", "x" * 129, 2_000_000_060),
        ("morning", "day", 0),
        ("morning", "day", True),
        ("morning", "day", float("nan")),
    ],
)
async def test_occurrence_validation(runtime_factory, policy_id, occurrence_id, expiry):
    runtime, _ = await runtime_factory(
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "target": {"position": 60},
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    before = deepcopy(runtime._state)
    with pytest.raises(ServiceValidationError):
        await runtime.async_submit_occurrence(policy_id, occurrence_id, expiry)
    assert deepcopy(runtime._state) == before
    assert runtime.fault is None


async def test_policy_enabled_validation(runtime_factory):
    runtime, _ = await runtime_factory()
    with pytest.raises(ServiceValidationError, match="Unknown policy"):
        await runtime.async_set_policy_enabled("other", True)


@pytest.mark.parametrize(
    "manual",
    [
        None,
        [],
        "target",
        {"mode": "not-a-mode", "target": {"position": 50}},
        {"mode": "target", "target": {}},
        {"mode": "target", "target": {"position": 50}, "expires_at": "tomorrow"},
    ],
)
async def test_corrupt_nested_manual_inhibits_without_overwriting(runtime_factory, manual):
    original = []

    async def prepare(runtime):
        data = deepcopy(runtime._state)
        data["modes"]["roof"] = "live"
        data["manuals"]["roof"] = manual
        contents = json.dumps({"version": 1, "data": data})
        original.append(contents)
        Path(runtime._store.path).parent.mkdir(exist_ok=True)
        await asyncio.to_thread(Path(runtime._store.path).write_text, contents)

    runtime, calls = await runtime_factory(initialized=True, before_start=prepare, setup_error=True)
    assert runtime.fault == "storage_error"
    assert await asyncio.to_thread(Path(runtime._store.path).read_text) == original[0]
    assert calls == []


async def test_explicit_empty_request_id_is_rejected(runtime_factory):
    runtime, _ = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    before = deepcopy(runtime._state)
    with pytest.raises(ServiceValidationError, match="1 to 128"):
        await runtime.async_request("roof", target={"position": 50}, request_id="")
    assert deepcopy(runtime._state) == before


async def test_absolute_expiry_retry_after_expiry_returns_original_receipt(runtime_factory, clock):
    runtime, _ = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    deadline = clock[0] + 60
    original = await runtime.async_request(
        "roof", target={"position": 50}, expires_at=deadline, request_id="absolute"
    )
    clock[0] = deadline + 1
    retry = await runtime.async_request(
        "roof", target={"position": 50}, expires_at=deadline, request_id="absolute"
    )
    assert retry == original
    assert runtime.manual("roof") is None


@pytest.mark.parametrize(
    "updates",
    [
        {"requests": {"r": None}},
        {"requests": {"r": {"fingerprint": "bad", "receipt": {}}}},
        {"requests": {"r": {"fingerprint": "a" * 64, "receipt": None}}},
        {
            "requests": {
                "r": {
                    "fingerprint": "a" * 64,
                    "receipt": {
                        "request_id": "other",
                        "resource_id": "roof",
                        "expires_at": None,
                    },
                }
            }
        },
        {
            "requests": {
                "r": {
                    "fingerprint": "a" * 64,
                    "receipt": {
                        "request_id": "r",
                        "resource_id": "",
                        "expires_at": None,
                    },
                }
            }
        },
        {"occurrences": {"bad": None}},
        {"occurrences": {"bad": {"policy_id": "p", "occurrence_id": "day", "expires_at": 1}}},
        {
            "occurrences": {
                json.dumps(["p", "day"]): {
                    "policy_id": "p",
                    "occurrence_id": "day",
                    "expires_at": 1,
                    "skipped": "yes",
                }
            }
        },
        {
            "occurrences": {
                json.dumps(["p", "day"]): {
                    "policy_id": "p",
                    "occurrence_id": "day",
                    "expires_at": 1,
                }
            }
        },
    ],
)
async def test_corrupt_saved_records_are_readable_and_cannot_be_mutated(runtime_factory, updates):
    contents = []

    async def prepare(runtime):
        data = {**deepcopy(runtime._state), **updates}
        contents.append(json.dumps({"version": 1, "data": data}))
        Path(runtime._store.path).parent.mkdir(exist_ok=True)
        await asyncio.to_thread(Path(runtime._store.path).write_text, contents[-1])

    runtime, calls = await runtime_factory(before_start=prepare, setup_error=True)
    assert runtime.fault == "storage_error"
    assert runtime.explain()["fault"] == "storage_error"
    assert runtime.manual("roof") is None
    with pytest.raises(HomeAssistantError) as raised:
        await runtime.async_set_mode("roof", "live")
    assert raised.value.translation_key == "storage_inhibited"
    assert await asyncio.to_thread(Path(runtime._store.path).read_text) == contents[0]
    assert calls == []


async def test_restart_preserves_receipts_occurrences_and_suppression(runtime_factory, clock):
    runtime, _ = await runtime_factory(
        policies={
            "morning": {
                "name": "Morning",
                "kind": "occurrence",
                "target": {"position": 65},
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    receipt = await runtime.async_request(
        "roof",
        target={"position": 50},
        request_id="restart",
        indefinite=True,
    )
    await runtime.async_skip_occurrence("morning", "sleep-in", clock[0] + 100)
    await runtime.async_submit_occurrence("morning", "ordinary", clock[0] + 100)
    await runtime.async_close()
    restored = OperatorRuntime(runtime.hass, runtime.entry)
    try:
        await restored.async_start()
        assert restored.fault is None
        assert restored.manual("roof").target == Target(position=50)
        retried = await restored.async_request(
            "roof",
            target={"position": 50},
            request_id="restart",
            indefinite=True,
        )
        assert retried == receipt
        assert len(deepcopy(restored._state)["occurrences"]) == 2
    finally:
        await restored.async_close()


async def test_request_cannot_admit_after_observe_change(runtime_factory):
    runtime, calls = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_set_mode("roof", "observe")
    with pytest.raises(ServiceValidationError, match="observe"):
        await runtime.async_request("roof", target={"position": 90})
    assert runtime._state["manuals"] == {} and calls == []


async def test_indefinite_manual_target_survives_runtime_restart(runtime_factory, hass, clock):
    runtime, calls = await runtime_factory()
    await runtime.async_set_mode("roof", "live")
    await runtime.async_request(
        "roof", target={"position": 72}, indefinite=True, request_id="indefinite-restart"
    )
    await hass.async_block_till_done()
    assert calls[-1]["data"]["position"] == 72
    assert current_state(runtime)["manuals"]["roof"]["expires_at"] is None
    await runtime.async_close()
    calls.clear()
    clock[0] += 365 * 86400
    restored = OperatorRuntime(hass, runtime.entry)
    try:
        await restored.async_start()
        await hass.async_block_till_done()
        assert restored.manual("roof").expires_at is None
        assert restored.manual("roof").request_id == "indefinite-restart"
        assert calls == [
            {
                "service": "set_cover_position",
                "data": {"entity_id": "cover.raw", "position": 72.0},
            }
        ]
        assert restored.observations["roof"].target == Target(position=0)
    finally:
        await restored.async_close()


async def test_expired_occurrence_never_replays_after_runtime_restart(runtime_factory, hass, clock):
    runtime, calls = await runtime_factory(
        policies={
            "morning": {"name": "Morning", "kind": "occurrence", "target": {"position": 68}},
        }
    )
    await runtime.async_set_mode("roof", "live")
    deadline = clock[0] + 60
    original = await runtime.async_submit_occurrence("morning", "old-day", deadline)
    await hass.async_block_till_done()
    assert calls[-1]["data"]["position"] == 68
    await runtime.async_close()
    calls.clear()
    clock[0] = deadline + 1
    restored = OperatorRuntime(hass, runtime.entry)
    try:
        await restored.async_start()
        await hass.async_block_till_done()
        assert calls == []
        assert restored.decisions["roof"].status == "idle"
        await restored.async_set_policy_enabled("morning", False)
        await restored.async_set_policy_enabled("morning", True)
        retried = await restored.async_submit_occurrence("morning", "old-day", clock[0] + 100)
        await hass.async_block_till_done()
        assert retried["expires_at"] == original["expires_at"]
        assert calls == []
        await restored.async_submit_occurrence("morning", "new-day", clock[0] + 100)
        await hass.async_block_till_done()
        assert calls == [
            {
                "service": "set_cover_position",
                "data": {"entity_id": "cover.raw", "position": 68.0},
            }
        ]
    finally:
        await restored.async_close()


async def test_release_manual_resumes_surviving_policy_command(runtime_factory, hass, clock):
    runtime, calls = await runtime_factory(
        policies={
            "daytime": {"name": "Daytime", "kind": "state", "target": {"position": 35}},
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert calls[-1]["data"]["position"] == 35
    clock[0] += 31
    await runtime.async_request("roof", target={"position": 81})
    await hass.async_block_till_done()
    assert calls[-1]["data"]["position"] == 81
    assert runtime.manual("roof").target == Target(position=81)
    clock[0] += 31
    await runtime.async_release("roof")
    await hass.async_block_till_done()
    assert [call["data"]["position"] for call in calls] == [35, 81, 35]
    assert runtime.manual("roof") is None
    assert current_state(runtime)["manuals"] == {}
    assert runtime.policy_enabled("daytime") is True
    assert runtime.decisions["roof"].target == Target(position=35)


async def test_observe_rejects_occurrence_without_saving_or_actuating(runtime_factory, hass, clock):
    runtime, calls = await runtime_factory(
        policies={
            "morning": {"name": "Morning", "kind": "occurrence", "target": {"position": 65}},
        }
    )
    before = current_state(runtime)
    with pytest.raises(ServiceValidationError, match="observe"):
        await runtime.async_submit_occurrence("morning", "observe-day", clock[0] + 600)
    await hass.async_block_till_done()
    assert runtime.mode("roof") == "observe"
    assert deepcopy(runtime._state) == before
    assert current_state(runtime) == before
    assert deepcopy(runtime._state)["occurrences"] == {}
    assert calls == []


async def test_manual_expiry_exact_deadline_dispatches_current_policy_target(
    runtime_factory, hass, clock
):
    hass.states.async_set("sensor.target", "schedule", {"position": 20})
    runtime, calls = await runtime_factory(
        policies={
            "daytime": {
                "name": "Daytime",
                "kind": "state",
                "target_entity": "sensor.target",
                "target_attribute": "position",
            },
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert [call["data"]["position"] for call in calls] == [20]
    clock[0] += 31
    receipt = await runtime.async_request("roof", target={"position": 81}, duration=60)
    await hass.async_block_till_done()
    assert [call["data"]["position"] for call in calls] == [20, 81]
    clock[0] = receipt["expires_at"] - 0.1
    hass.states.async_set("sensor.target", "schedule", {"position": 65})
    await hass.async_block_till_done()
    assert runtime.manual("roof").target == Target(position=81)
    assert [call["data"]["position"] for call in calls] == [20, 81]
    clock[0] = receipt["expires_at"]
    # Identical telemetry wakes the real report-event subscription at the deadline.
    hass.states.async_set("sensor.target", "schedule", {"position": 65})
    await hass.async_block_till_done()
    assert runtime.manual("roof") is None
    assert runtime.decisions["roof"].target == Target(position=65)
    assert [call["data"]["position"] for call in calls] == [20, 81, 65]


@pytest.mark.parametrize("origin", ["user", "parent", "none", "operator_correlated"])
async def test_raw_context_never_creates_or_renews_manual_ownership(
    runtime_factory, hass, clock, origin
):
    """Origin metadata on raw changes is evidence, never managed-command admission."""
    contexts = []

    async def raw_position_service(call):
        contexts.append(call.context)
        hass.states.async_set(
            "cover.raw",
            "open",
            {"supported_features": 15, "current_position": call.data["position"]},
            context=call.context,
        )

    hass.services.async_register("cover", "set_cover_position", raw_position_service)
    runtime, _ = await runtime_factory(
        policies={
            "daytime": {"name": "Daytime", "kind": "state", "target": {"position": 70}},
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    assert contexts  # An actual operator dispatch supplied the correlation context.
    operator_context = contexts[-1]
    assert runtime.manual("roof") is None

    def context():
        if origin == "user":
            return Context(user_id="a" * 32)
        if origin == "parent":
            return Context(parent_id="01J00000000000000000000000")
        if origin == "operator_correlated":
            return operator_context
        return None

    async def raw_changes():
        hass.states.async_set(
            "cover.raw",
            "open",
            {"supported_features": 15, "current_position": 37},
            context=context(),
        )
        await hass.async_block_till_done()
        assert runtime.observations["roof"].target == Target(position=37)
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": "cover.raw", "position": 38},
            blocking=True,
            context=context(),
        )
        await hass.async_block_till_done()
        assert runtime.observations["roof"].target == Target(position=38)
        # Repeat the unchanged report as well as the state/attribute transition.
        hass.states.async_set(
            "cover.raw",
            "open",
            {"supported_features": 15, "current_position": 38},
            context=context(),
        )
        await hass.async_block_till_done()

    without_manual = current_state(runtime)
    await raw_changes()
    assert runtime.manual("roof") is None
    assert deepcopy(runtime._state)["manuals"] == {}
    assert deepcopy(runtime._state)["requests"] == {}
    assert current_state(runtime) == without_manual

    clock[0] += 31
    receipt = await runtime.async_request(
        "roof", target={"position": 52}, duration=120, request_id="explicit-managed-command"
    )
    await hass.async_block_till_done()
    operator_context = contexts[-1]
    accepted = current_state(runtime)
    clock[0] += 15
    await raw_changes()
    lease = runtime.manual("roof")
    assert lease.target == Target(position=52)
    assert lease.expires_at == receipt["expires_at"]
    assert lease.request_id == "explicit-managed-command"
    assert current_state(runtime) == accepted
    assert deepcopy(runtime._state) == accepted
