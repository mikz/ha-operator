"""Existing numeric entities parameterize admission, never accepted deadlines."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import json
from copy import deepcopy
from time import process_time
from unittest.mock import patch

import pytest
from homeassistant.components.trace.const import DATA_TRACE
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.ha_operator.configuration import (
    ConfigurationError,
    validate_policy,
    validate_resource,
)
from tests.integration.helpers import async_add_physical_cover, async_setup_operator
from tests.integration.test_config_flow import _start, native_sources  # noqa: F401
from tests.integration.test_runtime_reconciliation import (  # noqa: F401
    cover_data,
    reported,
    runtime_factory,
)


@pytest.mark.parametrize(
    ("value", "unit", "seconds"),
    [(17, "min", 1020), (3, "h", 10800), (120, "min", 7200), (5, "min", 300), (250, "ms", 0.25)],
)
async def test_manual_duration_is_sampled_once_and_replay_never_resamples(
    hass, runtime_factory, freezer, value, unit, seconds
):
    reported(hass)
    hass.states.async_set("input_number.duration", str(value), {"unit_of_measurement": unit})
    factory, _ = runtime_factory
    runtime = await factory(
        resources={"roof": cover_data(manual_duration_entity="input_number.duration")}
    )
    await runtime.async_set_mode("roof", "live")
    receipt = await runtime.async_request("roof", target={"position": 50}, request_id="same")
    assert receipt["expires_at"] == dt_util.utcnow().timestamp() + seconds
    assert runtime.reason_details("roof")["duration_source"]["seconds"] == seconds
    assert (
        runtime.reason_details("roof")["duration_source"]["source_entity"]
        == "input_number.duration"
    )
    await hass.async_block_till_done()
    with patch.object(runtime, "_recompute", wraps=runtime._recompute) as recompute:
        hass.states.async_set("input_number.duration", "unavailable", {"unit_of_measurement": unit})
        await hass.async_block_till_done()
        assert recompute.call_count == 0
    assert runtime.manual("roof").expires_at == receipt["expires_at"]
    replay = await runtime.async_request("roof", target={"position": 50}, request_id="same")
    assert replay == receipt
    with pytest.raises(ServiceValidationError, match="Duration source"):
        await runtime.async_request("roof", target={"position": 50}, request_id="new")
    assert runtime.manual("roof").request_id == "same"


@pytest.mark.parametrize(
    ("state", "unit"),
    [
        ("unknown", "min"),
        ("unavailable", "min"),
        ("nan", "min"),
        ("inf", "min"),
        ("0", "min"),
        ("-1", "min"),
        ("2", None),
        ("2", "°C"),
        ("1e308", "h"),
    ],
)
async def test_invalid_source_blocks_default_request_but_not_explicit_or_stop(
    hass, runtime_factory, state, unit
):
    reported(hass)
    hass.states.async_set("number.duration", state, {"unit_of_measurement": unit})
    factory, commands = runtime_factory
    runtime = await factory(
        resources={"roof": cover_data(manual_duration_entity="number.duration", manual_duration=70)}
    )
    await runtime.async_set_mode("roof", "live")
    with pytest.raises(ServiceValidationError):
        await runtime.async_request("roof", target={"position": 50})
    assert runtime.manual("roof") is None
    await runtime.async_request("roof", target={"position": 50}, duration=90)
    explicit_expiry = dt_util.utcnow().timestamp() + 30
    receipt = await runtime.async_request(
        "roof", target={"position": 50}, expires_at=explicit_expiry
    )
    assert receipt["expires_at"] == explicit_expiry
    await runtime.async_request("roof", target={"position": 50}, indefinite=True)
    assert runtime.manual("roof").expires_at is None
    await runtime.async_stop("roof")
    assert any(command[0] == "stop_cover" for command in commands)
    assert runtime.reason_details("roof")["duration_source"]["fallback_seconds"] == 70


async def test_native_occurrence_source_failure_preserves_previous_accepted_request(
    hass, runtime_factory, freezer
):
    reported(hass)
    hass.states.async_set("input_number.duration", "5", {"unit_of_measurement": "min"})
    factory, _ = runtime_factory
    runtime = await factory(
        policies={
            "p": {
                "name": "Timed",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 50},
                "triggers": [{"trigger": "event", "event_type": "admit"}],
                "duration_entity": "input_number.duration",
            }
        }
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    hass.bus.async_fire("admit")
    await hass.async_block_till_done()
    previous = deepcopy(runtime._state["occurrences"])
    assert next(iter(previous.values()))["expires_at"] == dt_util.utcnow().timestamp() + 300
    assert runtime.reason_details("roof")["duration_source"]["source_value"] == "5"
    hass.states.async_set("input_number.duration", "0", {"unit_of_measurement": "min"})
    hass.bus.async_fire("admit")
    await hass.async_block_till_done()
    assert runtime._state["occurrences"] == previous
    assert runtime.explain()["policies"]["p"]["duration_error"] == "duration_source_unavailable"
    hass.states.async_set("input_number.duration", "2", {"unit_of_measurement": "min"})
    hass.bus.async_fire("admit")
    await hass.async_block_till_done()
    assert len(runtime._state["occurrences"]) == 1
    assert runtime._state["occurrences"] != previous
    assert "duration_error" not in runtime.explain()["policies"]["p"]


def test_duration_configuration_boundary():
    resource = validate_resource(cover_data(manual_duration_entity="input_number.duration"))
    assert resource["manual_duration"] == 1800
    with pytest.raises(ConfigurationError):
        validate_resource(cover_data(manual_duration_entity="sensor.duration"))
    with pytest.raises(ConfigurationError):
        validate_policy(
            {
                "name": "Bad",
                "kind": "occurrence",
                "resource_id": "roof",
                "target": {"position": 50},
                "duration_entity": "input_number.duration",
            },
            {"roof": resource},
        )


async def test_duration_sources_roundtrip_real_native_forms(hass, native_sources):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ha_operator.const import DOMAIN

    hass.states.async_set("input_number.duration", "5", {"unit_of_measurement": "min"})
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    resource_form = await _start(hass, entry, "resource")
    data = {
        "name": "Roof",
        "kind": "cover",
        "entity_id": "cover.raw",
        "manual_duration_entity": "input_number.duration",
    }
    result = await hass.config_entries.subentries.async_configure(resource_form["flow_id"], data)
    assert result["type"] == "create_entry"
    resource = next(iter(entry.get_subentries_of_type("resource")))
    policy_form = await _start(hass, entry, "policy")
    policy = {
        "name": "Timed",
        "kind": "occurrence",
        "resource_id": resource.subentry_id,
        "target": {"position": 50},
        "triggers": [{"trigger": "event", "event_type": "admit"}],
        "duration_entity": "input_number.duration",
    }
    result = await hass.config_entries.subentries.async_configure(policy_form["flow_id"], policy)
    assert result["type"] == "create_entry"
    for subentry in entry.subentries.values():
        stored = json.loads(json.dumps(dict(subentry.data)))
        form = await entry.start_subentry_reconfigure_flow(hass, subentry.subentry_id)
        result = await hass.config_entries.subentries.async_configure(form["flow_id"], stored)
        assert result["reason"] == "reconfigure_successful"


async def test_native_reason_trace_and_reload_preserve_admitted_deadline(hass, tmp_path):
    await async_add_physical_cover(hass)
    hass.states.async_set("input_number.duration", "17", {"unit_of_measurement": "min"})
    entry = await async_setup_operator(
        hass,
        tmp_path,
        resources={
            "roof": cover_data(
                entity_id="cover.physical_roof", manual_duration_entity="input_number.duration"
            )
        },
    )
    runtime = entry.runtime_data
    await runtime.async_set_mode("roof", "live")
    receipt = await runtime.async_request("roof", target={"position": 50})
    await hass.async_block_till_done()
    traces = hass.data[DATA_TRACE]["script.roof_reason"].runs
    sources = [
        trace.as_extended_dict()["trace"]["sequence/0"][0]["changed_variables"]["source"]
        for trace in traces.values()
    ]
    assert any(source.get("duration_source", {}).get("seconds") == 1020 for source in sources)
    hass.states.async_set("input_number.duration", "unavailable", {"unit_of_measurement": "min"})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.manual("roof").expires_at == receipt["expires_at"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_duration_parameter_updates_add_no_reconciliation_work(hass, runtime_factory):
    reported(hass)
    hass.states.async_set("input_number.duration", "17", {"unit_of_measurement": "min"})
    factory, commands = runtime_factory
    runtime = await factory(
        resources={"roof": cover_data(manual_duration_entity="input_number.duration")}
    )
    await runtime.async_set_mode("roof", "live")
    await hass.async_block_till_done()
    with (
        patch.object(
            runtime, "_duration_from_entity", wraps=runtime._duration_from_entity
        ) as sample,
        patch.object(runtime, "_recompute", wraps=runtime._recompute) as recompute,
    ):
        started = process_time()
        for value in range(1, 101):
            hass.states.async_set(
                "input_number.duration", str(value), {"unit_of_measurement": "min"}
            )
        await hass.async_block_till_done()
        print(
            json.dumps(
                {
                    "parameter_updates": 100,
                    "duration_samples": sample.call_count,
                    "recomputations": recompute.call_count,
                    "commands": len(commands),
                    "cpu_ms": (process_time() - started) * 1000,
                }
            )
        )
        assert sample.call_count == recompute.call_count == len(commands) == 0
        for _ in range(20):
            await runtime.async_request("roof", mode="hands_off")
        assert sample.call_count == 20
