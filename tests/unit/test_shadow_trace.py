"""Offline sanitized-trace safety, provenance, pagination, and settling tests."""

import hashlib
import json
from copy import deepcopy

import pytest

from tests.lab.shadow_trace import TraceValidationError, load_trace, validate_trace

ENTITY = "cover.shadow_" + "1" * 20
RESOURCE = "r_" + "2" * 20
SESSION = "00000000-0000-4000-8000-000000000001"


def make_trace(*, component_sha256="a" * 64):
    """A complete sanitized export with one exact pure-engine idle decision."""
    config = {
        "resources": {RESOURCE: {"name": RESOURCE, "kind": "cover", "entity_id": ENTITY}},
        "policies": {},
        "requirements": {},
        "trace_entities": [],
    }
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    health = {
        "enabled": True,
        "healthy": True,
        "session_id": SESSION,
        "last_sequence": 4,
        "durable_sequence": 4,
        "queued_records": 0,
        "dropped_records": 0,
        "write_errors": 0,
        "rotations": 0,
        "last_heartbeat": None,
        "last_write_at": 102.0,
        "complete": True,
        "queue_limit": 512,
        "record_bytes_limit": 65536,
        "disk_bytes_limit": 64 * 1024 * 1024,
        "unclean_previous": False,
        "history_gap": False,
    }
    engine = {
        "now": 102.0,
        "resources": {
            RESOURCE: {
                "id": RESOURCE,
                "kind": "cover",
                "mode": "observe",
                "tolerance": 2,
                "fault": None,
                "observation_after": None,
            }
        },
        "observations": {
            RESOURCE: {
                "target": {
                    "position": 0,
                    "on": None,
                    "percentage": None,
                    "direction": None,
                    "profile": None,
                },
                "available": True,
                "moving": False,
                "restriction": None,
                "reported_at": 100.0,
            }
        },
        "manuals": [],
        "policies": [],
        "occurrences": [],
        "requirements": [],
        "requirement_memory": {},
    }
    result = {
        "decisions": {
            RESOURCE: {
                "resource_id": RESOURCE,
                "target": None,
                "status": "idle",
                "source": None,
                "reason": "no eligible request",
                "candidates": [],
            }
        },
        "requirements": {},
        "next_evaluation": None,
    }
    data = [
        (
            "session_start",
            100.0,
            {
                "config": config,
                "intent": {"manuals": {}, "occurrences": [], "modes": {}, "policy_enabled": {}},
                "timezone": "Europe/Prague",
                "ha_version": "2026.9.3",
                "integration_version": "0.1.0",
                "config_hash": config_hash,
                "component_sha256": component_sha256,
                "shadow_lock": True,
                "previous_session_closed": None,
                "gap_since_previous": None,
            },
        ),
        (
            "input",
            100.0,
            {
                "entity_id": ENTITY,
                "event_type": "initial",
                "state": "closed",
                "attributes": {"current_position": 0, "supported_features": 15},
                "last_changed": 100.0,
                "last_updated": 100.0,
                "last_reported": 100.0,
                "old_last_reported": None,
                "event_context": None,
                "event_parent_context": None,
            },
        ),
        ("snapshot_end", 100.0, {"entities": 1}),
        (
            "decision",
            102.0,
            {
                "revision": 0,
                "fault": None,
                "resources": {},
                "requirements": {},
                "shadow_locked": True,
                "engine": engine,
                "engine_result": result,
                "engine_aliases": {RESOURCE: RESOURCE},
            },
        ),
    ]
    return {
        "schema": 1,
        "integration_version": "0.1.0",
        "config_hash": config_hash,
        "component_sha256": component_sha256,
        "through_sequence": 4,
        "records": [
            {
                "schema": 1,
                "sequence": index,
                "session_id": SESSION,
                "at": at,
                "kind": kind,
                "data": item,
            }
            for index, (kind, at, item) in enumerate(data, 1)
        ],
        "next_after": 4,
        "more": False,
        "gap": False,
        "health": health,
    }


def append_record(trace, kind, data, *, at=104.0):
    sequence = trace["through_sequence"] + 1
    trace["records"].append(
        {
            "schema": 1,
            "sequence": sequence,
            "session_id": SESSION,
            "at": at,
            "kind": kind,
            "data": data,
        }
    )
    trace["next_after"] = trace["through_sequence"] = sequence
    trace["health"]["last_sequence"] = trace["health"]["durable_sequence"] = sequence


def test_complete_trace_preserves_provenance_and_owns_data():
    payload = make_trace()
    trace = validate_trace(payload)
    assert trace.report["replay_complete"] is True
    assert trace.report["reasons"] == []
    assert trace.report["source"] == "recorded_feedback"
    assert trace.report["physical_effects"] == "not_observed"
    assert trace.report["conformance"] == "not_evaluated"
    assert trace.report["synthetic_scenarios"] == []
    assert trace.report["component_sha256"] == "a" * 64
    payload["records"][0]["data"]["config"]["resources"].clear()
    assert RESOURCE in trace.config["resources"]


