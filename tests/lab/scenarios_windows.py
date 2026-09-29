"""Exercise native window migration configuration in the isolated packaged lab.

Assertions are test acceptance checks, never production safety guards.
"""

from __future__ import annotations

import asyncio
import json
import time

from .window_recipe import (
    cold_automation,
    dispatcher,
    occurrence_actions,
    overdue_automation,
    preset,
    raw_open_helper,
    timer_automation,
)


async def run_windows(lab):
    from .runner import ARTIFACTS, eventually

    ha = lab.ha
    devices = ["skylight", "inlet", "stopped_unsupported"]
    resources = [lab.resource]
    entities = [lab.cover]
    modes = [lab.mode]
    baseline = 7

    async def explain(index=0):
        result = await ha.service(
            "ha_operator", "explain", {"resource_id": resources[index]}, response=True
        )
        return result["service_response"]["resources"][resources[index]]

    async def position(target, index=0):
        return await lab.wait_position(target, device=devices[index])

    async def mode(index, value):
        await ha.service("select", "select_option", {"entity_id": modes[index], "option": value})

    async def script_config(key, data):
        await ha.request("POST", "/api/config/script/config/" + key, data)
        await eventually(
            lambda: ha.request("GET", "/api/states/script." + key, allow_error=True),
            lambda state: "entity_id" in state,
        )

    async def automation_config(key, entity, data):
        previous = await ha.request("GET", "/api/states/" + entity, allow_error=True)
        await ha.request("POST", "/api/config/automation/config/" + key, data)
        # The native config endpoint schedules reload without waiting for it.
        await eventually(
            lambda: ha.request("GET", "/api/states/" + entity, allow_error=True),
            lambda state: (
                state.get("state") == "on"
                and state.get("last_changed") != previous.get("last_changed")
            ),
        )

    async def helper(domain, kind, config):
        flow = await ha.request("POST", "/api/config/config_entries/flow", {"handler": domain})
        path = "/api/config/config_entries/flow/" + flow["flow_id"]
        await ha.request("POST", path, {"next_step_id": kind})
        result = await ha.request("POST", path, config)
        assert result["type"] == "create_entry", result  # nosec B101
        return result["result"]["entry_id"]

    async def call(position=100, seconds=8, targets=None, **extra):
        return await ha.service(
            "script",
            "lab_window_request",
            {
                "windows": {"entity_id": targets or [entities[0]]},
                "position": position,
                "duration": seconds,
                **extra,
            },
            response=True,
        )

    async def control(index=0, **changes):
        return await lab.sim("POST", "/admin/devices/" + devices[index], changes)

    async def commands_since(marker):
        return [
            item
            for item in await lab.journal()
            if item["kind"] == "command" and item["seq"] > marker and item["device_id"] in devices
        ]

    async with lab.scenario("WINDOW-CONFIGURATION"):
        for index, device in enumerate(devices):
            await control(
                index, hidden_rain=False, rain_autoclose=False, speed=100, position=baseline
            )
            if index:
                resource = await ha.add_subentry(
                    lab.entry,
                    "resource",
                    {
                        "name": "Lab Window " + str(index + 1),
                        "kind": "cover",
                        "entity_id": "cover.sim_" + device,
                        "retry_interval": 1,
                        "command_interval": 0.2,
                        "movement_timeout": 3,
                        "manual_duration": 8,
                    },
                )
                resources.append(resource)
                entities.append(await ha.managed(resource, "cover"))
                modes.append(await ha.managed(resource, "select"))
            await ha.add_subentry(
                lab.entry,
                "policy",
                {
                    "name": "Window baseline " + str(index),
                    "kind": "state",
                    "resource_id": resources[index],
                    "priority": -1000,
                    "target": {"position": baseline},
                },
            )
        bindings = [
            {"entity_id": entity, "resource_id": resource}
            for entity, resource in zip(entities, resources, strict=True)
        ]
        recipe = dispatcher(bindings, "cover.lab_window_group", duration=8)
        await script_config("lab_window_request", recipe)
        await script_config(
            "lab_micro_vent",
            preset("Lab Micro Vent", "script.lab_window_request", entities, baseline, 8),
        )
        await helper(
            "group",
            "cover",
            {
                "name": "Lab Window Group",
                "entities": entities,
                "hide_members": False,
            },
        )
        await helper(
            "template",
            "binary_sensor",
            raw_open_helper(
                ["cover.sim_" + device for device in devices],
                baseline,
            ),
        )
        (ARTIFACTS / "window-recipe.json").write_text(json.dumps(recipe, indent=2))

    async with lab.scenario("WINDOW-OBSERVE-PREFLIGHT-ZERO"):
        before = (await lab.sim())["journal_seq"]
        rejected = await ha.request(
            "POST",
            "/api/services/script/lab_window_request?return_response",
            {"windows": {"entity_id": entities}, "duration": 8},
            allow_error=True,
        )
        assert not rejected.get("service_response", {}).get("accepted"), rejected  # nosec B101
        assert not await commands_since(before)  # nosec B101
        for i in range(3):
            assert (await explain(i))["manual"] is None  # nosec B101
        await ha.ws("input_boolean/create", name="Lab Continuation")
        wrapper = preset("Lab guarded continuation", "script.lab_window_request", entities, 100, 8)
        wrapper["sequence"][-1:] = [
            {
                "action": "input_boolean.turn_on",
                "target": {"entity_id": "input_boolean.lab_continuation"},
            }
        ]
        await script_config("lab_guarded_continuation", wrapper)
        await ha.request(
            "POST",
            "/api/services/script/lab_guarded_continuation?return_response",
            {},
            allow_error=True,
        )
        assert (await ha.state("input_boolean.lab_continuation"))["state"] == "off"  # nosec B101
        assert not await commands_since(before)  # nosec B101
        for i in range(3):
            await mode(i, "live")

    async with lab.scenario("WINDOW-NATIVE-SCENE-NOOP-AND-EXPLICIT-INTENT"):
        await control(hidden_rain=True)
        await call(100, 20)
        original = (await explain())["manual"]
        assert original["target"]["position"] == 100  # nosec B101
        await ha.request(
            "POST",
            "/api/config/scene/config/lab_window_micro",
            {
                "id": "lab_window_micro",
                "name": "Lab Window Micro",
                "entities": {entities[0]: {"state": "open", "current_position": baseline}},
            },
        )
        await eventually(
            lambda: ha.request("GET", "/api/states/scene.lab_window_micro", allow_error=True),
            lambda state: "entity_id" in state,
        )
        await ha.service("scene", "turn_on", {"entity_id": "scene.lab_window_micro"})
        assert (await explain())["manual"]["request_id"] == original["request_id"]  # nosec B101
        await ha.service("script", "lab_micro_vent", {}, response=True)
        assert (await explain())["manual"]["target"]["position"] == baseline  # nosec B101
        await control(hidden_rain=False)
        await position(baseline)

    async with lab.scenario("WINDOW-RAIN-CLEAR-BEFORE-EXPIRY"):
        await control(hidden_rain=True)
        await call(100, 12)
        accepted = (await explain())["manual"]
        await asyncio.sleep(1.3)
        await position(baseline)
        await control(hidden_rain=False)
        await position(100)
        assert (await explain())["manual"]["expires_at"] == accepted["expires_at"]  # nosec B101
        await eventually(
            lambda: lab.physical(devices[0]),
            lambda state: abs(state["position"] - baseline) <= 2,
            timeout=20,
        )

    async with lab.scenario("WINDOW-RAIN-CLEAR-AFTER-EXPIRY"):
        await control(hidden_rain=True)
        await call(100, 3)
        await eventually(explain, lambda state: state["manual"] is None, timeout=10)
        before = (await lab.sim())["journal_seq"]
        await control(hidden_rain=False)
        await asyncio.sleep(2.3)
        assert (
            not [  # nosec B101
                item
                for item in await commands_since(before)
                if item["data"].get("position", 0) > baseline
            ]
        )
        await position(baseline)

    async with lab.scenario("WINDOW-INDEPENDENT-EXPIRY-IDEMPOTENCY-SUPERSESSION"):
        deadline = time.time() + 6
        response = await call(60, 6, expires_at=deadline, request_id="fixed-window-request")
        assert response["service_response"]["accepted"]  # nosec B101
        await call(60, 6, expires_at=deadline, request_id="fixed-window-request")
        assert (await explain())["manual"]["expires_at"] == deadline  # nosec B101
        await call(35, 12, [entities[1]])
        await call(baseline, 3)
        await position(baseline)
        await position(35, 1)
        assert (await explain(1))["manual"]["target"]["position"] == 35  # nosec B101
        await asyncio.sleep(6.5)
        await position(baseline)
        assert (await explain(1))["manual"]["target"]["position"] == 35  # nosec B101

    async with lab.scenario("WINDOW-MULTI-PREFLIGHT-AND-GROUP-AVERAGE"):
        await mode(2, "observe")
        original = [(await explain(i))["manual"] for i in range(3)]
        failure = await ha.request(
            "POST",
            "/api/services/script/lab_window_request?return_response",
            {"windows": {"entity_id": "cover.lab_window_group"}, "position": 45, "duration": 10},
            allow_error=True,
        )
        assert not failure.get("service_response", {}).get("accepted"), failure  # nosec B101
        assert [(await explain(i))["manual"] for i in range(3)] == original  # nosec B101
        await mode(2, "live")
        for i, value in enumerate((0, 7, 14)):
            await ha.service("ha_operator", "release", {"resource_id": resources[i]})
            await control(i, hidden_rain=True, position=value, refuse_actions=["set_position"])
        await eventually(
            lambda: ha.state("cover.lab_window_group"),
            lambda state: state["attributes"].get("current_position") == baseline,
        )
        await eventually(
            lambda: ha.state("binary_sensor.window_opened"), lambda state: state["state"] == "on"
        )
        await call(baseline, 8, ["cover.lab_window_group"])
        for i in range(3):
            assert (await explain(i))["manual"]["target"]["position"] == baseline  # nosec B101
            await control(i, hidden_rain=False, refuse_actions=[])
            await position(baseline, i)

    async with lab.scenario("WINDOW-RAW-HELPER-UNKNOWN"):
        await eventually(
            lambda: ha.state("binary_sensor.window_opened"), lambda state: state["state"] == "off"
        )
        await control(1, available=False)
        await eventually(
            lambda: ha.state("binary_sensor.window_opened"),
            lambda state: state["state"] == "unavailable",
        )
        await control(2, position=70, hidden_rain=True, refuse_actions=["set_position"])
        await eventually(
            lambda: ha.state("binary_sensor.window_opened"), lambda state: state["state"] == "on"
        )
        await control(1, available=True)
        await control(2, position=baseline, hidden_rain=False, refuse_actions=[])

    async with lab.scenario("WINDOW-NO-PHYSICAL-STOP"):
        assert not (await ha.state(entities[2]))["attributes"]["supported_features"] & 8  # nosec B101
        await ha.service(
            "ha_operator",
            "request",
            {"resource_id": resources[2], "mode": "hands_off", "duration": 3},
        )
        before = (await lab.sim())["journal_seq"]
        await asyncio.sleep(1.5)
        assert (
            not [  # nosec B101
                item for item in await commands_since(before) if item["device_id"] == devices[2]
            ]
        )
        await ha.service("ha_operator", "release", {"resource_id": resources[2]})

    async with lab.scenario("WINDOW-TIMER-OCCURRENCE-CANCEL-RESTART"):
        await ha.ws("timer/create", name="Lab Window Timer", duration="00:01:00", restore=True)
        await ha.ws("input_text/create", name="Lab Window Epoch", max=255)
        await ha.ws("input_boolean/create", name="Lab Timer Ready", initial=False)
        timer, record = "timer.lab_window_timer", "input_text.lab_window_epoch"
        policy = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Window timed occurrence",
                "kind": "occurrence",
                "resource_id": resources[0],
                "priority": 20,
                "target": {"position": 100},
            },
        )
        config = timer_automation(
            timer,
            record,
            policy,
            entities[0],
            "input_boolean.lab_timer_ready",
            qualify=2,
            duration=12,
        )
        await ha.request(
            "POST",
            "/api/config/automation/config/lab_window_timer",
            {"id": "lab_window_timer", **config},
        )
        await ha.service("ha_operator", "release", {"resource_id": resources[0]})
        await ha.service(
            "input_text",
            "set_value",
            {
                "entity_id": record,
                "value": json.dumps(
                    {"v": 1, "phase": "idle", "initialized_at": time.time()},
                    separators=(",", ":"),
                ),
            },
        )
        await ha.service("input_boolean", "turn_on", {"entity_id": "input_boolean.lab_timer_ready"})
        await ha.service("timer", "start", {"entity_id": timer})
        await ha.service("timer", "cancel", {"entity_id": timer})
        await asyncio.sleep(5.3)
        await position(baseline)
        await ha.service("timer", "start", {"entity_id": timer})
        armed = json.loads((await ha.state(record))["state"])
        await ha.service("timer", "start", {"entity_id": timer, "duration": "00:01:30"})
        await eventually(
            lambda: ha.state(record),
            lambda state: json.loads(state["state"]).get("id") != armed["id"],
            timeout=5,
        )
        await position(100)
        admitted = json.loads((await ha.state(record))["state"])
        await ha.service("timer", "start", {"entity_id": timer, "duration": "00:02:00"})
        await eventually(
            lambda: ha.state(record),
            lambda state: json.loads(state["state"]).get("id") != admitted["id"],
            timeout=5,
        )
        await position(baseline)
        await position(100)
        await ha.service("timer", "pause", {"entity_id": timer})
        await position(baseline)
        await ha.service("timer", "start", {"entity_id": timer})
        await asyncio.sleep(5.3)
        await position(baseline)
        await ha.service("timer", "start", {"entity_id": timer, "duration": "00:01:00"})
        await position(100)
        await ha.service("timer", "cancel", {"entity_id": timer})
        await position(baseline)

    async with lab.scenario("WINDOW-COLD-POLICY-MANUAL-PRECEDENCE"):
        await ha.ws("input_text/create", name="Lab Cold Epoch", max=255)
        await ha.ws("input_boolean/create", name="Lab Cold Qualified", initial=False)
        await ha.ws("input_boolean/create", name="Lab Cold Ready", initial=False)
        cold = "input_boolean.lab_cold_qualified"
        await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Window cold policy",
                "kind": "state",
                "resource_id": resources[0],
                "priority": 100,
                "target": {"position": baseline},
                "eligibility_entity": cold,
            },
        )
        await ha.request(
            "POST",
            "/api/config/automation/config/lab_window_cold",
            {
                "id": "lab_window_cold",
                **cold_automation(
                    "sensor.sim_cellar_target",
                    "input_text.lab_cold_epoch",
                    cold,
                    "input_boolean.lab_cold_ready",
                    threshold=16,
                    qualify=2,
                ),
            },
        )
        await ha.service("input_boolean", "turn_on", {"entity_id": "input_boolean.lab_cold_ready"})
        await lab.sim("POST", "/admin/devices/cellar_target", {"value": 10})
        await eventually(lambda: ha.state(cold), lambda state: state["state"] == "on", timeout=15)
        await call(75, 6)
        await position(75)
        await eventually(
            lambda: lab.physical(devices[0]),
            lambda state: abs(state["position"] - baseline) <= 2,
            timeout=15,
        )
        await lab.sim("POST", "/admin/devices/cellar_target", {"value": 20})
        await eventually(lambda: ha.state(cold), lambda state: state["state"] == "off")

    for action in ("restart", "kill"):
        async with lab.scenario("WINDOW-EXPIRED-OPENING-" + action.upper()):
            if action == "restart":
                await ha.service(
                    "input_boolean", "turn_off", {"entity_id": "input_boolean.lab_timer_ready"}
                )
                await ha.service("timer", "start", {"entity_id": timer})
                await ha.service(
                    "input_text",
                    "set_value",
                    {
                        "entity_id": record,
                        "value": json.dumps(
                            {
                                "v": 1,
                                "id": "unadmitted-before-startup",
                                "due": time.time() - 5,
                                "expires": time.time() + 90,
                                "finish": (await ha.state(timer))["attributes"]["finishes_at"],
                                "phase": "armed",
                            },
                            separators=(",", ":"),
                        ),
                    },
                )
                await asyncio.sleep(5.3)
                assert (await explain())["decision"]["target"]["position"] == baseline  # nosec B101
            await control(hidden_rain=True)
            await call(100, 3)
            simulator_id = (await lab.sim(path="/health"))["instance_id"]
            await lab.crash(action)
            if action == "kill":
                await lab.crash("start")
            await lab.ready()
            assert (await ha.state("input_boolean.lab_timer_ready"))["state"] == "on"  # nosec B101
            assert json.loads((await ha.state(record))["state"])["phase"] == "idle"  # nosec B101
            assert (await lab.sim(path="/health"))["instance_id"] == simulator_id  # nosec B101
            before = (await lab.sim())["journal_seq"]
            await control(hidden_rain=False)
            await position(baseline)
            await asyncio.sleep(2.2)
            assert (
                not [  # nosec B101
                    item
                    for item in await commands_since(before)
                    if item["data"].get("position", 0) > baseline
                ]
            )
            if action == "restart":
                await ha.service("timer", "start", {"entity_id": timer})
                await position(100)
                await ha.service("timer", "cancel", {"entity_id": timer})
                await position(baseline)

    async with lab.scenario("WINDOW-PAUSED-TIMER-RESTORATION"):
        await ha.service("timer", "start", {"entity_id": timer})
        await position(100)
        await ha.service("timer", "pause", {"entity_id": timer})
        await position(baseline)
        await lab.crash("restart")
        await lab.ready()
        assert json.loads((await ha.state(record))["state"])["phase"] == "paused"  # nosec B101
        await ha.service("timer", "start", {"entity_id": timer})
        await asyncio.sleep(5.3)
        await position(baseline)
        await ha.service("timer", "start", {"entity_id": timer})
        await position(100)
        await ha.service("timer", "cancel", {"entity_id": timer})
        await position(baseline)

    async with lab.scenario("WINDOW-TIMER-PAUSE-WITHDRAWAL-FAILURE"):
        import copy

        broken = copy.deepcopy(config)

        def break_withdrawal(value):
            if isinstance(value, dict):
                if value.get("action") == "ha_operator.skip_occurrence":
                    value["data"]["policy_id"] = "missing-policy"
                for child in value.values():
                    break_withdrawal(child)
            elif isinstance(value, list):
                for child in value:
                    break_withdrawal(child)

        await ha.service("timer", "start", {"entity_id": timer})
        await position(100)
        original = json.loads((await ha.state(record))["state"])
        break_withdrawal(broken)
        await automation_config(
            "lab_window_timer",
            "automation.timed_window_occurrence",
            {"id": "lab_window_timer", **broken},
        )
        await ha.service("timer", "pause", {"entity_id": timer})
        paused = await eventually(
            lambda: ha.state(record),
            lambda state: json.loads(state["state"]).get("phase") == "paused",
        )
        assert json.loads(paused["state"])["id"] == original["id"]  # nosec B101
        await ha.service("timer", "start", {"entity_id": timer})
        await asyncio.sleep(0.3)
        assert json.loads((await ha.state(record))["state"])["id"] == original["id"]  # nosec B101
        await automation_config(
            "lab_window_timer",
            "automation.timed_window_occurrence",
            {"id": "lab_window_timer", **config},
        )
        await ha.service("timer", "start", {"entity_id": timer})
        await position(baseline)
        await eventually(
            lambda: ha.state(record),
            lambda state: json.loads(state["state"])["phase"] == "idle",
        )
        await ha.service("timer", "start", {"entity_id": timer})
        await position(100)
        await ha.service("timer", "cancel", {"entity_id": timer})
        await position(baseline)

    await lab.homekit()
    async with lab.scenario("WINDOW-SAME-VALUE-HAP-REPLACES-INTENT"):
        await control(hidden_rain=True, position=baseline)
        await call(100, 20)
        await eventually(
            lambda: ha.state(entities[0]),
            lambda state: state["attributes"].get("current_position") == baseline,
        )
        target = lab.hap.characteristic("Lab Skylight", "0000007C-0000-1000-8000-0026BB765291")
        await lab.hap.write(target, baseline)
        accepted = await eventually(
            explain,
            lambda state: state["manual"] and state["manual"]["target"]["position"] == baseline,
        )
        (ARTIFACTS / "window-hap-durable-receipt.json").write_text(
            json.dumps(accepted["manual"], indent=2)
        )
        await control(hidden_rain=False)
        await position(baseline)

    async with lab.scenario("WINDOW-BOUNDED-AUTOMATIC-OCCURRENCE"):
        policy = await ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Window automatic occurrence",
                "kind": "occurrence",
                "resource_id": resources[1],
                "priority": 10,
                "target": {"position": 20},
            },
        )
        await lab.sim("POST", "/admin/devices/cellar_target", {"value": 10})
        await eventually(
            lambda: ha.state("sensor.sim_cellar_target"),
            lambda state: float(state["state"]) == 10,
        )
        await ha.service("ha_operator", "release", {"resource_id": resources[1]})
        await automation_config(
            "lab_window_automatic",
            "automation.lab_automatic_window",
            {
                "id": "lab_window_automatic",
                "alias": "Lab automatic window",
                "mode": "single",
                "triggers": [
                    {
                        "trigger": "numeric_state",
                        "entity_id": "sensor.sim_cellar_target",
                        "above": 15,
                    }
                ],
                "actions": occurrence_actions(
                    [{"entity_id": entities[1], "policy_id": policy}], duration=8
                ),
            },
        )
        await call(50, 3, [entities[1]])
        await lab.sim("POST", "/admin/devices/cellar_target", {"value": 20})
        await position(50, 1)
        await position(20, 1)
        await position(baseline, 1)

    async with lab.scenario("WINDOW-OVERDUE-RETURN-AND-RECOVERY"):
        await ha.ws("input_text/create", name="Lab Return Clock", max=255)
        registry = await ha.ws("config/entity_registry/list")
        desired = next(
            row["entity_id"]
            for row in registry
            if row.get("unique_id") == resources[1] + "_desired"
        )
        status = next(
            row["entity_id"] for row in registry if row.get("unique_id") == resources[1] + "_status"
        )
        await ha.request(
            "POST",
            "/api/config/automation/config/lab_window_return",
            {
                "id": "lab_window_return",
                **overdue_automation(
                    resources[1],
                    entities[1],
                    desired,
                    status,
                    "input_text.lab_return_clock",
                    timeout=2,
                ),
            },
        )
        await control(1, position=70, refuse_actions=["set_position"])
        notification_id = "ha_operator_return_" + resources[1].lower()
        await eventually(
            lambda: ha.ws("persistent_notification/get"),
            lambda items: any(item["notification_id"] == notification_id for item in items),
            timeout=15,
        )
        await ha.service(
            "ha_operator",
            "request",
            {"resource_id": resources[1], "mode": "hands_off", "duration": 15},
        )
        await eventually(
            lambda: ha.ws("persistent_notification/get"),
            lambda items: all(item["notification_id"] != notification_id for item in items),
        )
        await ha.service("ha_operator", "release", {"resource_id": resources[1]})
        await eventually(
            lambda: ha.ws("persistent_notification/get"),
            lambda items: any(item["notification_id"] == notification_id for item in items),
            timeout=15,
        )
        await control(1, refuse_actions=[])
        await position(baseline, 1)
        await eventually(
            lambda: ha.ws("persistent_notification/get"),
            lambda items: all(item["notification_id"] != notification_id for item in items),
        )

    async with lab.scenario("WINDOW-REGISTRY-OWNER-TRANSFER"):
        await helper(
            "template",
            "cover",
            {
                "name": "Lab Logical Window",
                "state": "open",
                "position": "7",
                "open_cover": [],
                "close_cover": [],
                "set_cover_position": [],
            },
        )
        logical = "cover.lab_logical_window"
        await eventually(
            lambda: ha.request("GET", "/api/states/" + logical, allow_error=True),
            lambda state: "entity_id" in state,
        )
        group_entry = await helper(
            "group",
            "cover",
            {
                "name": "Lab Migration Group",
                "entities": [logical],
                "hide_members": False,
            },
        )
        before_registry = await ha.ws("config/entity_registry/get", entity_id=entities[2])
        await ha.ws(
            "config/entity_registry/update",
            entity_id=logical,
            new_entity_id=logical + "_legacy",
            disabled_by="user",
            hidden_by="user",
        )
        old = entities[2]
        await ha.ws("config/entity_registry/update", entity_id=old, new_entity_id=logical)
        await ha.reload_entry(group_entry)
        after_registry = await ha.ws("config/entity_registry/get", entity_id=logical)
        assert after_registry["unique_id"] == before_registry["unique_id"]  # nosec B101
        assert after_registry["platform"] == "ha_operator"  # nosec B101
        await ha.service(
            "cover",
            "set_cover_position",
            {"entity_id": "cover.lab_migration_group", "position": 35},
        )
        await position(35, 2)
        assert (await explain(2))["manual"]["target"]["position"] == 35  # nosec B101
        await ha.ws("config/entity_registry/update", entity_id=logical, new_entity_id=old)
        await ha.service("ha_operator", "release", {"resource_id": resources[2]})

    async with lab.scenario("WINDOW-RELOAD-PRESERVES-RELAY-FAN"):
        outputs = [
            "switch.sim_cellar_" + suffix + "_relay" for suffix in ("low", "inward", "outward")
        ]
        fan = await ha.add_subentry(
            lab.entry,
            "resource",
            {
                "name": "Lab Independent Fan",
                "kind": "relay_fan",
                "outputs": outputs,
                "profiles": {
                    "off": {
                        "outputs": dict.fromkeys(outputs, False),
                        "percentage": 0,
                        "direction": "forward",
                    },
                    "inward": {
                        "outputs": dict(zip(outputs, [True, True, False], strict=True)),
                        "percentage": 100,
                        "direction": "forward",
                    },
                    "outward": {
                        "outputs": dict(zip(outputs, [True, False, True], strict=True)),
                        "percentage": 100,
                        "direction": "reverse",
                    },
                },
                "default_target": {"profile": "inward"},
                "reversal_dead_time": 0.5,
                "retry_interval": 1,
                "command_interval": 0.2,
            },
        )
        selector = await ha.managed(fan, "select")
        await ha.service("select", "select_option", {"entity_id": selector, "option": "live"})
        await ha.service(
            "ha_operator",
            "request",
            {"resource_id": fan, "target": {"profile": "inward"}, "duration": 120},
        )
        await eventually(lambda: lab.physical("cellar_inward_relay"), lambda state: state["on"])
        original = (
            await ha.service("ha_operator", "explain", {"resource_id": fan}, response=True)
        )["service_response"]["resources"][fan]["manual"]
        marker = (await lab.sim())["journal_seq"]
        for _ in range(3):
            await ha.reload_entry(lab.entry)
            current = (
                await ha.service("ha_operator", "explain", {"resource_id": fan}, response=True)
            )["service_response"]["resources"][fan]["manual"]
            assert current == original  # nosec B101
        events = [
            item
            for item in await lab.journal()
            if item["seq"] > marker and item["device_id"].startswith("cellar_")
        ]
        assert (
            not [  # nosec B101
                item
                for item in events
                if item["kind"] == "command" and item["data"]["action"] == "turn_off"
            ]
        )
