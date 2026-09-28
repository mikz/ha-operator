"""Real encrypted fan commands with independently refused and confirmed effects."""

import asyncio
import os
import re

from .hap import HAPClient


async def fan_hap(scenarios):
    from .runner import ARTIFACTS, STATE

    lab, ha = scenarios.lab, scenarios.ha
    async with scenarios.case("CELLAR-HAP-FAN-COMPOSITION") as proof:
        if lab.hap:
            await lab.hap.close()
            lab.hap = None
        entries = await ha.ws("config_entries/get")
        bridge = next(item["entry_id"] for item in entries if item["domain"] == "homekit")
        await ha.request("POST", f"/api/config/config_entries/entry/{bridge}/reload", {})
        pairing = STATE / "homekit-pairing.json"
        pin = None
        if not pairing.exists():
            notices = await scenarios.wait(
                lambda: ha.ws("persistent_notification/get"),
                lambda items: any(item.get("title") == "HomeKit Pairing" for item in items),
            )
            notice = next(item for item in notices if item.get("title") == "HomeKit Pairing")
            pin = re.search(r"\b\d{3}-\d{2}-\d{3}\b", notice["message"]).group()
            lab.record_secret(pin)
        lab.hap = hap = await HAPClient.connect(
            os.environ["LAB_HA_ADDRESS"],
            21063,
            pin,
            pairing,
            journal_path=ARTIFACTS / "hap-transcript.jsonl",
        )
        proof["commands"] = []
        for resource, entity, name, devices in (
            (scenarios.fan, scenarios.fan_entity, "Lab Cellar Fan", ("cellar_fan",)),
            (scenarios.relay, scenarios.relay_entity, "Lab Cellar Relays", scenarios.channels),
        ):
            active = hap.characteristic(name, "ACTIVE")
            direction = hap.characteristic(name, "ROTATION_DIRECTION")
            speed = hap.characteristic(name, "ROTATION_SPEED")
            await hap.subscribe([active, direction, speed])

            async def explained(resource=resource):
                result = await ha.service(
                    "ha_operator", "explain", {"resource_id": resource}, response=True
                )
                return result["service_response"]["resources"][resource]

            async def reset(resource=resource, entity=entity, devices=devices):
                for device in devices:
                    await scenarios.control(device, refuse_actions=[], telemetry_delay=0)
                await ha.service("fan", "turn_off", {"entity_id": entity})
                await scenarios.wait(
                    lambda: ha.state(entity), lambda state: state["state"] == "off"
                )
                await ha.service("ha_operator", "release", {"resource_id": resource})
                for device in devices:
                    await scenarios.control(device, refuse_actions=["turn_on"])

            for order in ("on_direction", "direction_on", "batch", "off_direction", "restart"):
                await reset()
                start = await scenarios.marker()
                if order == "direction_on":
                    await hap.write(direction, 1)
                    await scenarios.wait(explained, lambda value: value["manual"] is not None)
                    assert (await explained())["manual"]["target"]["on"] is False
                    await hap.write(active, 1)
                elif order == "batch":
                    await hap.write_many([(active, 1), (direction, 1), (speed, 100)])
                else:
                    await hap.write(active, 1)
                    if order == "off_direction":
                        await hap.write(active, 0)
                    await hap.write(direction, 1)
                expected_on = order != "off_direction"
                result = await scenarios.wait(
                    explained,
                    lambda value, expected_on=expected_on: (
                        value["manual"] is not None
                        and value["manual"]["target"].get("on") == expected_on
                        and (
                            not expected_on
                            or value["manual"]["target"].get("direction") == "reverse"
                        )
                    ),
                )
                assert (await ha.state(entity))["state"] == "off"
                if order == "restart":
                    await hap.close()
                    lab.hap = None
                    simulator = (await lab.sim(path="/health"))["instance_id"]
                    await lab.crash("restart")
                    await lab.ready()
                    assert (await lab.sim(path="/health"))["instance_id"] == simulator
                    assert (await explained())["manual"] == result["manual"]
                    lab.hap = hap = await HAPClient.connect(
                        os.environ["LAB_HA_ADDRESS"],
                        21063,
                        None,
                        pairing,
                        journal_path=ARTIFACTS / "hap-transcript.jsonl",
                    )
                    await hap.subscribe([active, direction, speed])
                before_effect = hap.sequence
                for device in devices:
                    await scenarios.control(device, refuse_actions=[])
                if expected_on:
                    await scenarios.fan_observed(entity, True, "reverse", 100)
                    # A HAP write may already set its characteristic to 1, so HA
                    # need not emit a duplicate event when feedback catches up.
                    assert await hap.read(active) == 1
                    assert await hap.read(direction) == 1
                    if resource == scenarios.fan:
                        physical = await lab.physical("cellar_fan")
                        assert physical["on"] and physical["direction"] == "reverse"
                    else:
                        assert await scenarios.bundle() == {
                            "cellar_low_relay": False,
                            "cellar_high_relay": True,
                            "cellar_inward_relay": False,
                            "cellar_outward_relay": True,
                        }
                    before_effect = hap.sequence
                    await ha.service("fan", "turn_off", {"entity_id": entity})
                    await scenarios.wait(
                        lambda entity=entity: ha.state(entity),
                        lambda value: value["state"] == "off",
                    )
                    await hap.wait_for_event(active, lambda value: value == 0, after=before_effect)
                else:
                    await asyncio.sleep(1.25)
                    assert (await ha.state(entity))["state"] == "off"
                    assert not any(
                        row["kind"] == "effect"
                        and row["device_id"] in devices
                        and row["data"].get("on")
                        for row in await scenarios.since(start)
                    )
                assert not [
                    row for row in await scenarios.since(start) if row["kind"] == "unsafe_command"
                ]
                proof["commands"].append(
                    {
                        "adapter": "native" if resource == scenarios.fan else "relay",
                        "order": order,
                        "accepted": result["manual"],
                        "after_sequence": start,
                        "through_sequence": await scenarios.marker(),
                    }
                )
            await reset()
            for device in devices:
                await scenarios.control(device, refuse_actions=[])
