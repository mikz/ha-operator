"""Schedule acceptance through the packaged integration's public HA boundaries.

The timed-block test creates an actual native schedule helper. Occurrence tests
use stable identifiers containing explicit DST offsets; they exercise persistent
identity/absolute expiry across a real HA restart and timezone configuration
change, without pretending to advance the container's wall clock through DST.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


async def _eventually(*args, **kwargs):
    # Resolve after runner initialization so it can import this scenario module directly.
    from .runner import eventually

    return await eventually(*args, **kwargs)


async def _entity(lab, subentry, domain):
    async def lookup():
        registry = await lab.ha.ws("config/entity_registry/list")
        return next(
            (
                item["entity_id"]
                for item in registry
                if item.get("platform") == "ha_operator"
                and item.get("config_subentry_id") == subentry
                and item["entity_id"].startswith(domain + ".")
            ),
            None,
        )

    entity_id = await _eventually(lookup)
    await _ready_entity(lab, entity_id)
    return entity_id


async def _ready_entity(lab, entity_id):
    async def lookup():
        states = await lab.ha.request("GET", "/api/states")
        return next(
            (
                state
                for state in states
                if state["entity_id"] == entity_id
                and state["state"] not in {"unavailable", "unknown"}
            ),
            None,
        )

    return await _eventually(lookup, timeout=60)


async def _enabled(lab, policy, value):
    switch = await _entity(lab, policy, "switch")
    await lab.ha.service("switch", "turn_on" if value else "turn_off", {"entity_id": switch})
    await _eventually(
        lambda: lab.ha.state(switch),
        lambda state: state["state"] == ("on" if value else "off"),
    )


async def _prepare(lab):
    await lab.sim("POST", "/admin/devices/skylight", {"hidden_rain": False, "speed": 100})
    await lab.ha.service("select", "select_option", {"entity_id": lab.mode, "option": "live"})
    await lab.request(0)
    await lab.wait_position(0)
    await lab.ha.service("ha_operator", "release", {"resource_id": lab.resource})


async def _occurrence(lab, policy, identity, expiry, *, skip=False):
    result = await lab.ha.service(
        "ha_operator",
        "skip_occurrence" if skip else "submit_occurrence",
        {"policy_id": policy, "occurrence_id": identity, "expires_at": expiry},
        response=not skip,
    )
    return None if skip else result["service_response"]


async def _commands_since(lab, sequence):
    return [
        event
        for event in await lab.journal()
        if event["kind"] == "command"
        and event["device_id"] == "skylight"
        and event["seq"] > sequence
    ]


async def _assert_quiet(lab, sequence, position, seconds=2.3):
    """Watch beyond two one-second retries; inspect physical output, not status."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert not await _commands_since(lab, sequence), "Suppressed/expired intent dispatched"
        physical = await lab.physical()
        assert abs(physical["position"] - position) <= 2, physical
        assert not physical["moving"], physical
        await asyncio.sleep(0.2)


async def _restart(lab):
    instance = (await lab.sim(path="/health"))["instance_id"]
    await lab.crash("restart")
    await lab.ready()
    await _ready_entity(lab, lab.cover)
    assert (await lab.sim(path="/health"))["instance_id"] == instance


async def sleep_in(lab):
    """Skip before admission survives restart/re-enable; other controls still work."""
    morning = evening = None
    async with lab.scenario("SCHEDULE-SLEEP-IN-DURABLE"):
        await _prepare(lab)
        morning = await lab.ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Lab Morning Occurrence",
                "kind": "occurrence",
                "resource_id": lab.resource,
                "target": {"position": 85},
                "priority": 20,
            },
        )
        evening = await lab.ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Lab Evening Occurrence",
                "kind": "occurrence",
                "resource_id": lab.resource,
                "target": {"position": 15},
                "priority": 10,
            },
        )
        try:
            await _entity(lab, morning, "switch")
            await _entity(lab, evening, "switch")
            expiry = time.time() + 600
            identity = "morning/" + datetime.now(UTC).date().isoformat()
            await _occurrence(lab, morning, identity, expiry, skip=True)
            first = await _occurrence(lab, morning, identity, expiry)
            assert first["skipped"] is True
            assert first["expires_at"] == expiry
            before = (await lab.sim(path="/health"))["journal_seq"]
            await _assert_quiet(lab, before, 0)
            await _restart(lab)
            await _enabled(lab, morning, False)
            await _enabled(lab, morning, True)
            duplicate = await _occurrence(lab, morning, identity, time.time() + 1200)
            assert duplicate["skipped"] is True
            assert duplicate["expires_at"] == expiry, "Duplicate occurrence extended its lifetime"
            await _assert_quiet(lab, before, 0)

            evening_id = "evening/" + datetime.now(UTC).date().isoformat()
            receipt = await _occurrence(lab, evening, evening_id, time.time() + 180)
            assert receipt["skipped"] is False
            await lab.wait_position(15)
            evening_commands = await _commands_since(lab, before)
            assert any(e["data"].get("position") == 15 for e in evening_commands)
            assert all(e["data"].get("position") != 85 for e in evening_commands)
            await lab.request(65)
            await lab.wait_position(65)
            await lab.ha.service("ha_operator", "release", {"resource_id": lab.resource})
            await lab.wait_position(15)
            commands = await _commands_since(lab, before)
            targets = [event["data"].get("position") for event in commands]
            assert 65 in targets and targets[-1] == 15, targets
            assert 85 not in targets, "Morning catch-up happened during evening/manual control"
        finally:
            for policy in (morning, evening):
                if policy is not None:
                    await _enabled(lab, policy, False)
            await lab.ha.service("ha_operator", "release", {"resource_id": lab.resource})


