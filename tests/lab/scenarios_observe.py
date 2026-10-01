"""Native observe lock validated against independent simulator effects."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .readiness import wait_operator_ready

ARTIFACTS = Path("/artifacts")


async def observe(lab, *, locked):
    await lab.ha.options(lab.entry, {"shadow_lock": locked})
    await wait_operator_ready(lab.ha, lab.entry, locked=locked)


async def lock(lab):
    await observe(lab, locked=True)


async def zero_commands(lab, start):
    events = (await lab.sim(path=f"/admin/journal?after={start}"))["events"]
    commands = [event for event in events if event["kind"] == "command"]
    assert not commands, f"Shadow lock dispatched physical commands: {commands}"
    return events


async def run_observe_scenarios(lab):
    from .runner import eventually

    # A current policy creates desired intent under the global lock. Neither
    # previous manual ownership nor a disabled signal can make zero work trivial.
    await lab.ha.service("ha_operator", "release", {"resource_id": lab.resource})
    await lab.ha.add_subentry(
        lab.entry,
        "policy",
        {
            "name": "Lab Shadow Target",
            "resource_id": lab.resource,
            "kind": "state",
            "priority": 999,
            "target": {"position": 61},
        },
    )
    await lock(lab)
    started = (await lab.sim(path="/health"))["journal_seq"]
    states = []
    async with lab.scenario("SHADOW-LOCK-ZERO-COMMANDS"):
        await lab.sim("POST", "/admin/devices/skylight", {"position": 15, "hidden_rain": False})
        for domain, service, data in (
            ("select", "select_option", {"entity_id": lab.mode, "option": "live"}),
            (
                "ha_operator",
                "request",
                {"resource_id": lab.resource, "target": {"position": 90}, "duration": 20},
            ),
            ("cover", "open_cover", {"entity_id": lab.cover}),
        ):
            rejected = await lab.ha.request(
                "POST", f"/api/services/{domain}/{service}", data, allow_error=True
            )
            assert rejected["status"] >= 400, rejected

        async def desired():
            data = await lab.ha.service(
                "ha_operator", "explain", {"resource_id": lab.resource}, response=True
            )
            return data["service_response"]["resources"][lab.resource]

        state = await eventually(
            desired,
            lambda data: (
                data.get("decision") is not None
                and data["decision"]["target"]["position"] == 61
                and data["decision"]["status"] == "observe"
            ),
        )
        assert state["manual"] is None
        states.append({"boundary": "locked", "state": state})
        await asyncio.sleep(1.2)
        await zero_commands(lab, started)

    async with lab.scenario("SHADOW-LOCK-RELOAD-RESTART"):
        for boundary in ("reload", "restart"):
            if boundary == "reload":
                await lab.ha.request(
                    "POST", f"/api/config/config_entries/entry/{lab.entry}/reload", {}
                )
            else:
                await lab.crash("restart")
                await lab.ready()
            state = await eventually(
                desired,
                lambda data: (
                    data.get("decision") is not None
                    and data["decision"]["target"]["position"] == 61
                    and data["decision"]["status"] == "observe"
                ),
            )
            assert (await lab.ha.state(lab.mode))["state"] == "observe"
            await lab.sim("POST", "/admin/devices/skylight", {"position": 25})
            await asyncio.sleep(1.2)
            await zero_commands(lab, started)
            states.append({"boundary": boundary, "state": state})
    events = await zero_commands(lab, started)
    (ARTIFACTS / "observe-lock-effects.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "status": "passed",
                "provenance": "simulator_generated",
                "physical_effects": "simulator_observed",
                "started_sequence": started,
                "completed_sequence": (await lab.sim(path="/health"))["journal_seq"],
                "command_count": 0,
                "boundaries": [item["boundary"] for item in states],
                "states": states,
                "events": events,
            },
            indent=2,
        )
        + "\n"
    )
