"""Native Light/Adaptive Lighting composition with independent device feedback."""

import asyncio
import json
import os
import re

from playwright.async_api import expect

from .hap import HAPClient
from .runner import ARTIFACTS, STATE, eventually
from .scenarios_observe import observe


async def run_light(lab):
    ha = lab.ha
    evidence = {"schema": 1, "adaptive_lighting_version": "1.32.0", "snapshots": []}
    await observe(lab, locked=False)

    async def explained():
        return (
            await ha.service("ha_operator", "explain", {"resource_id": resource}, response=True)
        )["service_response"]["resources"][resource]

    async def control(**values):
        await lab.sim("POST", "/admin/devices/ambient_light", values)

    async def commands(after):
        return [
            row
            for row in await lab.journal()
            if row["seq"] > after
            and row["device_id"] == "ambient_light"
            and row["kind"] == "command"
        ]

    async with lab.scenario("LIGHT-NATIVE-AL-BARE-ON"):
        await eventually(
            lambda: ha.state("switch.adaptive_lighting_lab_ambient"),
            lambda state: state["state"] == "on",
        )
        resource = await ha.add_resource(
            lab.entry,
            {
                "name": "Lab ambient facade",
                "kind": "light",
                "entity_id": "light.sim_ambient_light",
                "manual_duration_entity": "number.sim_light_duration",
                "retry_interval": 1,
                "command_interval": 0.2,
            },
        )
        managed = await ha.managed(resource, "light")
        registry = await ha.ws("config/entity_registry/list")
        mode = next(
            row["entity_id"] for row in registry if row.get("unique_id") == resource + "_mode"
        )
        await ha.service("select", "select_option", {"entity_id": mode, "option": "live"})
        policy = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Lab ambient automatic",
                "kind": "state",
                "resource_id": resource,
                "target": {"on": True},
            },
        )
        await eventually(
            lambda: lab.physical("ambient_light"),
            lambda state: state["on"] and state["brightness"] == 153,
        )
        result = await explained()
        assert result["manual"] is None
        evidence["snapshots"].append(result)

    async with lab.scenario("LIGHT-NATIVE-AL-RESUME-AUTOMATION"):
        registry = await ha.ws("config/entity_registry/list")
        desired = next(
            row["entity_id"] for row in registry if row.get("unique_id") == resource + "_desired"
        )
        await ha.request(
            "POST",
            "/api/config/automation/config/lab_light_resume",
            {
                "alias": "Lab light resume",
                "mode": "single",
                "triggers": [
                    {
                        "trigger": "state",
                        "entity_id": desired,
                        "attribute": "source_kind",
                        "from": "manual",
                        "to": "policy",
                    }
                ],
                "conditions": [
                    {
                        "condition": "template",
                        "value_template": f"{{{{ state_attr('{desired}', 'source_id') == "
                        f"'{policy}' and "
                        f"state_attr('{desired}', 'target') == {{'on': true}} and "
                        "is_state('light.sim_ambient_light', 'on') }}",
                    }
                ],
                "actions": [
                    {
                        "action": "adaptive_lighting.set_manual_control",
                        "data": {
                            "entity_id": "switch.adaptive_lighting_lab_ambient",
                            "lights": ["light.sim_ambient_light"],
                            "manual_control": False,
                        },
                    }
                ],
            },
        )
        await eventually(
            lambda: ha.request("GET", "/api/states/automation.lab_light_resume", allow_error=True),
            lambda state: state.get("state") == "on",
        )
        evidence["resume_automation"] = "automation.lab_light_resume"

    async with lab.scenario("LIGHT-EXPLICIT-COLOR-FROZEN-EXPIRY"):
        await ha.service(
            "light",
            "turn_on",
            {
                "entity_id": managed,
                "brightness": 64,
                "hs_color": [25, 70],
            },
        )
        await eventually(
            lambda: lab.physical("ambient_light"),
            lambda state: state["brightness"] == 64 and state["hs_color"] == [25, 70],
        )
        accepted = (await explained())["manual"]
        assert accepted["target"]["brightness"] == 64
        await lab.sim("POST", "/admin/devices/light_duration", {"value": 90})
        assert (await explained())["manual"] == accepted
        evidence["snapshots"].append(await explained())
        await eventually(explained, lambda state: state["manual"] is None, timeout=12)
        assert (await explained())["selection"]["source_id"] == policy
        await eventually(
            lambda: lab.physical("ambient_light"),
            lambda state: (
                state["brightness"] == 153 and state["color_temp_kelvin"] == 3500 and state["on"]
            ),
        )
        evidence["resumed_automatic_physical"] = await lab.physical("ambient_light")
        # The native automation restores AL ownership without synthesizing OFF/ON.
        marker = (await lab.sim())["journal_seq"]
        await asyncio.sleep(2.2)
        assert not await commands(marker)

    async with lab.scenario("LIGHT-NEW-MANUAL-DURING-NATIVE-AL-RESUME"):
        await ha.service(
            "adaptive_lighting",
            "change_switch_settings",
            {
                "entity_id": "switch.adaptive_lighting_lab_ambient",
                "separate_turn_on_commands": True,
                "send_split_delay": 1500,
            },
        )
        await lab.sim("POST", "/admin/devices/light_duration", {"value": 3})
        await eventually(
            lambda: ha.state("number.sim_light_duration"), lambda state: float(state["state"]) == 3
        )
        await ha.service(
            "light",
            "turn_on",
            {
                "entity_id": managed,
                "brightness": 80,
                "hs_color": [40, 60],
            },
        )
        await eventually(
            lambda: lab.physical("ambient_light"), lambda state: state["brightness"] == 80
        )
        await lab.sim("POST", "/admin/devices/light_duration", {"value": 90})
        marker = (await lab.sim())["journal_seq"]
        await eventually(
            lambda: ha.state("number.sim_light_duration"), lambda state: float(state["state"]) == 90
        )
        # Native AL sends brightness first, then awaits its configured split delay.
        await eventually(
            lambda: lab.physical("ambient_light"),
            lambda state: state["brightness"] == 153,
            timeout=10,
        )
        await ha.service(
            "light",
            "turn_on",
            {
                "entity_id": managed,
                "brightness": 73,
                "hs_color": [220, 75],
            },
        )
        new_manual = (await explained())["manual"]
        await asyncio.sleep(3)
        physical = await lab.physical("ambient_light")
        assert physical["brightness"] == 73 and physical["hs_color"] == [220, 75], physical
        assert (await explained())["manual"] == new_manual
        rows = await commands(marker)
        assert len(rows) <= 5, rows
        evidence["concurrent_manual_resume"] = {
            "commands": rows,
            "manual": new_manual,
            "physical": physical,
        }
        await ha.service("ha_operator", "release", {"resource_id": resource})
        await eventually(
            lambda: lab.physical("ambient_light"),
            lambda state: (
                state["color_mode"] == "color_temp" and state["color_temp_kelvin"] == 3500
            ),
        )
        await ha.service(
            "adaptive_lighting",
            "change_switch_settings",
            {
                "entity_id": "switch.adaptive_lighting_lab_ambient",
                "separate_turn_on_commands": False,
                "send_split_delay": 0,
            },
        )

    async with lab.scenario("LIGHT-TELEMETRY-NO-OWNERSHIP-OR-LOOP"):
        marker = (await lab.sim())["journal_seq"]
        await control(brightness=110, color_temp_kelvin=4200, color_mode="color_temp")
        await eventually(
            lambda: ha.state(managed), lambda state: state["attributes"].get("brightness") == 110
        )
        assert (await explained())["manual"] is None
        await asyncio.sleep(2.2)
        assert not await commands(marker)
        evidence["snapshots"].append(await explained())

    async with lab.scenario("LIGHT-NATIVE-MORE-INFO"):
        await lab.page.goto(ha.base + "/config/entities")
        await lab.page.get_by_placeholder(re.compile(r"Search \d+ entities")).fill(managed)
        await lab.page.get_by_role("rowheader", name="Lab ambient facade", exact=True).click()
        dialog = lab.page.locator("ha-more-info-dialog")
        await expect(dialog.get_by_role("dialog").last).to_be_visible()
        await expect(dialog.get_by_role("slider", name="Brightness", exact=True)).to_be_visible()
        await expect(dialog.get_by_role("button", name="History", exact=True)).to_be_visible()
        await lab.page.screenshot(path=str(ARTIFACTS / "light-native-more-info.png"))
        await dialog.get_by_role("button", name="History", exact=True).click()
        await expect(dialog.locator("ha-more-info-history-and-logbook")).to_be_visible()
        await lab.page.screenshot(path=str(ARTIFACTS / "light-native-history.png"))

    async with lab.scenario("LIGHT-HAP-DIM-COLOR-INDEPENDENT-FEEDBACK"):
        if lab.hap:
            await lab.hap.close()
            lab.hap = None
        entries = await ha.ws("config_entries/get")
        bridge = next(row["entry_id"] for row in entries if row["domain"] == "homekit")
        await ha.request("POST", f"/api/config/config_entries/entry/{bridge}/reload", {})
        pin = None
        pairing = STATE / "homekit-pairing.json"
        if not pairing.exists():
            notices = await eventually(
                lambda: ha.ws("persistent_notification/get"),
                lambda rows: any(row.get("title") == "HomeKit Pairing" for row in rows),
            )
            notice = next(row for row in notices if row.get("title") == "HomeKit Pairing")
            pin = re.search(r"\b\d{3}-\d{2}-\d{3}\b", notice["message"]).group()
            lab.record_secret(pin)
        lab.hap = hap = await HAPClient.connect(
            os.environ["LAB_HA_ADDRESS"],
            21063,
            pin,
            pairing,
            journal_path=ARTIFACTS / "hap-transcript.jsonl",
        )
        on = hap.characteristic("Lab ambient facade", "ON")
        brightness = hap.characteristic("Lab ambient facade", "BRIGHTNESS")
        hue = hap.characteristic("Lab ambient facade", "HUE")
        saturation = hap.characteristic("Lab ambient facade", "SATURATION")
        await control(refuse_actions=["turn_on"])
        before = await lab.physical("ambient_light")
        await hap.write_many([(on, True), (brightness, 30), (hue, 120), (saturation, 60)])
        await eventually(explained, lambda state: state["manual"] is not None)
        assert (await lab.physical("ambient_light"))["brightness"] == before["brightness"]
        await control(refuse_actions=[])
        await eventually(
            lambda: lab.physical("ambient_light"),
            lambda state: (
                abs(state["brightness"] - 76) <= 1 and abs(state["hs_color"][0] - 120) < 1
            ),
        )
        evidence["snapshots"].append(await explained())
        await ha.service("ha_operator", "release", {"resource_id": resource})
    async with lab.scenario("LIGHT-AL-DELAYED-ON-VERSUS-OFF"):
        await ha.service("switch", "turn_off", {"entity_id": await ha.managed(policy, "switch")})
        await ha.service("ha_operator", "release", {"resource_id": resource})
        await ha.service(
            "adaptive_lighting",
            "change_switch_settings",
            {
                "entity_id": "switch.adaptive_lighting_lab_ambient",
                "adapt_delay": 1.5,
                "intercept": False,
            },
        )
        races = []
        for actor in ("light.sim_ambient_light", managed):
            await ha.service("light", "turn_off", {"entity_id": actor})
            await eventually(lambda: lab.physical("ambient_light"), lambda state: not state["on"])
            await asyncio.sleep(2)
            marker = (await lab.sim())["journal_seq"]
            await ha.service("light", "turn_on", {"entity_id": actor})
            await eventually(lambda: lab.physical("ambient_light"), lambda state: state["on"])
            await ha.service("light", "turn_off", {"entity_id": actor})
            await asyncio.sleep(4)
            physical = await lab.physical("ambient_light")
            rows = await commands(marker)
            assert not physical["on"], {"actor": actor, "commands": rows, "physical": physical}
            assert len(rows) <= 5, rows
            quiet = (await lab.sim())["journal_seq"]
            await asyncio.sleep(2.2)
            assert not await commands(quiet)
            races.append({"actor": actor, "commands": rows, "physical": physical})
            await ha.service("ha_operator", "release", {"resource_id": resource})
        evidence["delayed_adjustment_races"] = races
    evidence["events"] = [row for row in await lab.journal() if row["device_id"] == "ambient_light"]
    evidence["status"] = "passed"
    (ARTIFACTS / "light-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
