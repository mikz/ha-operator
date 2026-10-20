"""Number/select reconciliation against independent physical effects."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from .runner import ARTIFACTS, eventually
from .scenarios_observe import observe


async def run_typed(lab):
    ha = lab.ha
    evidence = {"schema": 1, "physical_effects": "simulator_observed", "snapshots": []}
    await observe(lab, locked=False)
    protected = {
        "number.sim_setpoint",
        "sensor.sim_setpoint_desired",
        "sensor.lab_setpoint_observer",
    }
    protected_registry = {
        row["entity_id"]: row["id"]
        for row in await ha.ws("config/entity_registry/list")
        if row["entity_id"] in protected
    }
    assert set(protected_registry) == protected

    async def control(device, **changes):
        await lab.sim("POST", f"/admin/devices/{device}", changes)

    async def explained(resource):
        return (
            await ha.service("ha_operator", "explain", {"resource_id": resource}, response=True)
        )["service_response"]["resources"][resource]

    async def entity(resource, key):
        registry = await ha.ws("config/entity_registry/list")
        return next(
            row["entity_id"] for row in registry if row.get("unique_id") == resource + "_" + key
        )

    async def mode(resource, value):
        await ha.service(
            "select",
            "select_option",
            {"entity_id": await entity(resource, "mode"), "option": value},
        )

    async def commands(device, after):
        return [
            row
            for row in await lab.journal()
            if row["seq"] > after and row["device_id"] == device and row["kind"] == "command"
        ]

    async with lab.scenario("TYPED-NATIVE-RESOURCES"):
        numeric = await ha.add_resource(
            lab.entry,
            {
                "name": "Lab numeric setpoint",
                "kind": "number",
                "entity_id": "number.sim_setpoint",
                "unit_of_measurement": "°C",
                "tolerance": 0.01,
                "retry_interval": 1,
                "command_interval": 0.2,
            },
        )
        selected = await ha.add_resource(
            lab.entry,
            {
                "name": "Lab selected mode",
                "kind": "select",
                "entity_id": "select.sim_operating_mode",
                "retry_interval": 1,
                "command_interval": 0.2,
            },
        )
        number_entity, select_entity = (
            await entity(numeric, "managed"),
            await entity(selected, "managed"),
        )
        numeric_policy = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "External numeric desired",
                "kind": "state",
                "resource_id": numeric,
                "target_entity": "sensor.sim_setpoint_desired",
                "target_field": "value",
            },
        )
        low = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Quiet fallback",
                "kind": "state",
                "resource_id": selected,
                "priority": 0,
                "target": {"option": "quiet"},
            },
        )
        high = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Boost desired",
                "kind": "state",
                "resource_id": selected,
                "priority": 20,
                "target": {"option": "boost"},
            },
        )
        for resource in (numeric, selected):
            await mode(resource, "live")

    async with lab.scenario("TYPED-INDEPENDENT-OBSERVATION"):
        await eventually(lambda: lab.physical("setpoint"), lambda item: item["value"] == 20.125)
        await eventually(
            lambda: lab.physical("operating_mode"), lambda item: item["value"] == "boost"
        )
        await eventually(
            lambda: ha.state(number_entity), lambda item: float(item["state"]) == 20.125
        )
        await eventually(lambda: ha.state(select_entity), lambda item: item["state"] == "boost")
        assert (await explained(numeric))["selection"]["source_id"] == numeric_policy
        await control("setpoint", refuse_actions=["set_value"], value=-1.25)
        await eventually(
            lambda: ha.state(number_entity), lambda item: float(item["state"]) == -1.25
        )
        result = await explained(numeric)
        assert result["decision"]["target"]["value"] == 20.125
        assert (await lab.physical("setpoint"))["value"] == -1.25
        evidence["snapshots"].append(result)
        await control("setpoint", refuse_actions=[])
        await eventually(lambda: lab.physical("setpoint"), lambda item: item["value"] == 20.125)

    async with lab.scenario("TYPED-UNIT-DRIFT-INHIBITS-DISPATCH"):
        await control("setpoint", unit="°F", value=20.125)
        await eventually(
            lambda: ha.state(number_entity), lambda item: item["state"] == "unavailable"
        )
        marker = (await lab.sim())["journal_seq"]
        await asyncio.sleep(2.2)
        assert not await commands("setpoint", marker)
        evidence["snapshots"].append(await explained(numeric))
        await control("setpoint", unit="°C", value=18)
        await eventually(lambda: lab.physical("setpoint"), lambda item: item["value"] == 20.125)

    async with lab.scenario("TYPED-DYNAMIC-OPTIONS-ARBITRATION"):
        await control("operating_mode", options=["quiet"], value="quiet")
        result = await eventually(
            lambda: explained(selected), lambda item: item["selection"].get("source_id") == low
        )
        assert result["decision"]["target"]["option"] == "quiet"
        evidence["snapshots"].append(result)
        await control("operating_mode", options=["quiet", "boost"])
        await eventually(
            lambda: explained(selected), lambda item: item["selection"].get("source_id") == high
        )
        await eventually(
            lambda: lab.physical("operating_mode"), lambda item: item["value"] == "boost"
        )

    async with lab.scenario("TYPED-INVALID-ACCEPTED-MANUAL-RETAINS-LEASE"):
        await ha.service("select", "select_option", {"entity_id": select_entity, "option": "boost"})
        original = (await explained(selected))["manual"]
        await control("operating_mode", options=["quiet"], value="quiet")
        await eventually(
            lambda: ha.state("select.sim_operating_mode"),
            lambda item: item["attributes"]["options"] == ["quiet"],
        )
        marker = (await lab.sim())["journal_seq"]
        await asyncio.sleep(2.2)
        assert (await explained(selected))["manual"] == original
        assert not await commands("operating_mode", marker)
        await control("operating_mode", options=["quiet", "boost"])
        await eventually(
            lambda: lab.physical("operating_mode"), lambda item: item["value"] == "boost"
        )
        assert (await explained(selected))["manual"] == original
        await ha.service("ha_operator", "release", {"resource_id": selected})

    async with lab.scenario("TYPED-AWAITED-NATIVE-ACTOR-SUPERSESSION"):
        await control("cellar_demand", value=False)
        await control("room_lighting", on=False)
        await eventually(
            lambda: ha.state("binary_sensor.sim_cellar_demand"), lambda item: item["state"] == "off"
        )
        awaited = await ha.add_resource(
            lab.entry,
            {
                "name": "Lab awaited correction",
                "kind": "switch",
                "entity_id": "switch.lab_awaited_actor",
                "retry_interval": 1,
                "command_interval": 0.2,
            },
        )
        awaited_entity = await entity(awaited, "managed")
        await mode(awaited, "live")
        marker = (await lab.sim())["journal_seq"]
        await ha.service("switch", "turn_on", {"entity_id": awaited_entity})
        await eventually(
            lambda: explained(awaited), lambda item: item["decision"]["status"] == "applying"
        )
        await ha.service("switch", "turn_off", {"entity_id": awaited_entity})
        await asyncio.sleep(2.2)
        assert not await commands("room_lighting", marker)
        assert not (await lab.physical("room_lighting"))["on"]
        # Supersession cannot roll back a nested native action already waiting.
        # It must retain the awaited call, then correct the independent feedback.
        await control("cellar_demand", value=True)
        events = await eventually(
            lambda: commands("room_lighting", marker), lambda rows: len(rows) >= 2
        )
        assert [row["data"]["action"] for row in events] == ["turn_on", "turn_off"]
        await eventually(lambda: lab.physical("room_lighting"), lambda item: not item["on"])
        assert (await explained(awaited))["manual"]["target"]["on"] is False
        evidence["awaited_supersession_commands"] = events
        await control("cellar_demand", value=False)

    async with lab.scenario("TYPED-NATIVE-MORE-INFO"):
        for identifier, name in (
            (number_entity, "Lab numeric setpoint"),
            (select_entity, "Lab selected mode"),
        ):
            await lab.page.goto(ha.base + "/config/entities")
            await lab.page.get_by_placeholder(re.compile(r"Search \d+ entities")).fill(identifier)
            await lab.page.get_by_role("rowheader", name=name, exact=True).click()
            await lab.page.get_by_role("dialog").last.wait_for(state="visible")
            await lab.page.screenshot(
                animations="disabled",
                path=str(
                    ARTIFACTS
                    / (
                        "typed-number-more-info.png"
                        if identifier == number_entity
                        else "typed-select-more-info.png"
                    )
                ),
            )
    async with lab.scenario("TYPED-RENDERED-NATIVE-POLICY-EDITOR"):
        from .native_editor import exercise_policy_conditions

        await exercise_policy_conditions(lab, "External numeric desired", numeric_policy)
    async with lab.scenario("TYPED-RENDERED-NATIVE-TRIGGER-EDITOR"):
        from .native_editor import exercise_occurrence_editor, prove_native_occurrence

        title = "Lab native event occurrence"
        occurrence = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": title,
                "kind": "occurrence",
                "resource_id": selected,
                "priority": 200,
                "target": {"option": "quiet"},
            },
        )
        await ha.service(
            "switch", "turn_off", {"entity_id": await ha.managed(occurrence, "switch")}
        )
        await exercise_occurrence_editor(lab, title, occurrence, "lab_typed_native")
        marker = (await lab.sim())["journal_seq"]
        evidence["native_occurrence"] = await prove_native_occurrence(
            lab, selected, occurrence, "lab_typed_native"
        )
        effects = await commands("operating_mode", marker)
        assert {row["data"]["option"] for row in effects} == {"quiet", "boost"}
        evidence["native_occurrence"]["commands"] = effects

    async with lab.scenario("TYPED-UNCHANGED-RECORDER-ROWS"):
        # Limit this window to these two settled resources, excluding other lab
        # scenarios' leases. Recorder is real; fixture profiling excludes it.
        await asyncio.sleep(6)
        entities = sorted(
            row["entity_id"]
            for row in await ha.ws("config/entity_registry/list")
            if row.get("config_subentry_id") in {numeric, selected}
        )
        start = datetime.now(UTC)
        end = start + timedelta(seconds=4)
        path = (
            "/api/history/period/"
            + start.isoformat()
            + "?"
            + urlencode(
                {
                    "filter_entity_id": ",".join(entities),
                    "end_time": end.isoformat(),
                    "skip_initial_state": "1",
                    "significant_changes_only": "0",
                }
            )
        )

        async def counts():
            result = dict.fromkeys(entities, 0)
            for group in await ha.request("GET", path):
                for row in group:
                    result[row["entity_id"]] += 1
            return result

        baseline_counts = await counts()
        for _ in range(3):
            await control("setpoint_desired", value=20.125)
            await ha.service("ha_operator", "reconcile", {"resource_id": numeric})
        await asyncio.sleep(max(0, (end - datetime.now(UTC)).total_seconds()) + 6)
        current_counts = await counts()
        assert not any(baseline_counts.values()), baseline_counts
        assert current_counts == baseline_counts, current_counts
        recorder = {
            "schema": 1,
            "status": "passed",
            "scope": (
                "Real Recorder history for two settled typed resource entities, including Reason; "
                "identical simulator input and explicit native reconciliation"
            ),
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "settle_seconds": 6,
                "input_updates": 3,
            },
            "entities": entities,
            "baseline_counts": baseline_counts,
            "current_counts": current_counts,
            "additional_rows": sum(current_counts.values()) - sum(baseline_counts.values()),
        }
        (ARTIFACTS / "recorder-unchanged-evidence.json").write_text(
            json.dumps(recorder, indent=2) + "\n"
        )
    async with lab.scenario("TYPED-REFERENCED-ACTORS-AND-CONSUMERS-SURVIVE"):
        from .native_editor import delete_subentry, edit_resource_actor

        before = {
            row["unique_id"]: row["id"]
            for row in await ha.ws("config/entity_registry/list")
            if row.get("config_subentry_id") == numeric
        }
        await edit_resource_actor(lab, "Lab numeric setpoint", "number.sim_setpoint_aux")
        await eventually(lambda: lab.physical("setpoint_aux"), lambda item: item["value"] == 20.125)
        assert {
            row["unique_id"]: row["id"]
            for row in await ha.ws("config/entity_registry/list")
            if row.get("config_subentry_id") == numeric
        } == before
        await edit_resource_actor(lab, "Lab numeric setpoint", "number.sim_setpoint")
        await delete_subentry(lab, "External numeric desired")
        await delete_subentry(lab, "Lab numeric setpoint")
        assert {
            row["entity_id"]: row["id"]
            for row in await ha.ws("config/entity_registry/list")
            if row["entity_id"] in protected
        } == protected_registry
        for identifier in protected:
            assert (await ha.state(identifier))["state"] not in {"unavailable", "unknown"}
        evidence["preserved_external_registry"] = protected_registry
    evidence["events"] = [
        row for row in await lab.journal() if row["device_id"] in {"setpoint", "operating_mode"}
    ]
    evidence["status"] = "passed"
    (ARTIFACTS / "typed-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
