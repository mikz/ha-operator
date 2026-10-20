"""Native conditions, with effects proved by the independent device journal."""

from __future__ import annotations

import asyncio
import json
import re

from .runner import ARTIFACTS, eventually
from .scenarios_observe import observe


async def run_conditions(lab):
    ha = lab.ha
    device, gate, metric = "room_flag", "cellar_policy", "cellar_target"
    raw = f"switch.sim_{device}"
    gate_entity, metric_entity = f"binary_sensor.sim_{gate}", f"sensor.sim_{metric}"
    evidence = {
        "schema": 1,
        "provenance": "simulator_generated",
        "physical_effects": "simulator_observed",
        "boundaries": [],
    }
    initial_helpers = {
        item["entity_id"]
        for item in await ha.request("GET", "/api/states")
        if item["entity_id"].startswith(("automation.", "script."))
    }

    async def control(identifier, **changes):
        await lab.sim("POST", f"/admin/devices/{identifier}", changes)

    async def signal(value):
        await control(gate, value=value)
        await eventually(
            lambda: ha.state(gate_entity), lambda item: item["state"] == ("on" if value else "off")
        )

    async def commands(after):
        return [
            item
            for item in await lab.journal()
            if item["seq"] > after and item["device_id"] == device and item["kind"] == "command"
        ]

    async def marker():
        return (await lab.sim(path="/health"))["journal_seq"]

    async with lab.scenario("CONDITIONS-NATIVE-CONFIGURATION"):
        await observe(lab, locked=False)
        await signal(False)
        await control(device, on=False, available=True, refuse_actions=[])
        await control(metric, value=20, available=True)
        config = {
            "name": "Lab Conditional Service",
            "kind": "switch",
            "entity_id": raw,
            "command_interval": 0.2,
            "retry_interval": 1,
            "command_conditions": {
                "turn_on": [{"condition": "state", "entity_id": gate_entity, "state": "on"}]
            },
            "health_checks": [
                {
                    "alias": "Service telemetry",
                    "conditions": [
                        {"condition": "numeric_state", "entity_id": metric_entity, "above": 10}
                    ],
                    "qualification": [
                        {
                            "condition": "state",
                            "entity_id": gate_entity,
                            "state": "on",
                            "for": {"seconds": 2},
                        }
                    ],
                }
            ],
        }
        resource = await ha.add_resource(lab.entry, config)
        registry = await ha.ws("config/entity_registry/list")
        entities = {
            key: next(
                item["entity_id"] for item in registry if item["unique_id"] == f"{resource}_{key}"
            )
            for key in ("managed", "mode", "desired", "health")
        }
        health_identity = next(
            item["id"] for item in registry if item["unique_id"] == f"{resource}_health"
        )
        await ha.service(
            "select", "select_option", {"entity_id": entities["mode"], "option": "live"}
        )

    async def health(value):
        state = await eventually(
            lambda: ha.state(entities["health"]), lambda item: item["state"] == value
        )
        evidence["boundaries"].append(
            {"health": state, "sequence": await marker(), "physical": await lab.physical(device)}
        )
        return state

    async def explained():
        result = await ha.service(
            "ha_operator", "explain", {"resource_id": resource}, response=True
        )
        return result["service_response"]["resources"][resource]

    async with lab.scenario("CONDITIONS-BLOCKED-ON-RETAINS-DESIRED"):
        await health("waiting")
        start = await marker()
        await ha.service("switch", "turn_on", {"entity_id": entities["managed"]})
        accepted = await eventually(
            explained,
            lambda item: item["manual"] is not None and item["manual"]["target"].get("on") is True,
        )
        await asyncio.sleep(2.2)
        assert not (await lab.physical(device))["on"]
        assert not await commands(start)
        assert accepted["decision"]["status"] == "restricted"
        assert accepted["decision"]["reason"] == "command_conditions_blocked"
        assert accepted["conditions"]["command_conditions"]["turn_on"]["status"] == "failed"
        evidence["blocked_request"] = accepted["manual"]
        evidence["blocked_decision"] = accepted["decision"]
        evidence["blocked_conditions"] = accepted["conditions"]
        await control(gate, available=False)
        await eventually(lambda: ha.state(gate_entity), lambda item: item["state"] == "unavailable")
        await health("unknown")
        assert not await commands(start)
        await control(gate, available=True, value=True)
        await eventually(lambda: ha.state(gate_entity), lambda item: item["state"] == "on")
        await eventually(lambda: lab.physical(device), lambda item: item["on"])
        assert (await explained())["manual"] == accepted["manual"]
        await health("healthy")
        await asyncio.sleep(2.2)
        assert len(await commands(start)) == 1

    async with lab.scenario("CONDITIONS-HEALTH-DOES-NOT-ACTUATE"):
        start = await marker()
        for value, expected in ((5, "unhealthy"), (20, "healthy")):
            await control(metric, value=value)
            await health(expected)
        await control(metric, available=False)
        await health("unknown")
        await control(metric, available=True)
        await health("healthy")
        # Native state-for qualification expires with clock progress, without another source write.
        await signal(False)
        await health("waiting")
        await signal(True)
        await health("waiting")
        await health("healthy")
        await asyncio.sleep(2.2)
        assert (await lab.physical(device))["on"]
        assert not await commands(start)
        assert (await explained())["manual"] == evidence["blocked_request"]
        evidence["health_only_commands"] = []

    async with lab.scenario("CONDITIONS-OFF-WITH-FALSE-START-GATE"):
        await signal(False)
        start = await marker()
        await ha.service("switch", "turn_off", {"entity_id": entities["managed"]})
        await eventually(lambda: lab.physical(device), lambda item: not item["on"])
        await asyncio.sleep(2.2)
        assert len(await commands(start)) == 1

    async with lab.scenario("CONDITIONS-NATIVE-HEALTH-MORE-INFO"):
        await lab.page.goto(ha.base + "/config/entities")
        await lab.page.get_by_placeholder(re.compile(r"Search \d+ entities")).fill(
            entities["health"]
        )
        await lab.page.get_by_role("rowheader", name="Health", exact=True).click()
        more_info = lab.page.locator("ha-more-info-dialog")
        await more_info.get_by_role("dialog").wait_for()
        await more_info.locator("state-info").get_by_text("Health", exact=True).wait_for()
        await more_info.get_by_role("heading", name="History", exact=True).wait_for()
        await lab.page.screenshot(path=str(ARTIFACTS / "conditions-health-more-info.png"))

    async with lab.scenario("CONDITIONS-RECONFIGURE-IDENTITY"):
        edited = {
            **config,
            "health_checks": [
                {
                    "alias": "Edited telemetry",
                    "conditions": config["health_checks"][0]["conditions"],
                }
            ],
        }
        await ha.reconfigure_subentry(lab.entry, resource, "resource", edited)
        registry = await ha.ws("config/entity_registry/list")
        assert (
            next(item["id"] for item in registry if item["unique_id"] == f"{resource}_health")
            == health_identity
        )
        await health("healthy")
        await ha.reconfigure_subentry(
            lab.entry, resource, "resource", {**config, "health_checks": []}
        )
        await eventually(
            lambda: ha.request("GET", "/api/states"),
            lambda rows: not any(item["entity_id"] == entities["health"] for item in rows),
        )
        await ha.reconfigure_subentry(lab.entry, resource, "resource", config)
        registry = await ha.ws("config/entity_registry/list")
        assert (
            next(item["id"] for item in registry if item["unique_id"] == f"{resource}_health")
            == health_identity
        )
        await health("waiting")
        evidence["resource_id"] = resource
        evidence["health_registry_id"] = health_identity
        evidence["configuration"] = config
    assert initial_helpers == {
        item["entity_id"]
        for item in await ha.request("GET", "/api/states")
        if item["entity_id"].startswith(("automation.", "script."))
    }
    evidence["events"] = [
        item for item in await lab.journal() if item["device_id"] in {device, gate, metric}
    ]
    (ARTIFACTS / "conditions-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
