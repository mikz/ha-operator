"""Offline passive reports never infer motors, airflow or manual ownership."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import State

from custom_components.ha_operator.shadow import ShadowTrace, entity_alias
from scripts import shadow_report as report
from tests.lab.shadow_trace import NormalizedTrace
from tests.unit.test_shadow import Host

SESSION = "8b2f607f-ab12-4886-829f-378e992f1090"
SECOND_SESSION = "12b2683a-36ed-460c-92fd-9a3418877613"
START = 1_790_467_200.0
ROLE_DOMAINS = {
    "virtual_fan": "fan",
    "power": "switch",
    "inward": "switch",
    "outward": "switch",
    "manual_flag": "input_boolean",
    "manual_timer": "timer",
    "cellar_timer": "timer",
}
CELLAR = {
    role: f"{domain}.shadow_{index:020x}"
    for index, (role, domain) in enumerate(ROLE_DOMAINS.items())
}
BINDINGS = {"cellar": CELLAR}
RESOURCE = "r_" + "1" * 20
POLICY = "p_" + "2" * 20
RAW = "cover.shadow_" + "3" * 20
VIRTUAL = "cover.shadow_" + "4" * 20


def trace(records, *, complete=True, config=None, settling=None):
    return NormalizedTrace(
        tuple(records),
        config or {},
        {
            "replay_complete": complete,
            "incomplete_reasons": [] if complete else ["sequence_gap"],
            "health": {"complete": complete},
            "sessions": {SESSION: {"timezone": "Europe/Prague"}},
            "settling": {"minimum_seconds": 1.0, "entities": settling or {}},
        },
    )


def record(records, kind, at=START, data=None, *, session=SESSION):
    result = {
        "schema": 1,
        "sequence": len(records) + 1,
        "session_id": session,
        "at": at,
        "kind": kind,
        "data": data or {},
    }
    records.append(result)
    return result


def input_record(records, role, state, at=START, attributes=None):
    return record(
        records,
        "input",
        at,
        {
            "entity_id": CELLAR.get(role, role),
            "state": state,
            "attributes": attributes or {},
            "event_type": "initial" if at == START else "state_changed",
        },
    )


def seeded(*, requested="on", power="on", inward="on", outward="off", flag="on", timer="idle"):
    records = []
    record(records, "session_start", data={"config": {"resources": {}}, "intent": {"modes": {}}})
    input_record(
        records, "virtual_fan", requested, attributes={"direction": "reverse", "optimistic": True}
    )
    for role, state in {
        "power": power,
        "inward": inward,
        "outward": outward,
        "manual_flag": flag,
        "manual_timer": timer,
        "cellar_timer": "idle",
    }.items():
        input_record(records, role, state)
    record(records, "snapshot_end")
    return records


def test_39_hour_legacy_flag_with_idle_timers_is_ambiguity_not_motor_fault():
    records = seeded()
    for minute in range(1, 39 * 60 + 1):
        record(records, "heartbeat", START + minute * 60)
    result = report.build_report(trace(records), BINDINGS)
    episodes = result["cellar"]["episodes"]
    assert episodes["legacy_manual_flag_active_without_running_timer"]["seconds"] == 39 * 3600
    assert episodes["legacy_manual_flag_active_idle_timers_power_on"]["count"] == 1
    assert episodes["raw_power_feedback_on"]["seconds"] == 39 * 3600
    assert "requested_state_differs_from_power_feedback" not in episodes
    assert "both_direction_relays_on" not in episodes
    assert "ownership ambiguous" in " ".join(result["cellar"]["interpretation"])
    assert result["operator"]["manual_actor"] == "not_inferred_from_telemetry"
    assert result["operator"]["attempt_evidence"] == "zero_attempts_in_complete_captured_interval"
    assert len(result["utc_days"]) == 2


def test_availability_bursts_are_telemetry_and_never_disagreement():
    records = seeded(requested="off", power="off", inward="off", flag="off")
    for start, duration in ((10, 10.349), (40, 0.911)):
        input_record(records, "inward", "unavailable", START + start)
        input_record(records, "inward", "off", START + start + duration)
    for start, duration in ((60, 10.343), (90, 0.701)):
        input_record(records, "power", "unavailable", START + start)
        input_record(records, "power", "off", START + start + duration)
    record(records, "heartbeat", START + 100)
    episodes = report.build_report(trace(records), BINDINGS)["cellar"]["episodes"]
    assert episodes["unknown_inward"]["count"] == 2
    assert episodes["unknown_inward"]["seconds"] == pytest.approx(11.260)
    assert episodes["unknown_power"]["seconds"] == pytest.approx(11.044)
    assert "requested_state_differs_from_power_feedback" not in episodes
    assert "raw_power_feedback_on" not in episodes


def test_requested_vs_raw_direction_and_conflicts_are_separate():
    records = seeded(power="off")
    input_record(records, "power", "on", START + 5)
    input_record(records, "inward", "off", START + 10)
    input_record(records, "outward", "on", START + 10)
    input_record(records, "inward", "on", START + 15)
    record(records, "heartbeat", START + 20)
    result = report.build_report(trace(records), BINDINGS)
    episodes = result["cellar"]["episodes"]
    assert episodes["requested_state_differs_from_power_feedback"]["seconds"] == 5
    assert episodes["requested_direction_differs_from_relay_feedback"]["seconds"] == 5
    assert episodes["both_direction_relays_on"]["seconds"] == 5
    bindings = deepcopy(BINDINGS)
    bindings["cellar"]["inward_direction"] = "forward"
    swapped = report.build_report(trace(records), bindings)
    assert swapped["cellar"]["direction_mapping"]["outward"] == "reverse"
    assert (
        swapped["cellar"]["episodes"]["requested_direction_differs_from_relay_feedback"]["seconds"]
        == 5
    )


def test_missing_feedback_and_active_second_timer_never_count_as_idle():
    records = seeded()
    input_record(records, "manual_timer", "unavailable", START + 2)
    input_record(records, "cellar_timer", "active", START + 3)
    record(records, "heartbeat", START + 10)
    result = report.build_report(trace(records), BINDINGS)
    assert (
        result["cellar"]["episodes"]["legacy_manual_flag_active_without_running_timer"]["seconds"]
        == 2
    )
    assert result["cellar"]["episodes"]["unknown_manual_timer"]["seconds"] == 8
    empty = report.build_report(trace([record([], "heartbeat")]), BINDINGS)
    assert "legacy_manual_flag_active_without_running_timer" not in empty["cellar"]["episodes"]


def test_gaps_reset_states_and_never_claim_zero_attempts_for_missing_time():
    records = seeded()
    record(records, "heartbeat", START + 2)
    gap = record(records, "heartbeat", START + 100)
    gap["sequence"] += 1
    after = record(records, "heartbeat", START + 110)
    after["sequence"] += 1
    result = report.build_report(trace(records, complete=False), BINDINGS)
    assert result["cellar"]["episodes"]["raw_power_feedback_on"]["seconds"] == 2
    assert result["cellar"]["unattributed_gap_seconds"] == 98
    assert result["operator"]["attempt_evidence"] == "zero_recorded_attempts; coverage_incomplete"
    assert result["capture"]["quality"]["incomplete_reasons"] == ["sequence_gap"]
    record(records, "session_start", START + 120, session=SECOND_SESSION)
    record(records, "heartbeat", START + 130, session=SECOND_SESSION)
    result = report.build_report(trace(records, complete=False), BINDINGS)
    assert result["cellar"]["episodes"]["raw_power_feedback_on"]["seconds"] == 2


def test_per_input_changes_ignore_repeated_reports_and_context_actor():
    records = seeded()
    same = input_record(records, "power", "on", START + 2)
    same["data"].update(event_type="state_reported", event_context="context_id_" + "a" * 20)
    input_record(records, "power", "on", START + 3, {"restored": True})
    input_record(records, "power", "off", START + 4)
    result = report.build_report(trace(records), BINDINGS)
    power = result["inputs"][CELLAR["power"]]
    assert power == {
        "records": 4,
        "state_changes": 1,
        "attribute_changes": 2,
        "unknown_records": 1,
        "initial_snapshots": 1,
    }
    assert result["operator"]["manual_leases"] == "not_inferred_from_telemetry"


def test_actual_dispatch_records_and_explicit_mode_observations():
    records = []
    record(
        records,
        "session_start",
        data={"config": {"resources": {RESOURCE: {}}}, "intent": {"modes": {}}},
    )
    record(
        records,
        "admission",
        START + 1,
        {"action": "set_mode", "resource_id": RESOURCE, "mode": "live", "revision": 1},
    )
    record(records, "dispatch", START + 2, {"resource_id": RESOURCE, "status": "started"})
    record(records, "dispatch", START + 2, {"resource_id": RESOURCE, "status": "completed"})
    result = report.build_report(trace(records, complete=False))
    assert result["operator"]["dispatch_attempts"] == 1
    assert result["operator"]["attempt_evidence"] == "attempts_recorded"
    assert result["operator"]["last_modes"] == {RESOURCE: "live"}
    assert result["operator"]["mode_observations"] == {"observe": 1, "live": 1}
    assert result["cellar"] == {"configured": False}


@pytest.mark.parametrize(
    "actual,raw_settled,target_settled,status",
    [
        (20, True, True, "matched"),
        (0, True, True, "different"),
        (0, False, True, "unsettled"),
        (20, True, False, "unsettled"),
        (None, True, True, "unknown"),
        (False, True, True, "unknown"),
    ],
)
def test_roof_comparisons_use_validator_settling(actual, raw_settled, target_settled, status):
    records = []
    input_record(records, RAW, "open", attributes={"current_position": actual})
    config = {
        "resources": {RESOURCE: {"kind": "cover", "entity_id": RAW, "tolerance": 2}},
        "policies": {
            POLICY: {
                "resource_id": RESOURCE,
                "target_entity": VIRTUAL,
                "target_attribute": "current_position",
            }
        },
    }
    settled = {
        VIRTUAL: {
            "state": "open",
            "target_attributes": {"current_position": 20},
            "settled": target_settled,
        },
        RAW: {
            "state": "open",
            "target_attributes": {"current_position": actual},
            "settled": raw_settled,
        },
    }
    result = report.build_report(trace(records, config=config, settling=settled))
    assert result["roof_target_comparisons"][0]["comparison"] == status
    records[0]["data"]["attributes"]["optimistic"] = True
    assert (
        report.build_report(trace(records, config=config, settling=settled))[
            "roof_target_comparisons"
        ][0]["comparison"]
        == "unknown"
    )


@pytest.mark.parametrize(
    "bindings",
    [
        [],
        {"credentials": "hidden"},
        {"cellar": {"power": "switch.private_room"}},
        {"cellar": {"power": CELLAR["power"], "inward": CELLAR["power"]}},
        {"cellar": {"inward_direction": "private-secret"}},
        {"cellar": {"unknown": RAW}},
    ],
)
def test_bindings_reject_unsanitized_or_ambiguous_roles(bindings):
    with pytest.raises(report.ReportError) as error:
        report.validate_bindings(bindings)
    assert "private" not in str(error.value)


def test_episode_limit_keeps_count_and_duration():
    records = seeded(requested="off", power="off", flag="off")
    for tick in range(1, 411):
        input_record(records, "power", "on" if tick % 2 else "off", START + tick)
    result = report.build_report(trace(records), BINDINGS)
    episodes = result["cellar"]["episodes"]["raw_power_feedback_on"]
    assert episodes["count"] == 205 and episodes["seconds"] == 205
    assert len(episodes["intervals"]) == 200 and episodes["omitted_intervals"] == 5


def test_cli_never_overwrites_or_echoes_private_paths(tmp_path, capsys):
    output = tmp_path / "private-house.json"
    output.write_text("keep")
    source = trace([])
    with patch.object(report, "load_trace", return_value=source):
        with pytest.raises(SystemExit) as error:
            report.main([str(tmp_path / "trace.json"), "--output", str(output)])
    assert error.value.code == 2 and output.read_text() == "keep"
    assert "private-house" not in capsys.readouterr().err
    with pytest.raises(SystemExit):
        report.main(["https://example.com/private.json"])
    assert "private" not in capsys.readouterr().err


def test_cli_new_file_and_bindings_limits(tmp_path, capsys):
    binding = tmp_path / "bindings.json"
    binding.write_text(json.dumps(BINDINGS))
    output = tmp_path / "report.json"
    with patch.object(report, "load_trace", return_value=trace([])):
        report.main(["local.json", "--bindings", str(binding), "--output", str(output)])
        assert json.loads(output.read_text())["cellar"]["configured"]
        report.main(["local.json"])
        assert json.loads(capsys.readouterr().out)["capture"]["records"] == 0
        binding.write_bytes(b" " * (report.BINDINGS_BYTES + 1))
        with pytest.raises(SystemExit):
            report.main(["local.json", "--bindings", str(binding)])
        assert "size limit" in capsys.readouterr().err


def test_shadow_lock_overrides_saved_live_mode_and_session_tail_is_explicit():
    records = []
    record(
        records,
        "session_start",
        data={
            "config": {"resources": {RESOURCE: {}}},
            "intent": {"modes": {RESOURCE: "live"}},
            "shadow_lock": True,
        },
    )
    record(records, "heartbeat", START + 1)
    result = report.build_report(trace(records))
    assert result["operator"]["last_modes"] == {RESOURCE: "observe"}
    assert result["capture"]["session_tails"] == {SESSION: "open_at_export"}
    record(records, "session_start", START + 2, session=SECOND_SESSION)
    record(records, "session_end", START + 3, session=SECOND_SESSION)
    tails = report.build_report(trace(records, complete=False))["capture"]["session_tails"]
    assert tails == {SESSION: "unclean", SECOND_SESSION: "closed"}


def test_zero_duration_conflicting_bits_remain_observed_without_duration_claim():
    records = seeded()
    input_record(records, "outward", "on", START + 1)
    input_record(records, "outward", "off", START + 1)
    result = report.build_report(trace(records), BINDINGS)["cellar"]
    assert result["condition_observations"]["both_direction_relays_on"] == 1
    assert "both_direction_relays_on" not in result["episodes"]


async def test_cli_reads_real_recorder_export_without_private_names(tmp_path):
    entry = SimpleNamespace(
        entry_id="local-only",
        options={"trace_enabled": True},
        async_create_background_task=lambda _hass, coroutine, name: asyncio.create_task(
            coroutine, name=name
        ),
    )
    recorder = ShadowTrace(
        Host(tmp_path),
        entry,
        {"resources": {}, "policies": {}, "requirements": {}},
        ["fan.private_cellar", "switch.private_power"],
    )
    try:
        await recorder.async_start(
            {"manuals": {}, "modes": {}, "policy_enabled": {}, "occurrences": {}}, True
        )
        recorder.input(
            "fan.private_cellar",
            State(
                "fan.private_cellar",
                "on",
                {
                    "direction": "reverse",
                    "percentage": 67,
                    "optimistic": True,
                },
            ),
            "initial",
        )
        recorder.input("switch.private_power", State("switch.private_power", "off"), "initial")
        recorder.record("snapshot_end", {"entities": 2})
        await recorder.async_close()
        exported = await recorder.async_export()
        source = tmp_path / "trace.json"
        source.write_text(json.dumps({"schema": 1, "pages": [exported]}))
        bindings = tmp_path / "roles.json"
        bindings.write_text(
            json.dumps(
                {
                    "cellar": {
                        "virtual_fan": entity_alias("fan.private_cellar"),
                        "power": entity_alias("switch.private_power"),
                    }
                }
            )
        )
        output = tmp_path / "report.json"
        report.main([str(source), "--bindings", str(bindings), "--output", str(output)])
        result = json.loads(output.read_text())
        assert not result["capture"]["replay_complete"]
        assert "engine_decisions_missing" in result["capture"]["quality"]["incomplete_reasons"]
        assert result["capture"]["session_tails"] == {recorder.session_id: "closed"}
        assert result["operator"]["dispatch_attempts"] == 0
        assert (
            result["cellar"]["condition_observations"][
                "requested_state_differs_from_power_feedback"
            ]
            >= 1
        )
        assert "private_cellar" not in output.read_text()
        assert "private_power" not in output.read_text()
        assert str(tmp_path) not in output.read_text()
    finally:
        await recorder.async_close()


def test_cli_validated_complete_export_reports_bounded_zero_attempt_evidence(tmp_path):
    from tests.unit.test_shadow_trace import make_trace

    source = tmp_path / "trace.json"
    source.write_text(json.dumps(make_trace()))
    output = tmp_path / "report.json"
    report.main([str(source), "--output", str(output)])
    result = json.loads(output.read_text())
    assert result["capture"]["replay_complete"]
    assert result["operator"]["attempt_evidence"] == "zero_attempts_in_complete_captured_interval"
    assert result["capture"]["start_utc"] == "1970-01-01T00:01:40Z"
    assert result["capture"]["end_utc"] == "1970-01-01T00:01:42Z"
    assert result["capture"]["quality"]["sessions"]
