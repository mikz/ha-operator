"""Streaming archive continuity stays distinct from recorder completeness guards."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from scripts import shadow_archive as archive
from tests.lab.shadow_trace import TraceValidationError, validate_trace
from tests.unit.test_shadow_report import BINDINGS, CELLAR
from tests.unit.test_shadow_trace import make_trace


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