def test_pages_assemble_with_advancing_upper_bound():
    trace = make_trace()
    first, second = deepcopy(trace), deepcopy(trace)
    first.update(records=first["records"][:2], next_after=2, through_sequence=3, more=True)
    second["records"] = second["records"][2:]
    result = validate_trace({"schema": 1, "pages": [first, second]})
    assert len(result.records) == 4
    assert result.report["replay_complete"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 2),
        ("records", {}),
        ("more", 0),
        ("component_sha256", None),
        ("through_sequence", 5),
    ],
)
def test_rejects_malformed_export(field, value):
    trace = make_trace()
    trace[field] = value
    with pytest.raises(TraceValidationError):
        validate_trace(trace)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "future_event"),
        ("sequence", True),
        ("at", float("nan")),
        ("at", float("inf")),
        ("at", 10**1000),
        ("session_id", "home-server"),
    ],
)
def test_rejects_malformed_record(field, value):
    trace = make_trace()
    trace["records"][1][field] = value
    with pytest.raises(TraceValidationError):
        validate_trace(trace)


@pytest.mark.parametrize(
    "field,value",
    [
        ("entity_id", "cover.bedroom"),
        ("entity_id", "cover.shadow_abc"),
        ("state", "http://home.local"),
        ("state", "/config/secrets.yaml"),
        ("state", "Bearer abc.def.ghi"),
        ("access_token", "value_" + "a" * 20),
        ("unexpected", None),
    ],
)
def test_rejects_private_or_unknown_input_fields(field, value):
    trace = make_trace()
    trace["records"][1]["data"][field] = value
    with pytest.raises(TraceValidationError):
        validate_trace(trace)


def test_rejects_unknown_nested_engine_fields_and_numeric_dictionary_keys():
    trace = make_trace()
    trace["records"][-1]["data"]["engine"]["resources"][RESOURCE]["last_write_at"] = 3
    with pytest.raises(TraceValidationError):
        validate_trace(trace)
    trace = make_trace()
    trace["records"][1]["data"]["attributes"][1] = 2
    with pytest.raises(TraceValidationError):
        validate_trace(trace)


def test_rejects_hash_mismatch_and_component_session_mismatch():
    trace = make_trace()
    trace["records"][0]["data"]["config_hash"] = "b" * 64
    with pytest.raises(TraceValidationError, match="Configuration hash"):
        validate_trace(trace)
    trace = make_trace()
    trace["records"][0]["data"]["component_sha256"] = "b" * 64
    with pytest.raises(TraceValidationError, match="component fingerprint"):
        validate_trace(trace)


def test_duplicate_keys_and_nonfinite_json_are_rejected(tmp_path):
    path = tmp_path / "trace.json"
    for text in ('{"schema":1,"schema":1}', '{"schema":NaN}', '{"schema":Infinity}'):
        path.write_text(text)
        with pytest.raises(TraceValidationError):
            load_trace(path)


