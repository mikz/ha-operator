"""Airflow acceptance through real HA and the independent physical simulator."""

from __future__ import annotations

import asyncio
from typing import Any


async def run_airflow_scenarios(lab: Any) -> None:
    """Run after the cover scenarios; all requests cross HA's public API."""
    # Import lazily so runner can import this entrypoint without a circular import.
    from .runner import eventually

    scenarios = _AirflowScenarios(lab, eventually)
    await scenarios.configure()
    try:
        await scenarios.off_direction()
        await scenarios.alternatives()
        await scenarios.manual_closure()
        await scenarios.handover()
        await scenarios.relay_reversal()
    finally:
        await scenarios.activate(False)


class _AirflowScenarios:
    def __init__(self, lab: Any, wait: Any) -> None:
        self.lab = lab
        self.wait = wait
        self.ha = lab.ha

    async def control(self, device: str, **values: Any) -> None:
        await self.lab.sim("POST", f"/admin/devices/{device}", values)

    async def entity(self, subentry: str, domain: str) -> str:
        async def registered_and_ready():
            registry = await self.ha.ws("config/entity_registry/list")
            ids = [
                item["entity_id"]
                for item in registry
                if item.get("platform") == "ha_operator"
                and item.get("config_subentry_id") == subentry
                and item["entity_id"].startswith(domain + ".")
            ]
            states = {
                item["entity_id"]: item for item in await self.ha.request("GET", "/api/states")
            }
            if len(ids) == 1 and states.get(ids[0], {}).get("state") not in {
                None,
                "unavailable",
                "unknown",
            }:
                return ids[0]
            return None

        return await self.wait(registered_and_ready)

    async def mode(self, resource: str, value: str) -> None:
        entity = await self.entity(resource, "select")
        await self.ha.service("select", "select_option", {"entity_id": entity, "option": value})

    async def activate(self, active: bool) -> None:
        await self.control("demand", value=active)
        await self.wait(
            lambda: self.ha.state("binary_sensor.sim_demand"),
            lambda state: state["state"] == ("on" if active else "off"),
        )
        if not active and hasattr(self, "requirement"):
            await self.requirement_status("inactive")

    async def requirement_state(self) -> dict[str, Any]:
        result = await self.ha.service("ha_operator", "explain", {}, response=True)
        return result["service_response"]["requirements"][self.requirement]

    async def requirement_status(self, status: str) -> dict[str, Any]:
        return await self.wait(
            self.requirement_state, lambda value: value["status"] == status, timeout=20
        )

    async def marker(self) -> int:
        return (await self.lab.sim(path="/health"))["journal_seq"]

    async def since(self, sequence: int) -> list[dict[str, Any]]:
        return (await self.lab.sim(path=f"/admin/journal?after={sequence}"))["events"]

    async def notification(self) -> list[dict[str, Any]]:
        return [
            item
            for item in await self.ha.ws("persistent_notification/get")
            if item.get("notification_id") == f"ha_operator_{self.lab.entry}_{self.requirement}"
        ]

    async def extraction_unchanged(self, sequence: int) -> None:
        physical = await self.lab.physical("exhaust")
        assert physical["on"] and physical["percentage"] == 100, physical
        events = await self.since(sequence)
        forbidden = [
            item
            for item in events
            if item["kind"] == "command"
            and item["device_id"] == "exhaust"
            and (item["data"]["action"] == "turn_off" or item["data"].get("percentage") == 0)
        ]
        assert not forbidden, f"Airflow fallback stopped extraction: {forbidden}"

    async def configure(self) -> None:
        async with self.lab.scenario("AIRFLOW-CONFIGURATION"):
            await self.control("demand", value=False)
            await self.control("extraction", value=True)
            await self.control("fireplace", value=False)
            await self.control("passive_window", value=False)
            await self.mode(self.lab.resource, "observe")
            await self.ha.service("ha_operator", "release", {"resource_id": self.lab.resource})
            await self.control("skylight", position=0, hidden_rain=False, speed=100)
            await self.control("inlet", position=0, hidden_rain=False, speed=100)
            await self.control("exhaust", on=False, percentage=0, direction="forward")
            timing = {
                "retry_interval": 1,
                "command_interval": 0.2,
                "movement_timeout": 4,
                "manual_duration": 300,
            }
            self.inlet = await self.ha.add_subentry(
                self.lab.entry,
                "resource",
                {
                    "name": "Lab Air Inlet",
                    "kind": "cover",
                    "entity_id": "cover.sim_inlet",
                    **timing,
                },
            )
            self.fan = await self.ha.add_subentry(
                self.lab.entry,
                "resource",
                {
                    "name": "Lab Air Fan",
                    "kind": "fan",
                    "entity_id": "fan.sim_exhaust",
                    "default_target": {"on": True, "percentage": 100, "direction": "forward"},
                    **timing,
                },
            )
            self.close_policy = await self.ha.add_subentry(
                self.lab.entry,
                "policy",
                {
                    "name": "Lab Close Unneeded Skylight",
                    "resource_id": self.lab.resource,
                    "kind": "state",
                    "target": {"position": 0},
                },
            )
            airflow = {
                "entity_id": "sensor.sim_airflow",
                "operator": "gte",
                "value": 60,
                "kind": "airflow",
            }
            self.requirement = await self.ha.add_subentry(
                self.lab.entry,
                "requirement",
                {
                    "name": "Lab Confirmed Incoming Air",
                    "activation_entities": [
                        "binary_sensor.sim_demand",
                        "binary_sensor.sim_extraction",
                    ],
                    "acquisition_timeout": 3,
                    "providers": [
                        {
                            "id": "reversed_fan",
                            "evidence": [
                                {
                                    "entity_id": "fan.sim_exhaust",
                                    "operator": "eq",
                                    "value": "on",
                                    "kind": "relay",
                                },
                                {
                                    "entity_id": "fan.sim_exhaust",
                                    "attribute": "direction",
                                    "operator": "eq",
                                    "value": "reverse",
                                    "kind": "relay",
                                },
                                airflow,
                            ],
                        },
                        {
                            "id": "skylight",
                            "resource_id": self.lab.resource,
                            "target": {"position": 70},
                            "evidence": [
                                {
                                    "entity_id": "cover.sim_skylight",
                                    "attribute": "current_position",
                                    "operator": "gte",
                                    "value": 65,
                                    "kind": "position",
                                },
                                {
                                    "entity_id": "binary_sensor.sim_fireplace",
                                    "operator": "eq",
                                    "value": "off",
                                    "kind": "contact",
                                },
                                airflow,
                            ],
                        },
                        {
                            "id": "inlet",
                            "resource_id": self.inlet,
                            "target": {"position": 70},
                            "evidence": [
                                {
                                    "entity_id": "cover.sim_inlet",
                                    "attribute": "current_position",
                                    "operator": "gte",
                                    "value": 65,
                                    "kind": "position",
                                },
                                airflow,
                            ],
                        },
                    ],
                },
            )
            self.fan_entity = await self.entity(self.fan, "fan")
            self.inlet_entity = await self.entity(self.inlet, "cover")
            await self.mode(self.fan, "live")
            await self.requirement_status("inactive")

    async def off_direction(self) -> None:
        async with self.lab.scenario("FAN-OFF-DIRECTION-NO-AIRFLOW"):
            before = await self.marker()
            await self.ha.service(
                "fan",
                "set_direction",
                {
                    "entity_id": self.fan_entity,
                    "direction": "reverse",
                },
            )
            await self.wait(
                lambda: self.lab.physical("exhaust"),
                lambda state: state["direction"] == "reverse" and not state["on"],
            )
            await self.activate(True)
            await self.requirement_status("unmet")
            await self.wait(self.notification)
            assert (await self.lab.physical("airflow"))["value"] == 0
            assert (await self.ha.state(self.fan_entity))["state"] == "off"
            events = await self.since(before)
            assert not [
                event
                for event in events
                if event["kind"] == "command"
                and event["device_id"] == "exhaust"
                and (event["data"]["action"] == "turn_on" or event["data"].get("percentage", 0) > 0)
            ], "Direction-only request energized an off fan"
            await self.activate(False)

    async def alternatives(self) -> None:
        async with self.lab.scenario("AIRFLOW-UNMET-ALTERNATIVES-KEEP-EXTRACTING"):
            # End the preceding explicit direction selection before testing the
            # configured bare-on default for this independent scenario.
            await self.ha.service("ha_operator", "release", {"resource_id": self.fan})
            await self.ha.service("fan", "turn_on", {"entity_id": self.fan_entity})
            await self.wait(
                lambda: self.lab.physical("exhaust"),
                lambda state: state["on"] and state["direction"] == "forward",
            )
            await self.mode(self.lab.resource, "live")
            await self.mode(self.inlet, "live")
            await self.control("skylight", hidden_rain=True)
            await self.control("inlet", hidden_rain=True)
            before = await self.marker()
            await self.activate(True)
            await self.requirement_status("unmet")
            await self.wait(self.notification)
            refusals = {
                event["device_id"]
                for event in await self.since(before)
                if event["kind"] == "refusal" and event["data"].get("reason") == "hidden_rain"
            }
            assert {"skylight", "inlet"} <= refusals, refusals
            assert (await self.lab.physical("airflow"))["value"] == 0
            await self.extraction_unchanged(before)
            # A new activation clears the exhausted acquisition round. Only the
            # alternative becomes available; the preferred skylight still refuses.
            await self.activate(False)
            await self.control("inlet", hidden_rain=False)
            retry = await self.marker()
            await self.activate(True)
            result = await self.requirement_status("satisfied")
            assert result["selected_provider"] == "inlet", result
            await self.lab.wait_position(70, device="inlet")
            await self.wait(self.notification, lambda notices: not notices)
            assert any(
                event["kind"] == "refusal" and event["device_id"] == "skylight"
                for event in await self.since(retry)
            ), "The blocked preferred provider was not attempted"
            await self.extraction_unchanged(before)

    async def manual_closure(self) -> None:
        async with self.lab.scenario("AIRFLOW-MANUAL-CLOSE-LAST-INLET"):
            assert (await self.lab.physical("skylight"))["position"] <= 2
            before = await self.marker()
            await self.ha.service("cover", "close_cover", {"entity_id": self.inlet_entity})
            await self.lab.wait_position(0, device="inlet")
            await self.requirement_status("unmet")
            await self.wait(self.notification)
            assert (await self.lab.physical("airflow"))["value"] == 0
            await asyncio.sleep(1.25)
            assert (await self.lab.physical("inlet"))["position"] <= 2
            commands = [
                event
                for event in await self.since(before)
                if event["kind"] == "command" and event["device_id"] == "inlet"
            ]
            assert commands and all(
                event["data"]["action"] == "close" or event["data"].get("position") == 0
                for event in commands
            ), commands
            await self.extraction_unchanged(before)
            await self.activate(False)

    async def handover(self) -> None:
        async with self.lab.scenario("AIRFLOW-MAKE-BEFORE-BREAK-PHYSICAL-CONFIRMATION"):
            await self.ha.service("ha_operator", "release", {"resource_id": self.inlet})
            await self.control("skylight", hidden_rain=False, position=0, speed=100)
            await self.control(
                "inlet", hidden_rain=False, position=0, speed=40, telemetry_delay=0.75
            )
            await self.control("fireplace", value=False)
            await self.activate(True)
            initial = await self.requirement_status("satisfied")
            assert initial["selected_provider"] == "skylight", initial
            await self.lab.wait_position(70)
            before = await self.marker()
            # Lose eligibility evidence while the old opening still supplies air.
            # The ordinary close policy must wait for real replacement feedback.
            await self.control("fireplace", value=True)
            await self.wait(
                self.requirement_state,
                lambda state: state["acquiring_provider"] == "inlet",
                timeout=10,
            )
            assert (await self.lab.physical("skylight"))["position"] >= 65
            final = await self.requirement_status("satisfied")
            assert final["selected_provider"] == "inlet", final
            await self.lab.wait_position(0)
            events = await self.since(before)
            confirmed = next(
                event
                for event in events
                if event["kind"] == "effect"
                and event["device_id"] == "inlet"
                and event["data"].get("action") == "position"
                and event["data"]["position"] >= 65
            )
            closures = [
                event
                for event in events
                if event["kind"] == "command"
                and event["device_id"] == "skylight"
                and (event["data"]["action"] == "close" or event["data"].get("position", 100) < 65)
            ]
            assert closures and all(event["seq"] > confirmed["seq"] for event in closures), {
                "replacement_physical_confirmation": confirmed,
                "old_provider_closures": closures,
            }
            assert closures[0]["monotonic"] - confirmed["monotonic"] >= 0.5, (
                "Old provider closed before delayed replacement feedback could arrive"
            )
            assert not [
                event
                for event in events
                if event["kind"] == "effect"
                and event["device_id"] == "airflow"
                and event["data"].get("value", 100) < 60
            ], "Physical airflow fell below the requirement during the autonomous handover"
            await self.extraction_unchanged(before)
            await self.activate(False)
            await self.control("inlet", telemetry_delay=0)

    async def relay_reversal(self) -> None:
        async with self.lab.scenario("RELAY-REVERSAL-CONFIRMED-DEAD-TIME"):
            channels = ("low_relay", "high_relay", "inward_relay", "outward_relay")
            outputs = [f"switch.sim_{channel}" for channel in channels]

            def profile(
                enabled: tuple[str, ...], percentage: int, direction: str
            ) -> dict[str, Any]:
                return {
                    "outputs": {
                        output: output.removeprefix("switch.sim_") in enabled for output in outputs
                    },
                    "percentage": percentage,
                    "direction": direction,
                }

            dead_time = 0.75
            resource = await self.ha.add_subentry(
                self.lab.entry,
                "resource",
                {
                    "name": "Lab Relay Ventilator",
                    "kind": "relay_fan",
                    "outputs": outputs,
                    "profiles": {
                        "off": profile((), 0, "forward"),
                        "forward": profile(("low_relay", "inward_relay"), 50, "forward"),
                        "reverse": profile(("high_relay", "outward_relay"), 100, "reverse"),
                    },
                    "default_target": {"profile": "forward"},
                    "reversal_dead_time": dead_time,
                    "retry_interval": 1,
                    "command_interval": 0.2,
                    "movement_timeout": 4,
                    "manual_duration": 300,
                },
            )
            fan = await self.entity(resource, "fan")
            await self.mode(resource, "live")

            async def bundle():
                states = (await self.lab.sim())["devices"]
                return {
                    item["id"]: item["physical"]["on"] for item in states if item["id"] in channels
                }

            initial = await self.marker()
            await self.ha.service("fan", "turn_on", {"entity_id": fan})
            await self.wait(
                bundle,
                lambda state: state == dict(zip(channels, (True, False, True, False), strict=True)),
            )
            # Direction-only intent preserves the observed on/off profile. Wait
            # for raw feedback to reach HA after proving the physical bundle.
            await self.wait(
                lambda: self.ha.state(fan),
                lambda state: state["state"] == "on"
                and state["attributes"].get("percentage") == 50
                and state["attributes"].get("direction") == "forward",
            )
            before = await self.marker()
            await self.ha.service(
                "fan", "set_direction", {"entity_id": fan, "direction": "reverse"}
            )
            await self.wait(
                bundle,
                lambda state: state == dict(zip(channels, (False, True, False, True), strict=True)),
            )
            events = await self.since(before)
            energized = next(
                event
                for event in events
                if event["kind"] == "effect"
                and event["device_id"] in channels
                and event["data"].get("on") is True
            )
            off = {
                channel: max(
                    event["monotonic"]
                    for event in events
                    if event["kind"] == "effect"
                    and event["device_id"] == channel
                    and event["data"].get("on") is False
                    and event["seq"] < energized["seq"]
                )
                for channel in channels
            }
            assert energized["monotonic"] - max(off.values()) >= dead_time - 0.01, {
                "all_channels_off_at": off,
                "first_reenergized": energized,
            }
            assert not [
                event for event in await self.since(initial) if event["kind"] == "unsafe_command"
            ]
            await self.ha.service("fan", "turn_off", {"entity_id": fan})
            await self.wait(bundle, lambda state: not any(state.values()))
