"""Recorder, Activity and frontend proof against the installed release ZIP."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from .runner import ARTIFACTS, eventually


async def run_observability(lab):
    ha = lab.ha
    started = datetime.now(UTC).isoformat()
    async with lab.scenario("OBS-NATIVE-ENTITIES"):
        baseline = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Background ventilation",
                "resource_id": lab.resource,
                "kind": "state",
                "target": {"position": 40},
                "priority": 0,
            },
        )
        evening = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Evening ventilation",
                "resource_id": lab.resource,
                "kind": "state",
                "target": {"position": 40},
                "priority": 10,
                "eligibility_entity": "input_boolean.demo_evening",
            },
        )
        registry = await ha.ws("config/entity_registry/list")
        entities = {
            key: next(
                item["entity_id"]
                for item in registry
                if item["unique_id"] == f"{lab.resource}_{key}"
            )
            for key in ("desired", "observed", "reason", "status", "expiry")
        }
        device_ids = {
            item["device_id"] for item in registry if item.get("config_subentry_id") == lab.resource
        }
        assert len(device_ids) == 1
        await ha.service("select", "select_option", {"entity_id": lab.mode, "option": "live"})
        desired = await eventually(
            lambda: ha.state(entities["desired"]), lambda state: state["state"] == "40.0"
        )
        assert desired["attributes"]["source_id"] == baseline
        assert desired["attributes"]["observed_entity"] == entities["observed"]
        assert desired["attributes"]["unit_of_measurement"] == "%"
        assert "state_class" not in desired["attributes"]
        observed = await ha.state(entities["observed"])
        assert observed["attributes"]["state_class"] == "measurement"
        assert float(observed["state"]) == 0
        await eventually(
            lambda: ha.state(entities["status"]), lambda state: state["state"] == "waiting"
        )
        assert (await lab.physical())["position"] == 0
        http_explain = await ha.service(
            "ha_operator", "explain", {"resource_id": lab.resource}, response=True
        )
        websocket_explain = await ha.ws(
            "call_service",
            domain="ha_operator",
            service="explain",
            service_data={"resource_id": lab.resource},
            return_response=True,
        )
        responses = {
            "http": http_explain["service_response"],
            "websocket": websocket_explain["response"],
        }
        (ARTIFACTS / "observability-explain-responses.json").write_text(
            json.dumps(responses, indent=2)
        )
        for response in responses.values():
            candidates = response["resources"][lab.resource]["decision"]["candidates"]
            assert isinstance(candidates, list) and candidates
            assert any(
                candidate["source"] == f"policy:{baseline}"
                and candidate["target"]["position"] == 40
                for candidate in candidates
            )

    async with lab.scenario("OBS-REASON-CHANGE-NO-DISPATCH"):
        await lab.sim("POST", "/admin/devices/skylight", {"hidden_rain": False})
        await lab.wait_position(40)
        await eventually(
            lambda: ha.state(entities["status"]), lambda state: state["state"] == "satisfied"
        )
        commands_before = [item for item in await lab.journal() if item["kind"] == "command"]
        await ha.service("input_boolean", "turn_on", {"entity_id": "input_boolean.demo_evening"})
        await eventually(
            lambda: ha.state(entities["reason"]),
            lambda state: state["state"] == "Evening ventilation",
        )
        await asyncio.sleep(2)
        assert [
            item for item in await lab.journal() if item["kind"] == "command"
        ] == commands_before
        desired = await ha.state(entities["desired"])
        assert desired["attributes"]["source_id"] == evening and float(desired["state"]) == 40
        stamp = desired["last_updated"]
        await ha.service("ha_operator", "reconcile", {"resource_id": lab.resource})
        assert (await ha.state(entities["desired"]))["last_updated"] == stamp

    async with lab.scenario("OBS-EXPIRY-AND-RECORDER"):
        await lab.sim(
            "POST", "/admin/devices/skylight", {"hidden_rain": True, "rain_autoclose": False}
        )
        await lab.request(90, duration=3, request_id="observability-expiry")
        await eventually(
            lambda: ha.state(entities["reason"]), lambda state: state["state"] == "Manual target"
        )
        await eventually(
            lambda: ha.state(entities["reason"]),
            lambda state: state["state"] == "Evening ventilation",
        )
        assert abs((await lab.physical())["position"] - 40) < 2

        async def history():
            return await ha.request(
                "GET",
                f"/api/history/period/{started}?filter_entity_id="
                + ",".join(entities[key] for key in ("desired", "observed", "reason")),
            )

        def recorded(rows):
            desired = next(
                (row for row in rows if row and row[0]["entity_id"] == entities["desired"]), []
            )
            return {item["attributes"].get("source_id") for item in desired} >= {
                baseline,
                evening,
            } and any(
                item["attributes"].get("request_id") == "observability-expiry" for item in desired
            )

        records = await eventually(history, recorded, timeout=30)
        (ARTIFACTS / "observability-history.json").write_text(json.dumps(records, indent=2))

        async def activity():
            return await ha.request("GET", f"/api/logbook/{started}?entity={lab.cover}")

        logbook = await eventually(
            activity,
            lambda rows: any(
                "Evening ventilation" in item.get("message", "")
                and item.get("entity_id") == lab.cover
                for item in rows
            ),
            timeout=30,
        )
        (ARTIFACTS / "observability-activity.json").write_text(json.dumps(logbook, indent=2))

    async with lab.scenario("OBS-FAN-PROFILE-REASON"):
        outputs = ["switch.sim_cellar_inward_relay", "switch.sim_cellar_outward_relay"]
        fan = await ha.add_resource(
            lab.entry,
            {
                "name": "Lab Ventilation Fan",
                "kind": "relay_fan",
                "outputs": outputs,
                "profiles": {
                    "off": {"outputs": dict.fromkeys(outputs, False), "percentage": 0},
                    "inward": {
                        "outputs": {outputs[0]: True, outputs[1]: False},
                        "percentage": 100,
                        "direction": "forward",
                    },
                    "outward": {
                        "outputs": {outputs[0]: False, outputs[1]: True},
                        "percentage": 100,
                        "direction": "reverse",
                    },
                },
                "default_target": {"profile": "inward"},
                "reversal_dead_time": 1,
                "retry_interval": 2,
                "command_interval": 0.2,
                "movement_timeout": 4,
            },
        )
        await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Fresh air schedule",
                "resource_id": fan,
                "kind": "state",
                "target": {"profile": "inward"},
            },
        )
        mode = await ha.managed(fan, "select")
        await ha.service("select", "select_option", {"entity_id": mode, "option": "live"})
        registry = await ha.ws("config/entity_registry/list")
        fan_entities = {
            key: next(item["entity_id"] for item in registry if item["unique_id"] == f"{fan}_{key}")
            for key in ("desired", "observed", "reason", "status")
        }
        await eventually(
            lambda: ha.state(fan_entities["observed"]), lambda state: state["state"] == "inward"
        )
        assert (await ha.state(fan_entities["reason"]))["state"] == "Fresh air schedule"
        assert (await lab.physical("cellar_inward_relay"))["on"] is True
        assert (await lab.physical("cellar_outward_relay"))["on"] is False
        # Independent simulator refusal leaves observed off while desired remains inward.
        await lab.sim(
            "POST",
            "/admin/devices/cellar_inward_relay",
            {"refuse_actions": ["turn_on"], "on": False},
        )
        await eventually(
            lambda: ha.state(fan_entities["observed"]), lambda state: state["state"] == "off"
        )
        assert (await ha.state(fan_entities["desired"]))["state"] == "inward"
        assert (await ha.state(fan_entities["reason"]))["state"] == "Fresh air schedule"

    async with lab.scenario("OBS-NATIVE-DASHBOARD"):
        await ha.ws(
            "lovelace/dashboards/create",
            url_path="operator-demo",
            title="Operator Lab",
            icon="mdi:robot",
            show_in_sidebar=True,
            require_admin=False,
        )

        def row(entity_id, name):
            return {"entity": entity_id, "name": name}

        dashboard = {
            "title": "Operator Lab",
            "views": [
                {
                    "title": "Status & history",
                    "path": "status",
                    "cards": [
                        {
                            "type": "markdown",
                            "content": (
                                "# HA Operator · simulated home\n"
                                "Native entities from the installed release ZIP.\n\n"
                                "Desired target is the effective target. "
                                "Observed is validated device feedback. "
                                "Reason records why the target was selected."
                            ),
                        },
                        {
                            "type": "entities",
                            "title": "Skylight · intent and feedback",
                            "show_header_toggle": False,
                            "entities": [
                                row(entities[key], name)
                                for key, name in (
                                    ("desired", "Desired position"),
                                    ("observed", "Observed position"),
                                    ("reason", "Reason"),
                                    ("status", "Execution status"),
                                    ("expiry", "Manual expiry"),
                                )
                            ],
                        },
                        {
                            "type": "history-graph",
                            "title": "Skylight · desired vs observed",
                            "hours_to_show": 1,
                            "entities": [
                                row(entities["desired"], "Desired"),
                                row(entities["observed"], "Observed"),
                            ],
                        },
                        {
                            "type": "history-graph",
                            "title": "Why the target changed",
                            "hours_to_show": 1,
                            "entities": [row(entities["reason"], "Reason")],
                        },
                        {
                            "type": "entities",
                            "title": "Ventilation fan · relay feedback",
                            "show_header_toggle": False,
                            "entities": [
                                row(fan_entities[key], name)
                                for key, name in (
                                    ("desired", "Desired profile"),
                                    ("observed", "Observed profile"),
                                    ("reason", "Reason"),
                                    ("status", "Execution status"),
                                )
                            ],
                        },
                        {
                            "type": "logbook",
                            "title": "Operator activity",
                            "entities": [lab.cover],
                            "hours_to_show": 1,
                        },
                    ],
                }
            ],
        }
        dashboard["views"][0]["cards"].append(
            {
                "type": "entities",
                "title": "Native Operator presentation",
                "entities": [entities["status"], entities["desired"], lab.mode],
            }
        )
        await ha.ws("lovelace/config/save", url_path="operator-demo", config=dashboard)
        await lab.request(80, duration=600, request_id="browser-demo-refusal")
        await lab.page.goto(ha.base + "/operator-demo/status", wait_until="domcontentloaded")
        await lab.page.get_by_text("Skylight · intent and feedback", exact=True).wait_for()
        await lab.page.screenshot(
            path=str(ARTIFACTS / "observability-dashboard.png"), full_page=True
        )
        (ARTIFACTS / "observability-dashboard.json").write_text(json.dumps(dashboard, indent=2))

    async with lab.scenario("OBS-NATIVE-TRANSLATED-PRESENTATION"):
        await lab.page.get_by_text("Native Operator presentation", exact=True).wait_for()
        status_state = await ha.state(entities["status"])
        status_label = lab.page.get_by_text(status_state["attributes"]["friendly_name"], exact=True)
        await status_label.wait_for()
        row_dom = await status_label.evaluate("""(label) => {
            const host = label.getRootNode().host;
            return { label: label.textContent.trim(), host: host.localName,
                     rendered: host.textContent.trim(), markup: host.outerHTML };
        }""")
        row_dom["counts"] = {
            "cards": await lab.page.locator("hui-entities-card").count(),
            "title": await lab.page.get_by_text("Native Operator presentation", exact=True).count(),
            "card_filter": await lab.page.locator("hui-entities-card")
            .filter(has_text="Native Operator presentation")
            .count(),
            "native_status_label": await status_label.count(),
        }
        (ARTIFACTS / "gold-native-dom.json").write_text(json.dumps(row_dom, indent=2))
        assert row_dom["host"] == "hui-generic-entity-row"
        assert row_dom["rendered"] == "Waiting"
        mode_state = await ha.state(lab.mode)
        await lab.page.get_by_text(mode_state["attributes"]["friendly_name"], exact=True).click()
        await lab.page.get_by_role("menuitem", name="Observe", exact=True).wait_for()
        await lab.page.get_by_role("menuitem", name="Live", exact=True).wait_for()
        await lab.page.screenshot(path=str(ARTIFACTS / "gold-native-mode.png"), full_page=True)
