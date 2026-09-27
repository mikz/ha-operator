"""The actual recorder export must pass the offline exact-component replay boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.ha_operator.runtime import OperatorRuntime
from tests.lab.replay import replay_trace
from tests.lab.shadow_trace import load_trace

from .helpers import operator_entry

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components/ha_operator"


async def test_real_recorded_admission_inputs_and_decisions_replay(hass, tmp_path):
    hass.config.config_dir = str(tmp_path / "ha")
    hass.states.async_set(
        "cover.physical_roof", "closed", {"current_position": 0, "supported_features": 15}
    )
    hass.states.async_set("binary_sensor.demand", "off")
    commands = []

    async def capture(call):
        commands.append(dict(call.data))

    hass.services.async_register("cover", "set_cover_position", capture)
    policies = {
        key: {"name": key, "resource_id": "roof", "kind": "state", "target": {"position": target}}
        for key, target in (("policy_a", 30), ("policy_z", 70))
    }
    policies["scoped_policy"] = {
        "name": "Scoped",
        "resource_id": "roof",
        "kind": "occurrence",
        "priority": 2,
        "target": {"position": 45},
    }
    entry = operator_entry(
        policies=policies,
        requirements={
            "air": {
                "name": "Air",
                "activation_entities": ["binary_sensor.demand"],
                "providers": [
                    {
                        "id": "roof",
                        "resource_id": "roof",
                        "target": {"position": 90},
                        "evidence": [
                            {
                                "entity_id": "cover.physical_roof",
                                "attribute": "current_position",
                                "operator": "gte",
                                "value": 85,
                                "kind": "position",
                            }
                        ],
                    }
                ],
            }
        },
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"trace_enabled": True})
    runtime = OperatorRuntime(hass, entry)
    entry.runtime_data = runtime
    try:
        await runtime.async_start()
        await runtime.async_set_mode("roof", "live")
        expiry = (datetime.now(UTC) + timedelta(minutes=2)).timestamp()
        await runtime.async_submit_occurrence("scoped_policy", "occurrence_a", expiry)
        await runtime.async_submit_occurrence("scoped_policy", "occurrence_z", expiry)
        await runtime.async_request("roof", mode="hands_off", duration=60)
        await hass.async_block_till_done()
        hass.states.async_set("binary_sensor.demand", "on")
        await hass.async_block_till_done()
        await runtime.async_release("roof")
        await hass.async_block_till_done()
        await runtime.async_close()
        hass.config_entries.async_update_entry(
            entry, options={"trace_enabled": True, "shadow_lock": True}
        )
        runtime = OperatorRuntime(hass, entry)
        entry.runtime_data = runtime
        await runtime.async_start()
        await hass.async_block_till_done()
        pages, after = [], None
        while True:
            exported = await runtime.async_export_trace(after=after, limit=5)
            pages.append(exported)
            if not exported["more"]:
                break
            after = exported["next_after"]
        path = tmp_path / "trace.json"
        path.write_text(json.dumps({"schema": 1, "pages": pages}))
        trace = load_trace(path)
        assert trace.report["replay_complete"], trace.report
        assert len(pages) > 1
        assert len({record["session_id"] for record in trace.records}) == 2
        assert any(record["kind"] == "admission" for record in trace.records)
        assert any(record["kind"] == "input" for record in trace.records)
        # This fixture package is assembled from the actual modules loaded above.
        # Final release acceptance separately insists on the immutable dist ZIP.
        archive = tmp_path / "component.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for source in sorted(COMPONENT.rglob("*")):
                if source.is_file() and source.suffix in {".py", ".json", ".yaml"}:
                    package.write(source, source.relative_to(COMPONENT).as_posix())
        digest = hashlib.sha256(await asyncio.to_thread(archive.read_bytes)).hexdigest()
        report = replay_trace(path, archive, digest)
        assert report["status"] == "passed", report
        assert report["comparisons"] and all(row["matched"] for row in report["comparisons"])
        assert report["physical_effects"] == "not_observed"
    finally:
        await runtime.async_close()


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("cover", "position", "40"),
        ("switch", "on", "on"),
        ("fan", "percentage", "50"),
        ("fan", "direction", "reverse"),
    ],
)
async def test_native_policy_target_field_survives_sanitized_export(
    hass, tmp_path, kind, field, value
):
    """Exercise the production sanitizer and journal for every admitted target field."""
    hass.config.config_dir = str(tmp_path)
    entity_id = f"{kind}.physical"
    hass.states.async_set(
        entity_id,
        "closed" if kind == "cover" else "on",
        {
            "supported_features": 63,
            "current_position": 0,
            "percentage": 0,
            "direction": "forward",
        },
    )
    hass.states.async_set("sensor.desired", value)
    literals = ["direction", "percentage", "profile"]
    extras = [f"sensor.literal_{index}" for index in range(len(literals))]
    for entity, literal in zip(extras, literals, strict=True):
        hass.states.async_set(entity, literal)
    resource = {"name": "Device", "kind": kind, "entity_id": entity_id}
    if kind == "fan":
        resource["default_target"] = {"on": True, "percentage": 50, "direction": "forward"}
    entry = operator_entry(
        resources={"device": resource},
        policies={
            "dynamic": {
                "name": "Dynamic",
                "resource_id": "device",
                "kind": "state",
                "target_entity": "sensor.desired",
                "target_field": field,
            }
        },
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            "trace_enabled": True,
            "shadow_lock": True,
            "trace_entities": extras,
        },
    )
    runtime = OperatorRuntime(hass, entry)
    entry.runtime_data = runtime
    try:
        await runtime.async_start()
        await hass.async_block_till_done()
        exported = await runtime.async_export_trace(limit=1000)
        path = tmp_path / "trace.json"
        path.write_text(json.dumps(exported))
        trace = load_trace(path)
        assert trace.report["replay_complete"], trace.report["reasons"]
        assert next(iter(trace.config["policies"].values()))["target_field"] == field
        states = {record["data"]["state"] for record in trace.records if record["kind"] == "input"}
        assert set(literals) <= states
    finally:
        await runtime.async_close()
