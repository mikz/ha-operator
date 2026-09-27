"""Synthetic cellar fault scenarios, not a reproduction of a historical incident."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .scenarios_airflow import _AirflowScenarios


async def run_cellar_scenarios(lab: Any) -> None:
    from .runner import ARTIFACTS, eventually

    cellar = _CellarScenarios(lab, eventually, ARTIFACTS / "cellar-evidence.json")
    await cellar.configure()
    try:
        await cellar.refused_actions()
        await cellar.off_feedback()
        await cellar.stale_availability()
        await cellar.fallback()
        await cellar.manual_expiry()
    finally:
        await cellar.activate(False)
        await cellar.signal("cellar_policy", False)


class _CellarScenarios(_AirflowScenarios):
    channels = (
        "cellar_low_relay",
        "cellar_high_relay",
        "cellar_inward_relay",
        "cellar_outward_relay",
    )

    def __init__(self, lab: Any, wait: Any, evidence_path: Path) -> None:
        super().__init__(lab, wait)
        self.evidence_path = evidence_path
        self.evidence: dict[str, Any] = {
            "schema": 1,
            "provenance": "simulator_generated",
            "physical_effects": "simulator_observed",
            "historical_reproduction": False,
            "scenarios": [],
        }

    @asynccontextmanager
    async def case(self, name: str):
        before = await self.marker()
        item: dict[str, Any] = {"id": name, "after_sequence": before, "status": "running"}
        self.evidence["scenarios"].append(item)
        try:
            async with self.lab.scenario(name):
                yield item
            item["status"] = "passed"
        except BaseException:
            item["status"] = "failed"
            raise
        finally:
            events = await self.since(before)
            item["journal_sequences"] = {
                kind: [event["seq"] for event in events if event["kind"] == kind]
                for kind in ("command", "effect", "feedback", "refusal", "unsafe_command")
            }
            snapshot = await self.lab.sim()
            item["simulator_instance_id"] = snapshot["instance_id"]
            item["last_sequence"] = snapshot["journal_seq"]
            item["physical_after"] = {
                device["id"]: device["physical"]
                for device in snapshot["devices"]
                if device["id"].startswith("cellar_") or device["id"] in {"airflow", "exhaust"}
            }
            await asyncio.to_thread(
                self.evidence_path.write_text, json.dumps(self.evidence, indent=2), encoding="utf-8"
            )

    async def signal(self, device: str, value: bool) -> None:
        await self.control(device, value=value)
        await self.wait(
            lambda: self.ha.state(f"binary_sensor.sim_{device}"),
            lambda state: state["state"] == ("on" if value else "off"),
        )

    async def activate(self, active: bool) -> None:
        await self.signal("cellar_demand", active)
        if not active and hasattr(self, "requirement"):
            await self.requirement_status("inactive")

    async def bundle(self) -> dict[str, bool]:
        return {
            item["id"]: item["physical"]["on"]
            for item in (await self.lab.sim())["devices"]
            if item["id"] in self.channels
        }

    async def fan_observed(self, entity: str, on: bool, direction: str, percentage: int) -> None:
        await self.wait(
            lambda: self.ha.state(entity),
            lambda state: (
                state["state"] == ("on" if on else "off")
                and state["attributes"].get("direction") == direction
                and state["attributes"].get("percentage") == percentage
            ),
        )

    async def native_off(self) -> None:
        await self.control(
            "cellar_fan",
            refuse_actions=[],
            suppress_off_feedback=False,
            unknown_direction=False,
            available=True,
            telemetry_delay=0,
        )
        await self.control(
            "cellar_on", suppress_off_feedback=False, available=True, telemetry_delay=0
        )
        await self.ha.service("fan", "turn_off", {"entity_id": self.fan_entity})
        await self.wait(lambda: self.lab.physical("cellar_fan"), lambda state: not state["on"])
        await self.wait(
            lambda: self.ha.state(self.fan_entity), lambda state: state["state"] == "off"
        )
        await self.ha.service("ha_operator", "release", {"resource_id": self.fan})

    async def configure(self) -> None:
        async with self.case("CELLAR-CONFIGURATION"):
            await self.signal("cellar_demand", False)
            await self.signal("cellar_policy", False)
            await self.signal("extraction", True)
            # Prior airflow scenarios leave this unrequested inlet physically open.
            # Establish the synthetic baseline before recording fault commands.
            await self.control("inlet", position=0)
            await self.control("cellar_inlet", position=0, speed=100)
            timing = {
                "retry_interval": 1,
                "command_interval": 0.2,
                "movement_timeout": 2,
                "manual_duration": 120,
            }
            self.fan = await self.ha.add_subentry(
                self.lab.entry,
                "resource",
                {
                    "name": "Lab Cellar Fan",
                    "kind": "fan",
                    "entity_id": "fan.sim_cellar_fan",
                    "default_target": {"on": True, "percentage": 100, "direction": "forward"},
                    **timing,
                },
            )
            self.inlet = await self.ha.add_subentry(
                self.lab.entry,
                "resource",
                {
                    "name": "Lab Cellar Fallback",
                    "kind": "cover",
                    "entity_id": "cover.sim_cellar_inlet",
                    **timing,
                },
            )
            outputs = [f"switch.sim_{channel}" for channel in self.channels]

            def profile(enabled: tuple[str, ...], speed: int, direction: str):
                return {
                    "outputs": {key: key.removeprefix("switch.sim_") in enabled for key in outputs},
                    "percentage": speed,
                    "direction": direction,
                }

            self.relay = await self.ha.add_subentry(
                self.lab.entry,
                "resource",
                {
                    "name": "Lab Cellar Relays",
                    "kind": "relay_fan",
                    "outputs": outputs,
                    "profiles": {
                        "off": profile((), 0, "forward"),
                        "forward": profile(
                            ("cellar_low_relay", "cellar_inward_relay"), 50, "forward"
                        ),
                        "reverse": profile(
                            ("cellar_high_relay", "cellar_outward_relay"), 100, "reverse"
                        ),
                    },
                    "default_target": {"profile": "forward"},
                    "reversal_dead_time": 0.5,
                    **timing,
                },
            )
            self.policy = await self.ha.add_subentry(
                self.lab.entry,
                "policy",
                {
                    "name": "Lab Cellar Current Policy",
                    "kind": "state",
                    "resource_id": self.fan,
                    "target": {"on": True, "direction": "forward"},
                    "target_entity": "sensor.sim_cellar_target",
                    "target_field": "percentage",
                    "eligibility_entity": "binary_sensor.sim_cellar_policy",
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
                    "name": "Lab Cellar Confirmed Air",
                    "acquisition_timeout": 3,
                    "activation_entities": [
                        "binary_sensor.sim_cellar_demand",
                        "binary_sensor.sim_extraction",
                    ],
                    "providers": [
                        {
                            "id": "cellar_fan",
                            "resource_id": self.fan,
                            "target": {"on": True, "percentage": 100, "direction": "forward"},
                            "evidence": [
                                {
                                    "entity_id": "fan.sim_cellar_fan",
                                    "operator": "eq",
                                    "value": "on",
                                    "kind": "relay",
                                },
                                {
                                    "entity_id": "fan.sim_cellar_fan",
                                    "attribute": "direction",
                                    "operator": "eq",
                                    "value": "forward",
                                    "kind": "relay",
                                },
                                {
                                    "entity_id": "binary_sensor.sim_cellar_on",
                                    "operator": "eq",
                                    "value": "on",
                                    "kind": "relay",
                                },
                                airflow,
                            ],
                        },
                        {
                            "id": "cellar_inlet",
                            "resource_id": self.inlet,
                            "target": {"position": 70},
                            "evidence": [
                                {
                                    "entity_id": "cover.sim_cellar_inlet",
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
            self.relay_entity = await self.entity(self.relay, "fan")
            for resource in (self.fan, self.inlet, self.relay):
                await self.mode(resource, "live")
            await self.requirement_status("inactive")

    async def refused_actions(self) -> None:
        async with self.case("CELLAR-REFUSED-POWER-DIRECTION") as proof:
            start = await self.marker()
            await self.control("cellar_fan", refuse_actions=["set_direction"])
            await self.ha.service(
                "fan", "set_direction", {"entity_id": self.fan_entity, "direction": "reverse"}
            )
            await self.wait(
                lambda: self.since(start),
                lambda rows: any(
                    row["kind"] == "refusal"
                    and row["device_id"] == "cellar_fan"
                    and row["data"].get("action") == "set_direction"
                    for row in rows
                ),
            )
            assert (await self.lab.physical("cellar_fan"))["direction"] == "forward"
            await self.control("cellar_fan", refuse_actions=["turn_on"])
            await self.ha.service("fan", "turn_on", {"entity_id": self.fan_entity})
            await self.wait(
                lambda: self.since(start),
                lambda rows: any(
                    row["kind"] == "refusal"
                    and row["device_id"] == "cellar_fan"
                    and row["data"].get("action") == "turn_on"
                    for row in rows
                ),
            )
            assert not (await self.lab.physical("cellar_fan"))["on"]
            assert (await self.ha.state(self.fan_entity))["state"] == "off"
            await self.ha.service("ha_operator", "release", {"resource_id": self.fan})
            await self.control("cellar_fan", refuse_actions=[])
            await self.control("cellar_low_relay", refuse_actions=["turn_on"])
            await self.ha.service("fan", "turn_on", {"entity_id": self.relay_entity})
            await self.wait(self.bundle, lambda values: values["cellar_inward_relay"])
            assert not (await self.lab.physical("cellar_low_relay"))["on"]
            assert (await self.lab.physical("airflow"))["value"] == 0
            proof["direction_on_without_power_airflow"] = 0
            await self.ha.service("ha_operator", "release", {"resource_id": self.relay})
            await self.control("cellar_low_relay", refuse_actions=[])
            await self.ha.service("fan", "turn_off", {"entity_id": self.relay_entity})
            await self.wait(self.bundle, lambda state: not any(state.values()))
            await self.extraction_unchanged(start)

    async def off_feedback(self) -> None:
        async with self.case("CELLAR-OFF-FEEDBACK-REVERSAL") as proof:
            await self.ha.service("fan", "turn_on", {"entity_id": self.relay_entity})
            await self.fan_observed(self.relay_entity, True, "forward", 50)
            await self.control("cellar_low_relay", suppress_off_feedback=True)
            before = await self.marker()
            await self.ha.service(
                "fan", "set_direction", {"entity_id": self.relay_entity, "direction": "reverse"}
            )
            await self.wait(self.bundle, lambda state: not any(state.values()))
            await asyncio.sleep(3.25)
            assert (await self.ha.state("switch.sim_cellar_low_relay"))["state"] == "on"
            assert not any((await self.bundle()).values())
            blocked = await self.since(before)
            assert not [
                row
                for row in blocked
                if row["kind"] == "command"
                and row["device_id"] in self.channels
                and row["data"].get("action") == "turn_on"
            ]
            clear = await self.marker()
            await self.control("cellar_low_relay", suppress_off_feedback=False)
            await self.fan_observed(self.relay_entity, True, "reverse", 100)
            events = await self.since(clear)
            observed_off = next(
                row
                for row in events
                if row["kind"] == "feedback"
                and row["device_id"] == "cellar_low_relay"
                and row["data"]["observation"].get("on") is False
            )
            energized = next(
                row
                for row in events
                if row["kind"] == "effect"
                and row["device_id"] in self.channels
                and row["data"].get("on") is True
            )
            assert energized["seq"] > observed_off["seq"]
            assert energized["monotonic"] - observed_off["monotonic"] >= 0.49
            assert not [row for row in await self.since(before) if row["kind"] == "unsafe_command"]
            proof.update(
                confirmed_off_sequence=observed_off["seq"], reenergized_sequence=energized["seq"]
            )
            await self.ha.service("fan", "turn_off", {"entity_id": self.relay_entity})
            await self.wait(self.bundle, lambda state: not any(state.values()))

    async def stale_availability(self) -> None:
        async with self.case("CELLAR-STALE-VIRTUAL-AVAILABILITY") as proof:
            await self.mode(self.inlet, "observe")
            await self.ha.service("fan", "turn_on", {"entity_id": self.fan_entity})
            await self.fan_observed(self.fan_entity, True, "forward", 100)
            await self.wait(
                lambda: self.ha.state("binary_sensor.sim_cellar_on"),
                lambda state: state["state"] == "on",
            )
            await self.control("cellar_on", suppress_off_feedback=True)
            await self.ha.service("fan", "turn_off", {"entity_id": self.fan_entity})
            await self.wait(
                lambda: self.ha.state(self.fan_entity), lambda state: state["state"] == "off"
            )
            await self.activate(True)
            await self.requirement_status("unmet")
            assert (await self.ha.state("binary_sensor.sim_cellar_on"))["state"] == "on"
            assert not (await self.lab.physical("cellar_fan"))["on"]
            assert (await self.lab.physical("airflow"))["value"] == 0
            proof["stale_virtual_on_with_physical_off"] = True
            await self.control("cellar_fan", unknown_direction=True)
            await self.ha.service("fan", "turn_on", {"entity_id": self.fan_entity})
            await self.wait(lambda: self.lab.physical("cellar_fan"), lambda state: state["on"])
            states = []
            for available in (False, True, False, True):
                await self.control("cellar_fan", available=available)
                await self.wait(
                    lambda: self.ha.state("fan.sim_cellar_fan"),
                    lambda state, available=available: (
                        state["state"] == ("on" if available else "unavailable")
                    ),
                )
                result = await self.requirement_state()
                assert result["status"] != "satisfied", result
                states.append(result["status"])
                if available:
                    assert (await self.ha.state("fan.sim_cellar_fan"))["attributes"].get(
                        "direction"
                    ) is None
            proof["requirement_states_during_flaps"] = states
            await self.control("cellar_fan", unknown_direction=False)
            await self.control("cellar_on", suppress_off_feedback=False)
            assert (await self.requirement_status("satisfied"))["selected_provider"] == "cellar_fan"
            await self.activate(False)
            await self.native_off()

    async def fallback(self) -> None:
        async with self.case("CELLAR-FALLBACK-KEEP-EXTRACTING") as proof:
            await self.mode(self.inlet, "live")
            await self.control("cellar_fan", refuse_actions=["turn_on"])
            before = await self.marker()
            await self.activate(True)
            result = await self.requirement_status("satisfied")
            assert result["selected_provider"] == "cellar_inlet", result
            await self.lab.wait_position(70, device="cellar_inlet")
            assert not (await self.lab.physical("cellar_fan"))["on"]
            assert (await self.lab.physical("airflow"))["value"] >= 65
            assert any(
                row["kind"] == "refusal" and row["device_id"] == "cellar_fan"
                for row in await self.since(before)
            )
            await self.extraction_unchanged(before)
            proof["selected_provider"] = "cellar_inlet"
            await self.activate(False)
            await self.control("cellar_fan", refuse_actions=[])

    async def manual_expiry(self) -> None:
        async with self.case("CELLAR-MANUAL-SCOPE-EXPIRY") as proof:
            await self.native_off()
            await self.control("cellar_target", value=1)
            await self.wait(
                lambda: self.ha.state("sensor.sim_cellar_target"),
                lambda state: float(state["state"]) == 1,
            )
            await self.ha.service(
                "ha_operator",
                "request",
                {
                    "resource_id": self.fan,
                    "mode": "hands_off",
                    "duration": 3,
                },
            )
            before = await self.marker()
            await self.signal("cellar_policy", True)
            await asyncio.sleep(0.5)
            assert not (await self.lab.physical("cellar_fan"))["on"]
            await self.control("cellar_target", value=100)
            await self.wait(
                lambda: self.ha.state("sensor.sim_cellar_target"),
                lambda state: float(state["state"]) == 100,
            )
            assert not [
                row
                for row in await self.since(before)
                if row["kind"] == "command" and row["device_id"] == "cellar_fan"
            ]
            await self.fan_observed(self.fan_entity, True, "forward", 100)
            commands = [
                row
                for row in await self.since(before)
                if row["kind"] == "command" and row["device_id"] == "cellar_fan"
            ]
            powered = [
                row for row in commands if row["data"]["action"] in {"turn_on", "set_percentage"}
            ]
            assert powered and all(row["data"].get("percentage") == 100 for row in powered), powered
            explanation = await self.ha.service(
                "ha_operator", "explain", {"resource_id": self.fan}, response=True
            )
            resource = explanation["service_response"]["resources"][self.fan]
            assert resource["manual"] is None
            assert resource["decision"]["target"]["percentage"] == 100
            proof.update(
                expired_mode="hands_off",
                resumed_current_percentage=100,
                obsolete_percentage_replayed=False,
            )
            await self.signal("cellar_policy", False)
            await self.native_off()
