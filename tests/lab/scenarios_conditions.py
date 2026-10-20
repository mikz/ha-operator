"""Native command gates; independent journal proves retained intent and effects."""

from __future__ import annotations

import asyncio
import json

from .runner import ARTIFACTS, eventually
from .scenarios_observe import observe


async def run_conditions(lab):
    ha = lab.ha
    device, gate = "room_flag", "binary_sensor.sim_cellar_policy"
    evidence = {
        "schema": 2,
        "provenance": "simulator_generated",
        "physical_effects": "simulator_observed",
    }
    initial_helpers = {
        row["entity_id"]
        for row in await ha.request("GET", "/api/states")
        if row["entity_id"].startswith(("automation.", "script."))
    }

    async def control(**changes):
        await lab.sim("POST", "/admin/devices/cellar_policy", changes)

    async def commands(after):
        return [
            row
            for row in await lab.journal()
            if row["seq"] > after and row["device_id"] == device and row["kind"] == "command"
        ]

    async def marker():
        return (await lab.sim(path="/health"))["journal_seq"]

    async with lab.scenario("CONDITIONS-NATIVE-CONFIGURATION"):
        await observe(lab, locked=False)
        await control(value=False, available=True)
        await eventually(lambda: ha.state(gate), lambda item: item["state"] == "off")
        await lab.sim(
            "POST",
            f"/admin/devices/{device}",
            {"on": False, "available": True, "refuse_actions": []},
        )
        config = {
            "name": "Lab Conditional Service",
            "kind": "switch",
            "entity_id": f"switch.sim_{device}",
            "command_interval": 0.2,
            "retry_interval": 1,
            "command_conditions": {
                "turn_on": [{"condition": "state", "entity_id": gate, "state": "on"}]
            },
        }
        resource = await ha.add_resource(lab.entry, config)
        managed = await ha.managed(resource, "switch")
        mode = await ha.managed(resource, "select")
        registry = await ha.ws("config/entity_registry/list")
        identities = {
            row["unique_id"]: row["id"]
            for row in registry
            if row.get("config_subentry_id") == resource
        }
        assert not any(key == resource + "_health" for key in identities)
        await ha.service("select", "select_option", {"entity_id": mode, "option": "live"})

    async def explained():
        result = await ha.service(
            "ha_operator", "explain", {"resource_id": resource}, response=True
        )
        return result["service_response"]["resources"][resource]

    async with lab.scenario("CONDITIONS-BLOCKED-ON-RETAINS-DESIRED"):
        start = await marker()
        await ha.service("switch", "turn_on", {"entity_id": managed})
        accepted = await eventually(
            explained,
            lambda item: (
                item["manual"] is not None
                and item["manual"]["target"].get("on") is True
                and item["decision"]["status"] == "restricted"
            ),
        )
        assert accepted["decision"]["reason"] == "command_conditions_blocked"
        assert accepted["conditions"]["command_conditions"]["turn_on"]["status"] == "failed"
        await asyncio.sleep(2.2)
        assert not (await lab.physical(device))["on"] and not await commands(start)
        await control(available=False)
        await eventually(lambda: ha.state(gate), lambda item: item["state"] == "unavailable")
        assert not await commands(start)
        await control(available=True, value=True)
        await eventually(lambda: lab.physical(device), lambda item: item["on"])
        assert (await explained())["manual"] == accepted["manual"]
        await asyncio.sleep(2.2)
        assert len(await commands(start)) == 1
        evidence["blocked"] = accepted

    async with lab.scenario("CONDITIONS-UNCHANGED-TRUTH-NO-DUPLICATE-COMMAND"):
        start = await marker()
        for value in (5, 20):
            await lab.sim("POST", "/admin/devices/cellar_target", {"value": value})
        await asyncio.sleep(2.2)
        assert (await lab.physical(device))["on"] and not await commands(start)
        assert (await explained())["manual"] == accepted["manual"]

    async with lab.scenario("CONDITIONS-OFF-WITH-FALSE-START-GATE"):
        await control(value=False)
        await eventually(lambda: ha.state(gate), lambda item: item["state"] == "off")
        start = await marker()
        await ha.service("switch", "turn_off", {"entity_id": managed})
        await eventually(lambda: lab.physical(device), lambda item: not item["on"])
        await asyncio.sleep(2.2)
        assert len(await commands(start)) == 1

    async with lab.scenario("CONDITIONS-RECONFIGURE-IDENTITY"):
        for conditions in ({}, config["command_conditions"]):
            await ha.reconfigure_subentry(
                lab.entry, resource, "resource", {**config, "command_conditions": conditions}
            )
            registry = await ha.ws("config/entity_registry/list")
            assert {
                row["unique_id"]: row["id"]
                for row in registry
                if row.get("config_subentry_id") == resource
            } == identities
    assert initial_helpers == {
        row["entity_id"]
        for row in await ha.request("GET", "/api/states")
        if row["entity_id"].startswith(("automation.", "script."))
    }
    evidence["events"] = [row for row in await lab.journal() if row["device_id"] == device]
    evidence["status"] = "passed"
    (ARTIFACTS / "conditions-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