def _timed_blocks(start, boundary, end):
    """Construct adjacent native blocks, also when the run straddles midnight."""
    days = {day: [] for day in DAYS}
    for left, right, position in ((start, boundary, 20), (boundary, end, 75)):
        while left < right:
            midnight = datetime.combine(
                left.date() + timedelta(days=1), datetime.min.time(), tzinfo=left.tzinfo
            )
            stop = min(right, midnight)
            days[DAYS[left.weekday()]].append(
                {
                    "from": left.strftime("%H:%M:%S"),
                    "to": "24:00:00" if stop == midnight else stop.strftime("%H:%M:%S"),
                    "data": {"position": position},
                }
            )
            left = stop
    return days


async def adjacent_blocks(lab):
    """Native schedule stays on at the boundary while target attributes change."""
    policy = helper_id = None
    async with lab.scenario("SCHEDULE-ADJACENT-NATIVE-BLOCKS"):
        await _prepare(lab)
        helper = await lab.ha.ws("schedule/create", name="Lab Adjacent Position Blocks")
        helper_id = helper["id"]

        async def helper_entity():
            registry = await lab.ha.ws("config/entity_registry/list")
            return next(
                (
                    item["entity_id"]
                    for item in registry
                    if item.get("platform") == "schedule" and item["unique_id"] == helper_id
                ),
                None,
            )

        try:
            schedule = await _eventually(helper_entity)
            await _ready_entity(lab, schedule)
            policy = await lab.ha.add_subentry(
                lab.entry,
                "policy",
                {
                    "name": "Lab Native Adjacent Schedule",
                    "kind": "state",
                    "resource_id": lab.resource,
                    "eligibility_entity": schedule,
                    "eligibility_state": "on",
                    "target_entity": schedule,
                    "target_attribute": "position",
                    "target_field": "position",
                    "priority": 30,
                },
            )
            await _entity(lab, policy, "switch")
            config = await lab.ha.request("GET", "/api/config")
            start = (datetime.now(ZoneInfo(config["time_zone"])) + timedelta(seconds=3)).replace(
                microsecond=0
            )
            boundary, end = start + timedelta(seconds=15), start + timedelta(seconds=30)
            before = (await lab.sim(path="/health"))["journal_seq"]
            await lab.ha.ws(
                "schedule/update",
                schedule_id=helper_id,
                name="Lab Adjacent Position Blocks",
                **_timed_blocks(start, boundary, end),
            )
            first = await _eventually(
                lambda: lab.ha.state(schedule),
                lambda state: state["state"] == "on" and state["attributes"].get("position") == 20,
                timeout=15,
            )
            await lab.wait_position(20)
            second = await _eventually(
                lambda: lab.ha.state(schedule),
                lambda state: state["state"] == "on" and state["attributes"].get("position") == 75,
                timeout=25,
            )
            assert first["last_changed"] == second["last_changed"], (
                "Eligibility toggled at boundary"
            )
            assert first["last_updated"] != second["last_updated"]
            await lab.wait_position(75)
            commands = await _commands_since(lab, before)
            targets = [event["data"].get("position") for event in commands]
            assert 20 in targets and 75 in targets, targets
            first_75 = next(event for event in commands if event["data"].get("position") == 75)
            assert first_75["time"] >= boundary.timestamp(), (
                "Future schedule target dispatched early"
            )
            assert not any(
                event["seq"] > first_75["seq"] and event["data"].get("position") == 20
                for event in commands
            ), "Superseded block dispatched after adjacent target"
            await _eventually(
                lambda: lab.ha.state(schedule), lambda state: state["state"] == "off", timeout=25
            )
            after = (await lab.sim(path="/health"))["journal_seq"]
            await _assert_quiet(lab, after, 75)
        finally:
            if policy is not None:
                await _enabled(lab, policy, False)
            if helper_id is not None:
                # Keep the configured source entity present for subsequent resource-flow
                # validation; leave its completed schedule empty and its policy disabled.
                await lab.ha.ws(
                    "schedule/update",
                    schedule_id=helper_id,
                    name="Lab Adjacent Position Blocks",
                    **{day: [] for day in DAYS},
                )


