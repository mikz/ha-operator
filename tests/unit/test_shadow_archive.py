"""Streaming archive continuity stays distinct from recorder completeness guards."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.core import State

from custom_components.ha_operator.shadow import ShadowTrace
from scripts import shadow_archive as archive
from tests.lab.shadow_trace import TraceValidationError, validate_trace
from tests.unit.test_shadow import Host
from tests.unit.test_shadow_report import BINDINGS, CELLAR, VIRTUAL
from tests.unit.test_shadow_trace import RESOURCE, make_trace


def seeded():
    page = make_trace()
    template = page["records"][1]
    roles = {
        "virtual_fan": "on",
        "power": "on",
        "inward": "on",
        "outward": "off",
        "manual_flag": "on",
        "manual_timer": "idle",
        "cellar_timer": "idle",
    }
    extras = []
    for role, state in roles.items():
        row = deepcopy(template)
        row["data"].update(entity_id=CELLAR[role], state=state, attributes={})
        if role == "virtual_fan":
            row["data"]["attributes"] = {
                "direction": "reverse",
                "percentage": 67,
                "optimistic": True,
            }
        extras.append(row)
    rows = page["records"][:2] + extras + page["records"][2:]
    rows[-2]["data"]["entities"] = 8
    for sequence, row in enumerate(rows, 1):
        row["sequence"] = sequence
    rows[0]["data"]["config"]["trace_entities"] = list(CELLAR.values())
    config_hash = hashlib.sha256(
        json.dumps(rows[0]["data"]["config"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    rows[0]["data"]["config_hash"] = page["config_hash"] = config_hash
    return page, rows


def heartbeat(page, sequence, at):
    health = deepcopy(page["health"])
    health.update(last_sequence=sequence, durable_sequence=sequence, last_write_at=at)
    return {
        "schema": 1,
        "sequence": sequence,
        "session_id": health["session_id"],
        "at": at,
        "kind": "heartbeat",
        "data": health,
    }


def write_index(tmp_path, page, chunks, *, health=None, more=False):
    entries = []
    after = None
    for index, rows in enumerate(chunks):
        exported = deepcopy(page)
        exported["records"] = rows
        through = rows[-1]["sequence"]
        exported.update(next_after=through, through_sequence=through, more=more)
        exported["health"].update(
            last_sequence=through,
            durable_sequence=through,
            last_write_at=rows[-1]["at"],
            **(health or {}),
        )
        path = tmp_path / f"page-{index}.json"
        encoded = json.dumps(exported).encode()
        path.write_bytes(encoded)
        entries.append(
            {
                "file": path.name,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "request_after": after,
                "capture_at": index + 1000,
            }
        )
        after = through
    target = tmp_path / "index.json"
    target.write_text(json.dumps({"schema": 1, "pages": entries}))
    return target


def alter_page(index_path, number, mutate, *, rehash=True):
    index = json.loads(index_path.read_text())
    entry = index["pages"][number]
    path = index_path.parent / entry["file"]
    page = json.loads(path.read_text())
    mutate(page)
    encoded = json.dumps(page).encode()
    path.write_bytes(encoded)
    if rehash:
        entry["sha256"] = hashlib.sha256(encoded).hexdigest()
        index_path.write_text(json.dumps(index))


def test_two_terminal_hourly_exports_join_without_rewriting_pages(tmp_path):
    page, rows = seeded()
    later = [heartbeat(page, len(rows) + 1, 160)]
    path = write_index(tmp_path, page, [rows, later])
    original = [p.read_bytes() for p in sorted(tmp_path.glob("page-*.json"))]
    with pytest.raises(TraceValidationError, match="terminal"):
        validate_trace({"schema": 1, "pages": [json.loads(value) for value in original]})
    result = archive.analyze_archive(path, BINDINGS)
    assert result["capture"]["complete_observed_interval"]
    assert result["unique_records"] == len(rows) + 1
    assert result["cellar"]["episodes"]["raw_power_feedback_on"]["seconds"] == 60
    assert result["operator"]["dispatch_attempts"] == 0
    assert result["operator"]["last_modes"]
    assert original == [p.read_bytes() for p in sorted(tmp_path.glob("page-*.json"))]


def test_39_hour_cellar_span_crosses_hourly_pages(tmp_path):
    page, initial = seeded()
    all_rows = initial + [
        heartbeat(page, len(initial) + minute, 100 + minute * 60)
        for minute in range(1, 39 * 60 + 1)
    ]
    chunks = [all_rows[index : index + 60] for index in range(0, len(all_rows), 60)]
    path = write_index(tmp_path, page, chunks)
    result = archive.analyze_archive(path, BINDINGS)
    episodes = result["cellar"]["episodes"]
    assert episodes["legacy_manual_flag_active_idle_timers_power_on"]["seconds"] == 39 * 3600
    assert episodes["legacy_manual_flag_active_idle_timers_power_on"]["count"] == 1
    assert result["capture"]["complete_observed_interval"]
    assert result["capture"]["unknown_seconds"] == 0
    assert "manual actor attribution" in result["not_exercised"]


def test_streams_over_100000_records_without_combined_validator_limit(tmp_path):
    page, initial = seeded()
    template = deepcopy(initial[2])
    template["data"]["event_type"] = "state_reported"

    def chunks():
        rows = list(initial)
        for sequence in range(len(initial) + 1, 100_012):
            row = deepcopy(template)
            row.update(sequence=sequence, at=102 + (sequence - len(initial)) * 0.001)
            rows.append(row)
            if len(rows) == 1000:
                yield rows
                rows = []
        if rows:
            yield rows

    path = write_index(tmp_path, page, chunks())
    result = archive.analyze_archive(path, BINDINGS)
    assert result["unique_records"] == 100_011
    assert result["capture"]["complete_observed_interval"]
    assert result["pages"] == 101


def test_identical_retry_is_deduplicated_but_conflict_rejected(tmp_path):
    page, rows = seeded()
    path = write_index(tmp_path, page, [rows, rows[-2:]])
    index = json.loads(path.read_text())
    index["pages"][1]["request_after"] = rows[-3]["sequence"]
    path.write_text(json.dumps(index))
    result = archive.analyze_archive(path, BINDINGS)
    assert result["unique_records"] == len(rows) and result["duplicate_records"] == 2
    alter_page(path, 1, lambda value: value["records"][-1].update(at=103))
    with pytest.raises(archive.ArchiveError, match="Conflicting duplicate"):
        archive.analyze_archive(path, BINDINGS)


def test_hash_mismatch_rejected_before_data_is_trusted(tmp_path):
    page, rows = seeded()
    path = write_index(tmp_path, page, [rows])
    alter_page(path, 0, lambda value: value.update(gap=True), rehash=False)
    with pytest.raises(archive.ArchiveError, match="hash mismatch"):
        archive.analyze_archive(path)


def test_sequence_gap_and_heartbeat_gap_reset_carried_cellar_state(tmp_path):
    page, rows = seeded()
    path = write_index(tmp_path, page, [rows, [heartbeat(page, len(rows) + 2, 3600)]])
    result = archive.analyze_archive(path, BINDINGS)
    assert not result["capture"]["complete_observed_interval"]
    assert result["capture"]["findings"]["sequence_gaps"] == 1
    assert result["capture"]["findings"]["heartbeat_gaps"] == 1
    assert result["capture"]["unknown_seconds"] == 3498
    assert result["cellar"]["episodes"]["raw_power_feedback_on"]["seconds"] == 2
    assert result["operator"]["attempt_evidence"] == "zero_recorded; coverage_uncertain"


@pytest.mark.parametrize(
    "health,complete",
    [
        ({"rotations": 1, "complete": True}, True),
        ({"rotations": 3, "complete": False}, True),
        ({"write_errors": 1, "healthy": False, "complete": False}, False),
        ({"dropped_records": 1, "healthy": False, "complete": False}, False),
        ({"unclean_previous": True, "complete": False}, False),
        ({"enabled": False, "complete": False}, False),
        ({"healthy": False, "complete": False}, False),
        ({"rotations": 32, "history_gap": True, "complete": False}, False),
    ],
)
def test_health_retained_without_equating_rotation_with_lost_archive(tmp_path, health, complete):
    page, rows = seeded()
    path = write_index(tmp_path, page, [rows], health=health)
    result = archive.analyze_archive(path, BINDINGS)
    assert result["capture"]["complete_observed_interval"] is complete
    assert result["recorder_health"]["incomplete_pages"] == (not health["complete"])
    assert result["per_page_validation_reasons"].get("incomplete_health", 0) == (
        not health["complete"]
    )
    assert result["recorder_health"].get("rotations", 0) == health.get("rotations", 0)
    assert result["recorder_health"]["disabled_pages"] == (health.get("enabled") is False)
    assert result["recorder_health"]["unhealthy_pages"] == (health.get("healthy") is False)


def test_terminal_start_without_snapshot_is_incomplete(tmp_path):
    page, rows = seeded()
    result = archive.analyze_archive(write_index(tmp_path, page, [rows[:1]]))
    assert not result["capture"]["complete_observed_interval"]
    assert result["capture"]["findings"]["missing_initial_snapshots"] == 1


def test_snapshot_count_does_not_replace_configured_entity_coverage(tmp_path):
    page, rows = seeded()
    rows = rows[:2] + rows[-2:]
    rows[-2]["data"]["entities"] = 1
    for sequence, row in enumerate(rows, 1):
        row["sequence"] = sequence
    result = archive.analyze_archive(write_index(tmp_path, page, [rows]))
    assert not result["capture"]["complete_observed_interval"]
    assert result["capture"]["findings"]["incomplete_initial_snapshot"] == 1


def test_later_complete_session_does_not_repair_earlier_missing_snapshot(tmp_path):
    page, initial = seeded()
    first = deepcopy(initial[:1])
    end = heartbeat(page, 2, 100)
    end["kind"] = "session_end"
    rows = first + [end] + deepcopy(initial)
    second_session = "00000000-0000-4000-8000-000000000002"
    for sequence, row in enumerate(rows, 1):
        row["sequence"] = sequence
        if sequence > 2:
            row["session_id"] = second_session
    rows[2]["data"]["previous_session_closed"] = True
    result = archive.analyze_archive(write_index(tmp_path, page, [rows]))
    assert not result["capture"]["complete_observed_interval"]
    assert result["capture"]["findings"]["missing_initial_snapshots"] == 1


def test_explicit_gap_record_blocks_complete_even_at_same_timestamp(tmp_path):
    page, rows = seeded()
    row = heartbeat(page, len(rows) + 1, rows[-1]["at"])
    row.update(kind="gap", data={"lost_records": 1, "health": row["data"]})
    result = archive.analyze_archive(write_index(tmp_path, page, [rows + [row]]))
    assert not result["capture"]["complete_observed_interval"]
    assert result["capture"]["findings"]["recorded_gaps"] == 1


def test_unfinished_export_cannot_claim_complete_capture(tmp_path):
    page, rows = seeded()
    result = archive.analyze_archive(write_index(tmp_path, page, [rows], more=True))
    assert not result["capture"]["complete_observed_interval"]
    assert result["per_page_validation_reasons"]["pagination_incomplete"] == 1


def test_fingerprint_change_and_out_of_order_index_rejected(tmp_path):
    page, rows = seeded()
    path = write_index(tmp_path, page, [rows, [heartbeat(page, len(rows) + 1, 160)]])
    alter_page(path, 1, lambda value: value.update(component_sha256="b" * 64))
    with pytest.raises(archive.ArchiveError, match="fingerprint"):
        archive.analyze_archive(path)
    index = json.loads(path.read_text())
    index["pages"][1]["capture_at"] = 0
    path.write_text(json.dumps(index))
    with pytest.raises(archive.ArchiveError, match="Out-of-order"):
        archive.analyze_archive(path)


def test_capacity_limits_fail_without_false_complete_result(tmp_path, monkeypatch):
    page, rows = seeded()
    path = write_index(tmp_path, page, [rows])
    monkeypatch.setattr(archive, "MAX_RECORDS", 2)
    with pytest.raises(archive.ArchiveError, match="Capacity exceeded"):
        archive.analyze_archive(path)
    monkeypatch.setattr(archive, "MAX_RECORDS", 2_000_000)
    monkeypatch.setattr(archive, "RETRY_CACHE", 2)
    path = write_index(tmp_path, page, [rows, rows[:1]])
    index = json.loads(path.read_text())
    index["pages"][1]["request_after"] = None
    path.write_text(json.dumps(index))
    with pytest.raises(archive.ArchiveError, match="retry overlap"):
        archive.analyze_archive(path)


def test_cli_reads_private_index_writes_sanitized_report_and_never_overwrites(tmp_path, capsys):
    page, rows = seeded()
    index = write_index(tmp_path, page, [rows])
    bindings = tmp_path / "private-bindings.json"
    bindings.write_text(json.dumps(BINDINGS))
    output = tmp_path / "public-report.json"
    archive.main([str(index), "--bindings", str(bindings), "--output", str(output)])
    result = json.loads(output.read_text())
    assert result["cellar"]["configured"]
    assert str(tmp_path) not in output.read_text() and "page-0.json" not in output.read_text()
    before = output.read_bytes()
    with pytest.raises(SystemExit) as failure:
        archive.main([str(index), "--output", str(output)])
    assert failure.value.code == 2 and output.read_bytes() == before
    assert str(tmp_path) not in capsys.readouterr().err


def cover_seed(*, timeout=10, target=100):
    page = make_trace()
    config = page["records"][0]["data"]["config"]
    config["resources"][RESOURCE]["movement_timeout"] = timeout
    policy = "p_" + "3" * 20
    config["policies"][policy] = {
        "name": policy,
        "resource_id": RESOURCE,
        "kind": "state",
        "target_entity": VIRTUAL,
        "target_attribute": "current_position",
    }
    virtual = deepcopy(page["records"][1])
    virtual["data"].update(
        entity_id=VIRTUAL, state="open", attributes={"current_position": target, "optimistic": True}
    )
    rows = page["records"][:2] + [virtual] + page["records"][2:]
    rows[-2]["data"]["entities"] = 2
    return page, rows


def cover_input(rows, at, position, *, virtual=False, state="open", flags=None):
    row = deepcopy(rows[2 if virtual else 1])
    row.update(at=at)
    row["data"].update(
        event_type="state_reported",
        state=state,
        attributes={"current_position": position, **(flags or {})},
        last_changed=at,
        last_updated=at,
        last_reported=at,
    )
    return row


def cover_report(tmp_path, page, rows, *, split=None):
    for row in rows:
        if row["kind"] == "session_start":
            config_hash = hashlib.sha256(
                json.dumps(row["data"]["config"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            row["data"]["config_hash"] = page["config_hash"] = config_hash
    for sequence, row in enumerate(rows, 1):
        row["sequence"] = sequence
    chunks = [rows] if split is None else [rows[:split], rows[split:]]
    result = archive.analyze_archive(write_index(tmp_path, page, chunks))
    return result["cover_comparisons"]["resources"][RESOURCE]


def test_cover_normal_travel_is_pending_then_matched(tmp_path):
    page, rows = cover_seed()
    rows += [
        cover_input(rows, 104, 40, state="opening"),
        cover_input(rows, 108, 100),
        heartbeat(page, 0, 115),
    ]
    result = cover_report(tmp_path, page, rows)
    assert result["seconds_by_status"] == {
        "target_settling": 1,
        "pending_movement": 7,
        "matched": 7,
    }
    assert result["known_seconds"] == 15 and result["known_ratio"] == 1
    assert result["latest"]["requested_position"] == result["latest"]["observed_position"] == 100
    assert (
        result["latest"]["effective"]["target"] is None
    )  # A recorded idle decision, not requested100.
    assert result["latest"]["last_dispatched_command"] is None


def test_cover_repeated_target_telemetry_does_not_postpone_timeout_across_pages(tmp_path):
    page, rows = cover_seed()
    rows.insert(-1, cover_input(rows, 100.5, 100, virtual=True))
    rows += [
        cover_input(rows, 107, 100, virtual=True),
        heartbeat(page, 0, 112),
    ]
    result = cover_report(tmp_path, page, rows, split=len(rows) - 1)
    assert result["seconds_by_status"] == {
        "target_settling": 1,
        "pending_movement": 9,
        "sustained_divergence": 2,
    }
    assert result["target_changes"] == 0
    assert result["episode_counts"]["pending_movement"] == 1


def test_cover_supersession_restarts_settling_and_travel_deadline(tmp_path):
    page, rows = cover_seed()
    rows += [
        cover_input(rows, 108, 50, virtual=True),
        heartbeat(page, 0, 115),
        heartbeat(page, 0, 120),
    ]
    result = cover_report(tmp_path, page, rows)
    assert result["target_changes"] == 1
    assert result["seconds_by_status"]["target_settling"] == 2
    assert result["seconds_by_status"]["sustained_divergence"] == 2
    assert result["latest"]["requested_position"] == 50


@pytest.mark.parametrize(
    "state,flags",
    [("unavailable", {}), ("open", {"optimistic": True}), ("open", {"restored": True})],
)
def test_cover_unreliable_raw_feedback_is_unknown_not_divergence(tmp_path, state, flags):
    page, rows = cover_seed()
    rows += [cover_input(rows, 105, 50, state=state, flags=flags), heartbeat(page, 0, 115)]
    result = cover_report(tmp_path, page, rows)
    assert result["unknown_seconds"] == 10
    assert result["known_seconds"] == 5 and result["known_ratio"] == 1 / 3
    assert result["latest"]["observed_position"] is None
    assert "sustained_divergence" not in result["episode_counts"]


def test_cover_sequence_and_heartbeat_gaps_do_not_carry_comparisons(tmp_path):
    page, rows = cover_seed()
    rows += [heartbeat(page, 0, 205)]
    result = cover_report(tmp_path, page, rows)
    assert result["unknown_seconds"] == 103
    assert result["known_seconds"] == 2
    assert result["latest"]["requested_position"] is None
    assert result["latest"]["effective"] is None


def test_cover_missing_initial_snapshot_keeps_all_time_unknown(tmp_path):
    page, rows = cover_seed()
    rows = [row for row in rows if row["kind"] != "snapshot_end"] + [heartbeat(page, 0, 112)]
    result = cover_report(tmp_path, page, rows)
    assert result["unknown_seconds"] == 12 and result["known_ratio"] == 0


@pytest.mark.parametrize("policy_kind", ["fixed", "multiple"])
def test_cover_unsupported_policies_are_explicitly_not_compared(tmp_path, policy_kind):
    page, rows = cover_seed()
    policies = rows[0]["data"]["config"]["policies"]
    policy = next(iter(policies.values()))
    if policy_kind == "fixed":
        policy.pop("target_entity")
        policy["target"] = {"position": 100}
    else:
        other = "p_" + "4" * 20
        policies[other] = {**policy, "name": other}
    rows += [heartbeat(page, 0, 112)]
    result = cover_report(tmp_path, page, rows)
    assert not result["comparison_supported"] and result["not_compared_reason"]
    assert result["seconds_by_status"]["not_compared"] == 12
    assert result["known_ratio"] is None


def test_cover_effective_target_uses_frame_alias_not_virtual_request(tmp_path):
    page, rows = cover_seed()
    data = rows[-1]["data"]
    local_id = "r_" + "8" * 20
    data["engine_aliases"][RESOURCE] = local_id
    decision = data["engine_result"]["decisions"].pop(RESOURCE)
    decision.update(resource_id=local_id, target={"position": 25}, status="observe")
    data["engine_result"]["decisions"][local_id] = decision
    rows += [heartbeat(page, 0, 112)]
    result = cover_report(tmp_path, page, rows)
    assert result["latest"]["effective"]["target"]["position"] == 25
    assert result["latest"]["requested_position"] == 100
    assert result["latest"]["observed_position"] == 0
    assert result["effective_frame_known_seconds"] == 10


def test_cover_episode_output_is_bounded_without_losing_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "INTERVAL_LIMIT", 3)
    page, rows = cover_seed()
    for second in range(103, 114):
        rows.append(cover_input(rows, second, 0 if second % 2 else 100, virtual=True))
    rows += [heartbeat(page, 0, 120)]
    result = cover_report(tmp_path, page, rows)
    assert len(result["episodes"]) == 3
    assert result["omitted_episodes"] == sum(result["episode_counts"].values()) - 3
    assert sum(result["seconds_by_status"].values()) == 20


def test_cover_cross_hour_pages_preserve_original_timeout(tmp_path):
    page, rows = cover_seed()
    first = len(rows)
    rows += [heartbeat(page, 0, 100 + minute * 60) for minute in range(1, 62)]
    result = cover_report(tmp_path, page, rows, split=first + 60)
    assert result["seconds_by_status"]["sustained_divergence"] == 3650
    assert result["episode_counts"]["sustained_divergence"] == 1
    assert result["known_ratio"] == 1


def test_cover_moving_forever_is_not_an_unbounded_transient(tmp_path):
    page, rows = cover_seed()
    rows += [cover_input(rows, 103, 100, state="opening"), heartbeat(page, 0, 120)]
    result = cover_report(tmp_path, page, rows)
    assert result["seconds_by_status"]["sustained_divergence"] == 10
    assert "matched" not in result["seconds_by_status"]


@pytest.mark.parametrize("change", ["same", "target", "timeout", "removed"])
def test_cover_reload_configuration_changes_fail_comparison_closed(tmp_path, change):
    page, rows = cover_seed()
    second = deepcopy(rows)
    session = "00000000-0000-4000-8000-000000000002"
    for row in second:
        row["session_id"] = session
        row["at"] += 6
    second[0]["data"]["previous_session_closed"] = True
    config = second[0]["data"]["config"]
    if change == "target":
        replacement = "cover.shadow_" + "9" * 20
        next(iter(config["policies"].values()))["target_entity"] = replacement
        second[2]["data"]["entity_id"] = replacement
    elif change == "timeout":
        config["resources"][RESOURCE]["movement_timeout"] = 50
    elif change == "removed":
        config["resources"], config["policies"] = {}, {}
        second = [row for row in second if row["kind"] not in {"input", "decision"}]
        second[-1]["data"]["entities"] = 0
    ending = heartbeat(page, 0, 105)
    ending["kind"] = "session_end"
    final = heartbeat(page, 0, 111)
    final["session_id"] = final["data"]["session_id"] = session
    result = cover_report(tmp_path, page, rows + [ending] + second + [final])
    assert result["comparison_supported"] == (change == "same")
    assert result["not_compared_reason"] == (
        None
        if change == "same"
        else "resource_removed"
        if change == "removed"
        else "configuration_changed"
    )
    if change == "same":
        assert result["unknown_seconds"] == 1
        assert result["seconds_by_status"]["target_settling"] == 2
    else:
        assert result["seconds_by_status"]["not_compared"] == 5


def test_cover_resource_capacity_is_explicit(tmp_path, monkeypatch):
    page, rows = cover_seed()
    monkeypatch.setattr(archive, "MAX_COVERS", 0)
    with pytest.raises(archive.ArchiveError, match="Capacity exceeded: cover"):
        cover_report(tmp_path, page, rows)


async def test_cover_effective_target_from_actual_recorder_validated_export(tmp_path):
    _, rows = cover_seed()
    config = rows[0]["data"]["config"]
    config.pop("trace_entities")
    entry = SimpleNamespace(
        entry_id="private-cover",
        options={"trace_enabled": True},
        async_create_background_task=lambda _hass, coroutine, name: asyncio.create_task(
            coroutine, name=name
        ),
    )
    recorder = ShadowTrace(Host(tmp_path), entry, config, [])
    try:
        await recorder.async_start(
            {"manuals": {}, "modes": {}, "policy_enabled": {}, "occurrences": {}}, True
        )
        for row in rows:
            if row["kind"] == "input":
                data = row["data"]
                recorder.input(
                    data["entity_id"],
                    State(data["entity_id"], data["state"], data["attributes"]),
                    "initial",
                )
        recorder.record("snapshot_end", {"entities": 2})
        decision = deepcopy(rows[-1]["data"])
        decision["engine_result"]["decisions"][RESOURCE].update(
            target={"position": 25}, status="observe"
        )
        recorder.event("decision", decision)
        await recorder.async_close()
        exported = await recorder.async_export()
        normalized = validate_trace(exported)
        assert normalized.report["replay_complete"]
        canonical = recorder.sanitizer.ids[RESOURCE]
        frame = next(row for row in normalized.records if row["kind"] == "decision")
        assert canonical != frame["data"]["engine_aliases"][canonical]
        path = write_index(tmp_path, exported, [list(normalized.records)])
        result = archive.analyze_archive(path)["cover_comparisons"]["resources"][canonical]
        assert result["latest"]["requested_position"] == 100
        assert result["latest"]["observed_position"] == 0
        assert result["latest"]["effective"]["target"]["position"] == 25
    finally:
        await recorder.async_close()
