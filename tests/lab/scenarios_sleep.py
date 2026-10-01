"""Desired-state migration through native config, groups, HAP and simulator effects."""

from __future__ import annotations

import asyncio
import json
import os
import re

from .hap import HAPClient
from .runner import ARTIFACTS, STATE, eventually


async def run_sleep(lab):
    ha = lab.ha
    evidence = {"effects": "independent simulator switch state; not KNX protocol or lamp behavior"}
    group_entries = {}
    async with lab.scenario("SLEEP-EXISTING-GROUPS-HAP"):
        for key in ("central", "room"):
            flow = await ha.request("POST", "/api/config/config_entries/flow", {"handler": "group"})
            flow = await ha.request(
                "POST",
                "/api/config/config_entries/flow/" + flow["flow_id"],
                {"next_step_id": "switch"},
            )
            title = f"Lab {key.title()} Sleep Public"
            created = await ha.request(
                "POST",
                "/api/config/config_entries/flow/" + flow["flow_id"],
                {
                    "name": title,
                    "entities": [f"switch.sim_{key}_{suffix}" for suffix in ("flag", "lighting")],
                    "hide_members": False,
                    "all": False,
                },
            )
            assert created["type"] == "create_entry", created
            entries = await ha.ws("config_entries/get")
            group_entries[key] = next(
                item["entry_id"]
                for item in entries
                if item["domain"] == "group" and item["title"] == title
            )
            await ha.wait_entry_loaded(group_entries[key])
        bridge = next(item["entry_id"] for item in entries if item["domain"] == "homekit")
        await ha.reload_entry(bridge)
        notices = await eventually(
            lambda: ha.ws("persistent_notification/get"),
            lambda items: any(item.get("title") == "HomeKit Pairing" for item in items),
        )
        notice = next(item for item in notices if item.get("title") == "HomeKit Pairing")
        pin = re.search(r"\b\d{3}-\d{2}-\d{3}\b", notice["message"]).group()
        lab.record_secret(pin)
        lab.hap = await HAPClient.connect(
            os.environ["LAB_HA_ADDRESS"],
            21063,
            pin,
            STATE / "homekit-pairing.json",
            journal_path=ARTIFACTS / "hap-transcript.jsonl",
        )
        original_hap_identity = lab.hap.characteristic("Lab Room Sleep Public", "ON").key
        await lab.hap.close()
        lab.hap = None
    async with lab.scenario("SLEEP-NATIVE-SETUP-OBSERVE"):
        room = await ha.add_subentry(
            lab.entry,
            "intent",
            {
                "name": "Lab Room Sleep",
                "initial_value": False,
            },
        )
        central = await ha.add_subentry(
            lab.entry,
            "intent",
            {
                "name": "Lab Central Sleep",
                "initial_value": False,
                "on_targets": [room],
            },
        )
        sources = {"central": central, "room": room}
        source_entities, resources, resource_entities = {}, {}, {}
        for key, identifier in sources.items():
            registry = await ha.ws("config/entity_registry/list")
            entity_id = next(
                item["entity_id"]
                for item in registry
                if item["unique_id"] == f"{identifier}_desired"
            )
            # Preserve predictable source-only public-group references.
            desired_id = f"switch.lab_{key}_sleep_desired"
            if entity_id != desired_id:
                await ha.ws(
                    "config/entity_registry/update", entity_id=entity_id, new_entity_id=desired_id
                )
            source_entities[key] = desired_id
            for suffix in ("flag", "lighting"):
                device = f"{key}_{suffix}"
                resource = await ha.add_resource(
                    lab.entry,
                    {
                        "name": f"Lab {key} {suffix}",
                        "kind": "switch",
                        "entity_id": f"switch.sim_{device}",
                        "manual_control": False,
                        "retry_interval": 2,
                        "command_interval": 0.1,
                    },
                )
                resources[device] = resource
                await ha.add_subentry(
                    lab.entry,
                    "policy",
                    {
                        "name": f"Follow {key} {suffix}",
                        "kind": "state",
                        "resource_id": resource,
                        "intent_id": identifier,
                    },
                )
        registry = await ha.ws("config/entity_registry/list")
        for device, resource in resources.items():
            resource_entities[device] = {
                name: next(
                    item["entity_id"]
                    for item in registry
                    if item["unique_id"] == f"{resource}_{name}"
                )
                for name in ("mode", "desired", "observed", "status")
            }
            assert not any(
                item["unique_id"]
                in {f"{resource}_{key}" for key in ("managed", "manual", "expiry", "release")}
                for item in registry
            )
        assert (await ha.state(source_entities["central"]))["state"] == "off"
        assert (await ha.state(source_entities["room"]))["state"] == "off"
        evidence["configured_defaults"] = {"central": False, "room": False}
        before = (await lab.sim(path="/health"))["journal_seq"]
        await ha.service("switch", "turn_on", {"entity_id": source_entities["central"]})
        await ha.service("switch", "turn_off", {"entity_id": source_entities["room"]})
        await asyncio.sleep(1)
        assert not [
            row for row in await lab.journal() if row["seq"] > before and row["kind"] == "command"
        ]
        assert (await ha.state(source_entities["central"]))["state"] == "on"
        assert (await ha.state(source_entities["room"]))["state"] == "off"
        evidence["initial_commands"] = {"central": True, "room": False, "zero_commands": True}
        for key, entry_id in group_entries.items():
            await ha.options(
                entry_id, {"entities": [source_entities[key]], "hide_members": False, "all": False}
            )
            public_state = await ha.state(f"switch.lab_{key}_sleep_public")
            assert public_state["attributes"]["entity_id"] == [source_entities[key]]
        evidence["existing_groups_transferred"] = True

    async def settled(central_on, room_on):
        expected = {
            device: central_on if device.startswith("central_") else room_on for device in resources
        }

        async def physical():
            state = await lab.sim()
            return {
                item["id"]: item["physical"]["on"]
                for item in state["devices"]
                if item["id"] in resources
            }

        await eventually(physical, lambda actual: actual == expected)
        for entities in resource_entities.values():
            await eventually(
                lambda e=entities["status"]: ha.state(e),
                lambda state: state["state"] == "satisfied",
            )
        evidence.setdefault("confirmed_pairs", []).append({"central": central_on, "room": room_on})

    async with lab.scenario("SLEEP-FRESH-COMMANDS-LIVE"):
        for entities in resource_entities.values():
            await ha.service(
                "select", "select_option", {"entity_id": entities["mode"], "option": "live"}
            )
        await settled(True, False)
        response = await ha.request(
            "POST",
            "/api/services/ha_operator/request",
            {
                "resource_id": resources["room_lighting"],
                "target": {"on": True},
            },
            allow_error=True,
        )
        assert response["status"] >= 400
    async with lab.scenario("SLEEP-SOURCE-ONLY-GROUP-ATTACHMENT"):

        async def public(key, on):
            await ha.service(
                "switch",
                "turn_on" if on else "turn_off",
                {
                    "entity_id": f"switch.lab_{key}_sleep_public",
                },
            )

        await public("central", False)
        await settled(False, False)
        await public("central", True)
        await settled(True, True)
        await public("room", False)
        await settled(True, False)
        before = (await lab.sim(path="/health"))["journal_seq"]
        await public("central", True)
        await asyncio.sleep(1)
        assert not [
            row for row in await lab.journal() if row["seq"] > before and row["kind"] == "command"
        ]
        await settled(True, False)
        await public("room", True)
        await public("central", False)
        await settled(False, True)

    async with lab.scenario("SLEEP-RESTART-DETACHED-PAIR"):
        await public("central", True)
        await public("room", False)
        await settled(True, False)
        simulator = (await lab.sim(path="/health"))["instance_id"]
        await lab.crash("kill")
        await lab.crash("start")
        await lab.ready()
        assert (await lab.sim(path="/health"))["instance_id"] == simulator
        await settled(True, False)
        assert (await ha.state(source_entities["room"]))["state"] == "off"

    async with lab.scenario("SLEEP-HAP-PUBLIC-IDENTITY"):
        lab.hap = await HAPClient.connect(
            os.environ["LAB_HA_ADDRESS"],
            21063,
            None,
            STATE / "homekit-pairing.json",
            journal_path=ARTIFACTS / "hap-transcript.jsonl",
        )
        control = lab.hap.characteristic("Lab Room Sleep Public", "ON")
        identity = control.key
        assert identity == original_hap_identity
        await lab.hap.write(control, True)
        await eventually(
            lambda: ha.state(source_entities["room"]), lambda state: state["state"] == "on"
        )
        await settled(True, True)
        await lab.hap.close()
        lab.hap = None
        await ha.reload_entry(lab.entry)
        lab.hap = await HAPClient.connect(
            os.environ["LAB_HA_ADDRESS"],
            21063,
            None,
            STATE / "homekit-pairing.json",
            journal_path=ARTIFACTS / "hap-transcript.jsonl",
        )
        assert lab.hap.characteristic("Lab Room Sleep Public", "ON").key == identity
        evidence["hap_identity_preserved"] = True

    async with lab.scenario("SLEEP-HAP-DIRECT-REPLACEMENT"):
        await lab.hap.close()
        lab.hap = None
        # Exercise the other migration shape: raw switches exposed directly on
        # a native UI-configured bridge. Pairing survives, accessory IDs change.
        flow = await ha.request("POST", "/api/config/config_entries/flow", {"handler": "homekit"})
        endpoint = "/api/config/config_entries/flow/" + flow["flow_id"]
        flow = await ha.request("POST", endpoint, {"include_domains": ["switch"]})
        assert flow["step_id"] == "pairing"
        created = await ha.request("POST", endpoint, {})
        assert created["type"] == "create_entry"
        entries = await ha.ws("config_entries/get")
        direct = next(
            item for item in entries if item["domain"] == "homekit" and item["entry_id"] != bridge
        )
        await ha.wait_entry_loaded(direct["entry_id"])
        port = int(direct["title"].rsplit(":", 1)[1])

        async def bridge_ready(entities=None):
            async def diagnostics():
                return (
                    await ha.request("GET", f"/api/diagnostics/config_entry/{direct['entry_id']}")
                )["data"]

            # Native HomeKit starts its HAP server after the entry becomes
            # loaded. Options already trigger reload; a second reload during
            # STATUS_WAIT can leave the first driver bound to the same port.
            return await eventually(
                diagnostics,
                lambda data: (
                    data["status"] == 1
                    and (
                        entities is None
                        or {item["entity_id"] for item in data["bridge"].values()} == set(entities)
                    )
                ),
            )

        await bridge_ready()

        async def expose(entities):
            flow = await ha.request(
                "POST", "/api/config/config_entries/options/flow", {"handler": direct["entry_id"]}
            )
            endpoint = "/api/config/config_entries/options/flow/" + flow["flow_id"]
            flow = await ha.request(
                "POST",
                endpoint,
                {"mode": "bridge", "include_exclude_mode": "include", "domains": ["switch"]},
            )
            assert flow["step_id"] == "include", flow
            flow = await ha.request("POST", endpoint, {"entities": entities})
            assert flow["step_id"] == "bridged_device_triggers", flow
            flow = await ha.request("POST", endpoint, {"devices": []})
            assert flow["type"] == "create_entry", flow
            await bridge_ready(entities)

        raw = ["switch.sim_central_flag", "switch.sim_room_flag"]
        public_room = "switch.lab_room_sleep_public"
        await expose([*raw, public_room])
        notices = await eventually(
            lambda: ha.ws("persistent_notification/get"),
            lambda items: any(item.get("title") == "HomeKit Pairing" for item in items),
        )
        notice = next(item for item in notices if item.get("title") == "HomeKit Pairing")
        pin = re.search(r"\b\d{3}-\d{2}-\d{3}\b", notice["message"]).group()
        lab.record_secret(pin)
        pairing_file = STATE / "homekit-direct-pairing.json"
        lab.hap = await HAPClient.connect(
            os.environ["LAB_HA_ADDRESS"],
            port,
            pin,
            pairing_file,
            journal_path=ARTIFACTS / "hap-transcript.jsonl",
        )
        raw_ids = [
            lab.hap.characteristic(
                (await ha.state(entity))["attributes"]["friendly_name"], "ON"
            ).key
            for entity in raw
        ]
        unrelated_id = lab.hap.characteristic("Lab Room Sleep Public", "ON").key
        await lab.hap.close()
        lab.hap = None
        await expose([*source_entities.values(), public_room])
        lab.hap = await HAPClient.connect(
            os.environ["LAB_HA_ADDRESS"],
            port,
            None,
            pairing_file,
            journal_path=ARTIFACTS / "hap-transcript.jsonl",
        )
        controls = {
            key: lab.hap.characteristic(
                (await ha.state(entity))["attributes"]["friendly_name"], "ON"
            )
            for key, entity in source_entities.items()
        }
        assert {control.aid for control in controls.values()}.isdisjoint(key[0] for key in raw_ids)
        assert lab.hap.characteristic("Lab Room Sleep Public", "ON").key == unrelated_id
        await lab.hap.write(controls["room"], False)
        await eventually(
            lambda: ha.state(source_entities["room"]), lambda state: state["state"] == "off"
        )
        await settled(True, False)
        await lab.hap.write(controls["central"], False)
        await eventually(
            lambda: ha.state(source_entities["central"]), lambda state: state["state"] == "off"
        )
        await settled(False, False)
        await lab.hap.write(controls["central"], True)
        await eventually(
            lambda: ha.state(source_entities["central"]), lambda state: state["state"] == "on"
        )
        await settled(True, True)
        evidence["direct_hap_replacement"] = {
            "old": raw_ids,
            "new": [control.key for control in controls.values()],
            "pairing_preserved": True,
            "unrelated_accessory_preserved": True,
            "client_routine_rebinding": "not exercised by aiohomekit; requires client review",
        }

    async with lab.scenario("SLEEP-NATIVE-DASHBOARD"):
        await ha.ws(
            "lovelace/dashboards/create",
            url_path="operator-sleep",
            title="Sleep controls",
            icon="mdi:sleep",
            show_in_sidebar=True,
            require_admin=False,
        )
        await ha.ws(
            "lovelace/config/save",
            url_path="operator-sleep",
            config={
                "title": "Operator lab",
                "views": [
                    {
                        "title": "Sleep controls",
                        "path": "sleep",
                        "cards": [
                            {
                                "type": "entities",
                                "title": "Desired controls",
                                "entities": list(source_entities.values()),
                            },
                            {
                                "type": "entities",
                                "title": "Followers",
                                "entities": [
                                    entity
                                    for entities in resource_entities.values()
                                    for key, entity in entities.items()
                                    if key != "mode"
                                ],
                            },
                            {
                                "type": "history-graph",
                                "title": "Desired and observed",
                                "hours_to_show": 1,
                                "entities": [
                                    source_entities["room"],
                                    resource_entities["room_lighting"]["desired"],
                                    resource_entities["room_lighting"]["observed"],
                                ],
                            },
                        ],
                    }
                ],
            },
        )
        await lab.page.goto(ha.base + "/operator-sleep/sleep", wait_until="domcontentloaded")
        await lab.page.get_by_text("Desired controls", exact=True).wait_for()
        await lab.page.get_by_text("Followers", exact=True).wait_for()
        await lab.page.screenshot(path=ARTIFACTS / "sleep-controls.png", full_page=True)
        evidence["native_browser"] = "sleep-controls.png"
    (ARTIFACTS / "sleep-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