async def dst_identity_and_expiry(lab):
    """Absolute deadlines and opaque DST-hour identities survive reload/timezone changes."""
    policy = None
    original_zone = (await lab.ha.request("GET", "/api/config"))["time_zone"]
    # The two occurrences of 02:30 in Prague's 2026 autumn fold have different UTC offsets.
    # Explicit-offset IDs are persisted verbatim; this does not claim to simulate a clock jump.
    first_id = "dst/2026-10-25T02:30:00+02:00"
    second_id = "dst/2026-10-25T02:30:00+01:00"
    assert (
        datetime.fromisoformat(second_id[4:]) - datetime.fromisoformat(first_id[4:])
    ).total_seconds() == 3600
    async with lab.scenario("OCCURRENCE-DST-IDENTITY-EXPIRY"):
        await _prepare(lab)
        policy = await lab.ha.add_subentry(
            lab.entry,
            "policy",
            {
                "name": "Lab DST Occurrence",
                "kind": "occurrence",
                "resource_id": lab.resource,
                "target": {"position": 80},
                "priority": 40,
            },
        )
        try:
            await _entity(lab, policy, "switch")
            await lab.ha.ws("config/core/update", time_zone="Europe/Prague")
            await lab.sim("POST", "/admin/devices/skylight", {"hidden_rain": True})
            expiry = time.time() + 4
            initial = await _occurrence(lab, policy, first_id, expiry)
            duplicate = await _occurrence(lab, policy, first_id, time.time() + 300)
            assert initial == duplicate and duplicate["expires_at"] == expiry
            marker = (await lab.sim(path="/health"))["journal_seq"]
            await _eventually(
                lab.journal,
                lambda rows: any(
                    event["device_id"] == "skylight"
                    and event["kind"] == "refusal"
                    and event["seq"] > marker
                    for event in rows
                ),
            )
            await _restart(lab)
            # Preserve actual wall time; expiry must elapse, not merely be asserted in metadata.
            await asyncio.sleep(max(0, expiry - time.time()) + 0.3)
            await lab.ha.ws("config/core/update", time_zone="UTC")
            replay = await _occurrence(lab, policy, first_id, time.time() + 300)
            assert replay["expires_at"] == expiry and replay["occurrence_id"] == first_id
            before = (await lab.sim(path="/health"))["journal_seq"]
            await lab.sim("POST", "/admin/devices/skylight", {"hidden_rain": False})
            await _assert_quiet(lab, before, 0)

            second_expiry = time.time() + 4
            second = await _occurrence(lab, policy, second_id, second_expiry)
            assert second["occurrence_id"] != initial["occurrence_id"]
            assert second["expires_at"] == second_expiry and second["skipped"] is False
            await lab.wait_position(80)
            assert any(
                event["data"].get("position") == 80 for event in await _commands_since(lab, before)
            )
            await asyncio.sleep(max(0, second_expiry - time.time()) + 0.3)
            await lab.request(0)
            await lab.wait_position(0)
            await lab.ha.service("ha_operator", "release", {"resource_id": lab.resource})
            before = (await lab.sim(path="/health"))["journal_seq"]
            await _occurrence(lab, policy, second_id, time.time() + 300)
            await _assert_quiet(lab, before, 0)
        finally:
            if policy is not None:
                await _enabled(lab, policy, False)
            await lab.ha.ws("config/core/update", time_zone=original_zone)
            await lab.sim("POST", "/admin/devices/skylight", {"hidden_rain": False})
            await lab.ha.service("ha_operator", "release", {"resource_id": lab.resource})


async def schedule_scenarios(lab):
    """Run sequentially after cover checks and before airflow reuses the raw skylight."""
    await sleep_in(lab)
    await adjacent_blocks(lab)
    await dst_identity_and_expiry(lab)