def test_resource_limits_are_enforced(monkeypatch, tmp_path):
    from tests.lab import shadow_trace

    path = tmp_path / "trace.json"
    path.write_text(json.dumps(make_trace()))
    monkeypatch.setattr(shadow_trace, "MAX_BYTES", 10)
    with pytest.raises(TraceValidationError, match="byte limit"):
        load_trace(path)
    monkeypatch.setattr(shadow_trace, "MAX_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(shadow_trace, "MAX_RECORDS", 3)
    with pytest.raises(TraceValidationError, match="record limit"):
        validate_trace(make_trace())


@pytest.mark.parametrize(
    "cause,reason",
    [
        ("gap", "declared_gap"),
        ("more", "pagination_incomplete"),
        ("snapshot", "engine_snapshot_missing"),
        ("prefix", "prefix_missing"),
        ("tail", "tail_missing"),
        ("initial", "initial_inputs_missing"),
    ],
)
def test_missing_evidence_is_valid_but_never_complete(cause, reason):
    trace = make_trace()
    if cause in {"gap", "more"}:
        trace[cause] = True
    elif cause == "snapshot":
        del trace["records"][-1]["data"]["engine"]
    elif cause == "prefix":
        trace["records"] = trace["records"][1:]
    elif cause == "initial":
        trace["records"][2]["data"]["entities"] = 2
    else:
        trace["through_sequence"] = 5
        trace["health"]["last_sequence"] = trace["health"]["durable_sequence"] = 5
    result = validate_trace(trace)
    assert result.report["replay_complete"] is False
    assert reason in result.report["reasons"]


def test_settling_requires_full_second_of_state_and_target_stability():
    trace = make_trace()
    trace["records"][-1]["at"] = 100.999
    assert validate_trace(trace).report["settling"]["entities"][ENTITY]["settled"] is False
    trace["records"][-1]["at"] = 101.0
    assert validate_trace(trace).report["settling"]["entities"][ENTITY]["settled"] is True
    update = deepcopy(trace["records"][1]["data"])
    update.update(event_type="state_changed", attributes={"current_position": 20})
    append_record(trace, "input", update, at=101.5)
    item = validate_trace(trace).report["settling"]["entities"][ENTITY]
    assert item["stable_since"] == 101.5
    assert item["settled"] is False


def test_animated_cover_is_never_manual_intent_or_settled():
    trace = make_trace()
    trace["records"][1]["data"]["state"] = "opening"
    result = validate_trace(trace).report["settling"]["entities"][ENTITY]
    assert result["moving"] is True
    assert result["settled"] is False
    assert result["manual_intent"] == "not_inferred"


def test_missing_sequence_or_clock_regression_invalidates_settling_interval():
    for change in ("gap", "clock"):
        trace = make_trace()
        if change == "gap":
            trace["records"].pop(2)
        else:
            trace["records"][-1]["at"] = 99.0
        result = validate_trace(trace)
        assert result.report["replay_complete"] is False
        assert result.report["settling"]["entities"] == {}


def test_input_metadata_is_exposed_without_changing_target_stability():
    trace = make_trace()
    update = deepcopy(trace["records"][1]["data"])
    update["event_type"] = "state_reported"
    update["attributes"]["optimistic"] = True
    append_record(trace, "input", update, at=103.0)
    result = validate_trace(trace).report["settling"]["entities"][ENTITY]
    assert result["stable_since"] == 100.0
    assert result["metadata_flags"]["optimistic"] is True


def test_timer_allowlist_and_dispatch_external_receipts(tmp_path):
    trace = make_trace()
    timer = deepcopy(trace["records"][1]["data"])
    timer.update(
        entity_id="timer.shadow_" + "3" * 20,
        event_type="state_changed",
        state="active",
        attributes={"duration": "1:00:00", "finishes_at": "2026-09-27T11:00:00+00:00"},
    )
    append_record(trace, "input", timer)
    context = "context_id_" + "4" * 20
    append_record(
        trace,
        "dispatch",
        {
            "resource_id": RESOURCE,
            "domain": "cover",
            "service": "set_cover_position",
            "data": {"entity_id": ENTITY, "position": 40},
            "context_id": context,
            "at": 104.0,
            "status": "started",
        },
    )
    append_record(
        trace,
        "external_command",
        {
            "source": "homekit",
            "entity_id": ENTITY,
            "service": "set_cover_position",
            "value": 40,
            "context_id": context,
            "evidence": "bridge_request",
            "at": 104.0,
        },
    )
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace))
    assert load_trace(path).report["replay_complete"] is True


@pytest.mark.parametrize(
    "field", ["write_errors", "dropped_records", "rotations", "history_gap", "unclean_previous"]
)
def test_recorder_health_loss_prevents_complete_replay(field):
    payload = make_trace()
    payload["health"][field] = True if field in {"history_gap", "unclean_previous"} else 1
    with pytest.raises(TraceValidationError, match="complete health"):
        validate_trace(payload)
    payload["health"]["complete"] = False
    result = validate_trace(payload)
    assert result.report["replay_complete"] is False
    assert field in result.report["reasons"]


def test_duplicate_sequences_and_bad_page_cursors_are_rejected():
    payload = make_trace()
    payload["records"][2]["sequence"] = 2
    with pytest.raises(TraceValidationError, match="Duplicate or reversed"):
        validate_trace(payload)
    payload = make_trace()
    payload["next_after"] = 3
    with pytest.raises(TraceValidationError, match="pagination cursor"):
        validate_trace(payload)


def test_opaque_local_policy_ids_are_scoped_to_engine_frames():
    payload = make_trace()
    decision = payload["records"][-1]["data"]["engine_result"]["decisions"][RESOURCE]
    decision["source"] = "policy:r_" + "5" * 20
    assert validate_trace(payload).report["replay_complete"] is True
    payload["records"][-1]["data"]["resources"] = {
        RESOURCE: {
            "mode": "observe",
            "decision": decision,
            "observation": None,
            "manual": None,
            "last_command": None,
            "next_attempt": None,
            "attempts": 0,
        }
    }
    with pytest.raises(TraceValidationError, match="source alias"):
        validate_trace(payload)


def test_nested_private_attribute_and_missing_engine_fields_are_rejected():
    payload = make_trace()
    payload["records"][1]["data"]["attributes"]["attribute_" + "1" * 20] = {"state": "on"}
    with pytest.raises(TraceValidationError, match="Nested input"):
        validate_trace(payload)
    payload = make_trace()
    del payload["records"][-1]["data"]["engine"]["observations"][RESOURCE]["available"]
    with pytest.raises(TraceValidationError, match="Missing trace fields"):
        validate_trace(payload)


def test_no_decision_is_explicitly_incomplete():
    payload = make_trace()
    payload["records"].pop()
    payload["next_after"] = payload["through_sequence"] = 3
    payload["health"]["last_sequence"] = payload["health"]["durable_sequence"] = 3
    result = validate_trace(payload)
    assert result.report["replay_complete"] is False
    assert "engine_decisions_missing" in result.report["reasons"]
