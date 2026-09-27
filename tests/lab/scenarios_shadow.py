"""Native locked observation, exported trace, and exact artifact replay acceptance."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .readiness import wait_trace_ready
from .replay import write_replay
from .shadow_trace import load_trace

ARTIFACTS = Path("/artifacts")
ARCHIVE = Path("/opt/ha-operator/ha_operator.zip")


async def export_page(lab, after=None):
    fields = {"config_entry_id": lab.entry, "limit": 1000}
    if after is not None:
        fields["after"] = after
    result = await lab.ha.service("ha_operator", "export_trace", fields, response=True)
    return result["service_response"]


async def observe(lab, *, locked, extras=()):
    previous = (await export_page(lab))["health"]["session_id"]
    await lab.ha.options(
        lab.entry,
        {"trace_enabled": True, "shadow_lock": locked, "trace_entities": list(extras)},
    )
    # HA.options includes the awaited native reload barrier. The changed session
    # also prevents treating stale pre-reload runtime data as success.
    await wait_trace_ready(lab.ha, lab.entry, previous, locked=locked)


async def lock(lab, extras=()):
    await observe(lab, locked=True, extras=extras)


async def zero_commands(lab, start):
    events = (await lab.sim(path=f"/admin/journal?after={start}"))["events"]
    commands = [event for event in events if event["kind"] == "command"]
    assert not commands, f"Shadow lock dispatched physical commands: {commands}"
    return events


async def run_shadow_scenarios(lab):
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
    await lock(lab, ("fan.sim_cellar_fan", "binary_sensor.sim_cellar_on"))
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
    (ARTIFACTS / "shadow-lock-journal.json").write_text(
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
    async with lab.scenario("SHADOW-TRACE-EXPORT"):
        pages, after = [], None
        for _ in range(100):
            page = await export_page(lab, after)
            pages.append(page)
            if not page["more"]:
                break
            assert page["next_after"] != after, "Export cursor did not advance"
            after = page["next_after"]
        else:
            raise AssertionError("Native trace export exceeded bounded pagination")
        path = ARTIFACTS / "shadow-trace.json"
        path.write_text(json.dumps({"schema": 1, "pages": pages}, indent=2) + "\n")
        normalized = load_trace(path)
        assert normalized.report["replay_complete"], normalized.report
        assert any(row["kind"] == "input" for row in normalized.records)
        assert any(row["kind"] == "decision" for row in normalized.records)
        locked_sessions = {
            row["session_id"]
            for row in normalized.records
            if row["kind"] == "session_start" and row["data"]["shadow_lock"]
        }
        assert not any(
            row["kind"] == "dispatch" and row["session_id"] in locked_sessions
            for row in normalized.records
        )
        if os.environ["LAB_SCENARIO"] == "all":
            assert any(
                row["kind"] == "admission"
                and row["data"]["action"] == "request"
                and row["data"].get("manual", {}).get("mode") == "hands_off"
                for row in normalized.records
            ), "Accepted manual lease was not present in trace"
        assert len({row["session_id"] for row in normalized.records}) >= 3
    async with lab.scenario("SHADOW-TRACE-REPLAY"):
        report = write_replay(
            path, ARCHIVE, os.environ["LAB_ARTIFACT_SHA256"], ARTIFACTS / "shadow-replay.json"
        )
        assert report["status"] == "passed", report


async def run_external_replay(lab):
    """Replay exact historical engine clocks; native ingress probe has explicit limits."""
    path = Path("/control/shadow-input.json")
    trace = load_trace(path)
    target = ARTIFACTS / "shadow-trace.json"
    target.write_bytes(await asyncio.to_thread(path.read_bytes))
    inputs = {
        row["data"]["entity_id"]: row["data"] for row in trace.records if row["kind"] == "input"
    }
    await lock(lab, inputs)
    start = (await lab.sim(path="/health"))["journal_seq"]
    async with lab.scenario("SHADOW-TRACE-REPLAY"):
        # This is a bounded current-state ingress probe. It deliberately does not
        # transplant historical absolute expiry or claim native timing replay.
        for entity, data in inputs.items():
            if data["state"] is None:
                continue
            await lab.ha.request(
                "POST",
                f"/api/states/{entity}",
                {
                    "state": data["state"],
                    "attributes": data["attributes"],
                },
            )
        await asyncio.sleep(1.2)
        await zero_commands(lab, start)
        report = write_replay(
            target, ARCHIVE, os.environ["LAB_ARTIFACT_SHA256"], ARTIFACTS / "shadow-replay.json"
        )
        report["native_boundary"] = {
            "provenance": "recorded_feedback_ingress_probe",
            "scope": "final_state_per_entity",
            "entities": len(inputs),
            "clock": "current_wall_clock",
            "historical_timing": False,
            "physical_effects": "not_observed",
            "simulator_command_count": 0,
            "completed_at": time.time(),
        }
        (ARTIFACTS / "shadow-replay.json").write_text(json.dumps(report, indent=2) + "\n")
        assert report["status"] == "passed", report
