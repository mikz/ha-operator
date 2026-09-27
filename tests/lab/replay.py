"""Replay recorded pure-engine snapshots from the exact packaged integration.

This does not reconstruct physical causality from telemetry. Each frame supplies
the recorded engine inputs and recorded time; raw feedback has separate settling
analysis. Only independent simulator journals can establish simulated effects.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .shadow_trace import load_trace


def primitive(value: Any) -> Any:
    """Serialize immutable engine records without deepcopying MappingProxyType."""
    if dataclasses.is_dataclass(value):
        return {
            field.name: primitive(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {key: primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive(item) for item in value]
    return value


def evaluate_snapshot(core: Any, snapshot: dict) -> dict:
    """Rebuild only known dataclasses, never import anything named by the input."""

    def target(value):
        return core.Target(**value) if value is not None else None

    def record(kind, value):
        fields = dict(value)
        if "target" in fields:
            fields["target"] = target(fields["target"])
        return kind(**fields)

    def memory(value):
        return core.RequirementMemory(
            **{
                **value,
                "failed_until": tuple(tuple(item) for item in value.get("failed_until", [])),
            }
        )

    requirements = []
    for value in snapshot.get("requirements", []):
        requirements.append(
            core.Requirement(
                **{
                    **value,
                    "providers": tuple(record(core.Provider, item) for item in value["providers"]),
                }
            )
        )
    result = core.evaluate(
        now=snapshot["now"],
        resources={key: core.Resource(**value) for key, value in snapshot["resources"].items()},
        observations={
            key: record(core.Observation, value) for key, value in snapshot["observations"].items()
        },
        manuals=[record(core.ManualLease, value) for value in snapshot.get("manuals", [])],
        policies=[record(core.Policy, value) for value in snapshot.get("policies", [])],
        occurrences=[record(core.Occurrence, value) for value in snapshot.get("occurrences", [])],
        requirements=requirements,
        requirement_memory={
            key: memory(value) for key, value in snapshot.get("requirement_memory", {}).items()
        },
    )
    return primitive(result)


def replay_trace(trace_path: Path, archive: Path, expected_sha256: str) -> dict:
    """Validate export, load the trusted release's core, compare all recorded frames."""
    trace = load_trace(trace_path)
    archive_bytes = archive.read_bytes()
    actual_digest = hashlib.sha256(archive_bytes).hexdigest()
    if actual_digest != expected_sha256:
        raise ValueError("Replay archive does not match prepared release SHA256")
    comparisons = []
    missing = []
    with tempfile.TemporaryDirectory(prefix="ha-operator-replay-") as directory:
        with zipfile.ZipFile(archive) as package:
            component_files = {
                name: hashlib.sha256(package.read(name)).hexdigest()
                for name in package.namelist()
                if Path(name).suffix in {".py", ".json", ".yaml"}
            }
            component_digest = hashlib.sha256(
                json.dumps(component_files, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if trace.report["component_sha256"] != component_digest:
                raise ValueError("Trace component fingerprint differs from replay package")
            # Fixed file only: no archive extraction paths or trace-supplied code.
            source = package.read("core.py")
        path = Path(directory) / "core.py"
        path.write_bytes(source)
        name = "_ha_operator_packaged_replay_core"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError("Cannot load packaged pure engine")
        core = importlib.util.module_from_spec(spec)
        sys.modules[name] = core
        try:
            spec.loader.exec_module(core)
            for frame in trace.records:
                if frame["kind"] != "decision":
                    continue
                data = frame["data"]
                if "engine" not in data or "engine_result" not in data:
                    missing.append(frame["sequence"])
                    continue
                actual = evaluate_snapshot(core, data["engine"])
                matches = actual == data["engine_result"]
                comparison = {"sequence": frame["sequence"], "matched": matches}
                if not matches:
                    comparison.update(expected=data["engine_result"], actual=actual)
                comparisons.append(comparison)
        finally:
            sys.modules.pop(name, None)
    complete = bool(trace.report["replay_complete"] and comparisons and not missing)
    matched = all(frame["matched"] for frame in comparisons)
    gaps = list(trace.report.get("reasons", []))
    if missing:
        gaps.append({"missing_engine_snapshot_sequences": missing})
    if not comparisons:
        gaps.append("No replayable decision snapshots")
    return {
        "schema": 1,
        "status": "passed" if complete and matched else "failed" if not matched else "incomplete",
        "trace_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        "artifact_sha256": actual_digest,
        "component_sha256": component_digest,
        "config_hash": trace.report["config_hash"],
        "complete": complete,
        "gaps": gaps,
        "physical_effects": "not_observed",
        "provenance": "recorded_engine_snapshot_replay",
        "clock": "recorded_epoch_per_snapshot",
        "feedback_analysis": trace.report,
        "comparisons": comparisons,
        "limits": [
            "Recorder supplies engine snapshots; inputs are not independently reconstructed.",
            "Recorded feedback does not prove actuator effects or identify manual intent.",
            "No accelerated native timers, HomeKit delivery, or historical causality are claimed.",
        ],
    }


def write_replay(trace_path: Path, archive: Path, expected_sha256: str, destination: Path) -> dict:
    report = replay_trace(trace_path, archive, expected_sha256)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report
