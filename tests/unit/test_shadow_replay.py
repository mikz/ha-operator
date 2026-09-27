"""Replay verifies trusted artifact bytes and every deterministic engine result field."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from tests.lab.replay import replay_trace
from tests.unit.test_shadow_trace import RESOURCE, make_trace

CORE = Path(__file__).resolve().parents[2] / "custom_components/ha_operator/core.py"


@pytest.fixture
def replay_files(tmp_path):
    source = CORE.read_bytes()
    component_digest = hashlib.sha256(
        json.dumps(
            {"core.py": hashlib.sha256(source).hexdigest()}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    archive = tmp_path / "integration.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("core.py", source)
    payload = make_trace(component_sha256=component_digest)
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps(payload))
    return trace, archive, hashlib.sha256(archive.read_bytes()).hexdigest(), payload


def test_replay_uses_recorded_clock_and_carries_independent_provenance(replay_files):
    trace, archive, digest, _ = replay_files
    report = replay_trace(trace, archive, digest)
    assert report["status"] == "passed" and report["complete"]
    assert report["comparisons"] == [{"sequence": 4, "matched": True}]
    assert report["clock"] == "recorded_epoch_per_snapshot"
    assert report["provenance"] == "recorded_engine_snapshot_replay"
    assert report["physical_effects"] == "not_observed"
    assert report["artifact_sha256"] == digest
    assert report["trace_sha256"] == hashlib.sha256(trace.read_bytes()).hexdigest()


def test_replay_does_not_call_changed_decision_conformant(replay_files):
    trace, archive, digest, payload = replay_files
    payload["records"][-1]["data"]["engine_result"]["decisions"][RESOURCE]["status"] = "observe"
    trace.write_text(json.dumps(payload))
    report = replay_trace(trace, archive, digest)
    assert report["status"] == "failed"
    result = report["comparisons"][0]
    assert result["matched"] is False
    assert result["expected"]["decisions"][RESOURCE]["status"] == "observe"
    assert result["actual"]["decisions"][RESOURCE]["status"] == "idle"


def test_replay_rejects_different_release_before_import(replay_files):
    trace, archive, _, _ = replay_files
    with pytest.raises(ValueError, match="prepared release SHA256"):
        replay_trace(trace, archive, "0" * 64)


def test_replay_rejects_source_component_mismatch(replay_files):
    trace, archive, digest, payload = replay_files
    payload["component_sha256"] = "0" * 64
    payload["records"][0]["data"]["component_sha256"] = "0" * 64
    trace.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="component fingerprint"):
        replay_trace(trace, archive, digest)


def test_replay_does_not_turn_retention_loss_into_a_pass(replay_files):
    trace, archive, digest, payload = replay_files
    payload["health"].update(history_gap=True, complete=False)
    trace.write_text(json.dumps(payload))
    report = replay_trace(trace, archive, digest)
    assert report["comparisons"][0]["matched"]
    assert report["complete"] is False
    assert report["status"] == "incomplete"
    assert report["gaps"]


def test_replay_requires_recorded_decision_frames(replay_files):
    trace, archive, digest, payload = replay_files
    payload["records"].pop()
    payload["next_after"] = payload["through_sequence"] = 3
    payload["health"]["last_sequence"] = payload["health"]["durable_sequence"] = 3
    trace.write_text(json.dumps(payload))
    report = replay_trace(trace, archive, digest)
    assert report["status"] == "incomplete" and not report["comparisons"]
    assert "No replayable decision snapshots" in report["gaps"]
