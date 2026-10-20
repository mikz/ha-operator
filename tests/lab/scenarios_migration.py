"""Supported 0.3 forms, refused preflight, and same-ID version-four migration."""

from __future__ import annotations

import asyncio
import json

from .runner import ARTIFACTS, eventually
from .scenarios_observe import observe


async def run_migration(lab):
    ha = lab.ha
    evidence = {"schema": 1, "configuration": "synthetic lab only", "snapshots": {}}

    async def snapshot(name):
        # Both native Store and config-entry writes use delayed saves. This is a
        # settled boundary, not a promise that an arbitrary live snapshot is atomic.
        await asyncio.sleep(2.2)
        first = (await lab.crash("migration-snapshot"))["migration"]
        await asyncio.sleep(1.2)
        second = (await lab.crash("migration-snapshot"))["migration"]
        assert first == second, "Migration snapshot changed across settled boundary"
        evidence["snapshots"][name] = second
        return second

    async def registered(identifier):
        rows = await ha.ws("config/entity_registry/list")
        return {
            row["unique_id"]: row["id"]
            for row in rows
            if row.get("config_subentry_id") == identifier
            and row["unique_id"]
            in {
                identifier + "_managed",
                identifier + "_mode",
                identifier + "_desired",
                identifier + "_observed",
            }
        }

    async with lab.scenario("MIGRATION-GENUINE-V3-FORMS"):
        await observe(lab, locked=False)
        config = {
            "name": "Lab migration actor",
            "kind": "cover",
            "entity_id": "cover.sim_inlet",
            "command_conditions": {
                "set_cover_position": [
                    {
                        "condition": "state",
                        "entity_id": "binary_sensor.sim_cellar_policy",
                        "state": "on",
                    }
                ]
            },
            "health_checks": [
                {
                    "alias": "Synthetic telemetry",
                    "conditions": [
                        {
                            "condition": "numeric_state",
                            "entity_id": "sensor.sim_cellar_target",
                            "above": 10,
                        }
                    ],
                }
            ],
        }
        resource = await ha.add_resource(lab.entry, config)
        policy_config = {
            "name": "Lab migration cold policy",
            "kind": "state",
            "resource_id": resource,
            "priority": 50,
            "target": {"position": 7},
            "input": {
                "type": "qualified_numeric",
                "entity_id": "sensor.sim_window_temperature",
                "threshold": 16,
                "unit": "°C",
                "qualification_seconds": 2,
            },
        }
        policy = await ha.add_subentry(lab.entry, "policy", policy_config)
        enabled = await ha.managed(policy, "switch")
        # The predicate is warm and has no accepted request. Disable deliberately
        # before changing its detector, preserving its subentry identity.
        await ha.service("switch", "turn_off", {"entity_id": enabled})
        await ha.ws("timer/create", name="Lab Migration Timer", duration="00:00:05", restore=True)
        timer = "timer.lab_migration_timer"
        timer_config = {
            "name": "Lab migration timer policy",
            "kind": "occurrence",
            "resource_id": resource,
            "priority": 20,
            "target": {"position": 25},
            "input": {
                "type": "timer_episode",
                "entity_id": timer,
                "qualification_seconds": 2,
                "request_seconds": 12,
            },
        }
        timed = await ha.add_subentry(lab.entry, "policy", timer_config)
        external_config = {
            "name": "Lab migration external occurrence",
            "kind": "occurrence",
            "resource_id": resource,
            "priority": 30,
            "target": {"position": 35},
            "eligibility_entity": "binary_sensor.sim_cellar_policy",
            "eligibility_state": "on",
        }
        external = await ha.add_subentry(lab.entry, "policy", external_config)
        for identifier in (timed, external):
            await ha.service(
                "switch", "turn_off", {"entity_id": await ha.managed(identifier, "switch")}
            )
        timer_registry = next(
            row["id"]
            for row in await ha.ws("config/entity_registry/list")
            if row["entity_id"] == timer
        )
        evidence["timer_policy_id"] = timed
        evidence["external_occurrence_policy_id"] = external
        mode = await ha.managed(resource, "select")
        await ha.service("select", "select_option", {"entity_id": mode, "option": "live"})
        await ha.service(
            "ha_operator",
            "request",
            {"resource_id": resource, "target": {"position": 42}, "duration": 1800},
        )
        identity = await registered(resource)
        baseline = await snapshot("baseline_v3_uncleared")
        assert baseline["entry"]["version"] == 3 and baseline["operator_store"] is not None
        evidence["resource_id"], evidence["policy_id"] = resource, policy

    async with lab.scenario("MIGRATION-UNCLEARED-REFUSED-WITHOUT-MUTATION"):
        await lab.crash("package-candidate")
        await lab.ready()
        await eventually(
            lambda: ha.ws("config_entries/get"),
            lambda rows: (
                next(row for row in rows if row["entry_id"] == lab.entry)["state"] != "loaded"
            ),
        )
        refused = await snapshot("candidate_refused")
        assert refused["entry"] == baseline["entry"]
        assert refused["operator_store"] == baseline["operator_store"]
        assert await registered(resource) == identity
        await lab.crash("package-baseline")
        await lab.ready()
        await ha.wait_entry_loaded(lab.entry)
        assert await registered(resource) == identity
        await snapshot("refusal_rollback_v3")

    async with lab.scenario("MIGRATION-CLEAR-THROUGH-V3-SUPPORTED-FORMS"):
        from .native_editor import (
            clear_v3_migration_fields,
            clear_v3_occurrence_eligibility,
            clear_v3_timer_input,
        )

        await clear_v3_migration_fields(lab, policy_config["name"], config["name"])
        await clear_v3_timer_input(lab, timer_config["name"])
        await clear_v3_occurrence_eligibility(lab, external_config["name"])
        for identifier in (policy, timed, external):
            assert (await ha.state(await ha.managed(identifier, "switch")))["state"] == "off"
        before = await snapshot("baseline_v3_cleared")
        assert before["entry"]["version"] == 3
        cleared = before["entry"]["subentries"]
        # Snapshot contains actual persisted native config, never a reconstructed fixture.
        for identifier in (policy, timed, external):
            row = next(item for item in cleared if item["subentry_id"] == identifier)
            assert not row["data"].get("input")
            assert "eligibility_entity" not in row["data"]
            assert "eligibility_state" not in row["data"]
        assert await registered(resource) == identity
        evidence["before_upgrade"] = before
        agents = (await ha.ws("backup/agents/info"))["agents"]
        assert any(item["agent_id"] == "backup.local" for item in agents)
        await ha.ws(
            "backup/generate",
            agent_ids=["backup.local"],
            name="Lab exact 0.3 rollback",
            include_homeassistant=True,
            include_database=True,
            password=None,
        )
        backups = await eventually(
            lambda: ha.ws("backup/info"),
            lambda result: (
                result["state"] == "idle"
                and any(item["name"] == "Lab exact 0.3 rollback" for item in result["backups"])
            ),
            timeout=120,
        )
        native_backup = next(
            item for item in backups["backups"] if item["name"] == "Lab exact 0.3 rollback"
        )
        evidence["native_backup_id"] = native_backup["backup_id"]

    async with lab.scenario("MIGRATION-SAME-ID-ACCEPTED-PRESERVES-STORE"):
        await lab.crash("package-candidate")
        await lab.ready()
        await ha.wait_entry_loaded(lab.entry)
        migrated = await snapshot("candidate_v4_migrated")
        assert migrated["entry"]["version"] == 4
        assert migrated["entry"]["entry_id"] == baseline["entry"]["entry_id"]
        assert await registered(resource) == identity
        before_store, after_store = (
            before["operator_store"]["data"],
            migrated["operator_store"]["data"],
        )
        assert after_store["manuals"] == before_store["manuals"]
        assert (await ha.state(enabled))["state"] == "off"
        assert not (await lab.physical("inlet"))["moving"]
    async with lab.scenario("MIGRATION-NATIVE-OCCURRENCE-ENABLED"):
        from .native_editor import exercise_occurrence_editor, prove_native_occurrence

        await exercise_occurrence_editor(lab, timer_config["name"], timed, "lab_migrated_native")
        # Manual preservation has already been proven. Deliberately withdraw this
        # synthetic lease so the migrated occurrence can win, then restore it via backup.
        await ha.service("ha_operator", "release", {"resource_id": resource})
        evidence["native_occurrence"] = await prove_native_occurrence(
            lab, resource, timed, "lab_migrated_native"
        )
        assert (
            next(
                row["id"]
                for row in await ha.ws("config/entity_registry/list")
                if row["entity_id"] == timer
            )
            == timer_registry
        )
        assert (await ha.state(timer))["state"] == "idle"
    async with lab.scenario("MIGRATION-NATIVE-BACKUP-ROLLBACK"):
        simulator = (await lab.sim())["instance_id"]
        restored_package = await lab.crash(
            "backup-restore",
            backup={"backup_id": native_backup["backup_id"], "agent_id": "backup.local"},
        )
        assert restored_package["package"]["version"] == "0.3.0"
        assert restored_package["package"]["installed_file_hashes_verified"]
        await lab.ready()
        await ha.wait_entry_loaded(lab.entry)
        restored = await snapshot("native_backup_restored_v3")
        assert restored["entry"]["version"] == 3
        assert restored["entry"]["entry_id"] == before["entry"]["entry_id"]
        assert restored["entry"]["subentries"] == before["entry"]["subentries"]
        assert (
            restored["operator_store"]["data"]["manuals"]
            == before["operator_store"]["data"]["manuals"]
        )
        assert (await lab.sim())["instance_id"] == simulator
        assert await registered(resource) == identity
    evidence["proven"] = [
        "genuine_v3_form_configuration",
        "uncleared_preflight_rejection_without_persisted_mutation",
        "same_id_v3_clearing",
        "version_four_migration",
        "manual_store_preservation",
        "refused_upgrade_rollback",
        "native_backup_successful_migration_rollback",
    ]
    evidence["status"] = "passed"
    (ARTIFACTS / "migration-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
