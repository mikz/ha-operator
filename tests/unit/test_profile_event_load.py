"""Comparative probes must execute identical workloads for both release archives."""

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from scripts.profile_event_load import run


def test_followers_compare_both_archives_with_three_alternating_repetitions(tmp_path, monkeypatch):
    packages = []
    for label in ("baseline", "candidate"):
        package = tmp_path / f"{label}.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("__init__.py", "ready = True\n")
        packages.append(package)
    calls = []

    def execute(command, *, env, cwd, **kwargs):
        calls.append(
            (
                env["OPERATOR_PROFILE_WORKLOAD"],
                env["OPERATOR_PROFILE_VARIANT"],
            )
        )
        Path(env["OPERATOR_PROFILE_OUTPUT"]).with_suffix(".json").write_text(
            json.dumps(
                {
                    "measurement": "timing",
                    "workload": env["OPERATOR_PROFILE_WORKLOAD"],
                    "cpu_seconds": 1,
                    "wall_seconds": 1,
                    "median_batch_ms": 1,
                    "p95_batch_ms": 1,
                    "counts": {"commands": 3},
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("scripts.profile_event_load.subprocess.run", execute)
    output = tmp_path / "comparison"
    run(
        SimpleNamespace(
            output=output,
            suite="events",
            workloads=["followers_commands", "followers_reports"],
            measurements=["timing"],
            repeats=3,
            baseline=packages[0],
            candidate=packages[1],
            batches=3,
            warmup_batches=10,
        )
    )
    assert len(calls) == 12
    for workload in ("followers_commands", "followers_reports"):
        assert [label for kind, label in calls if kind == workload] == [
            "baseline",
            "candidate",
            "candidate",
            "baseline",
            "baseline",
            "candidate",
        ]
    evidence = json.loads((output / "comparison.json").read_text())
    assert len(evidence["comparison"]) == 2
    assert evidence["baseline_operator_trace"] == "package_default"
    for row in evidence["comparison"]:
        assert (
            len(row["baseline"]["counts_per_run"]) == len(row["candidate"]["counts_per_run"]) == 3
        )
        assert row["baseline"]["counts_per_run"] == row["candidate"]["counts_per_run"]


def test_new_native_probe_is_candidate_only_without_comparative_speed_claim(tmp_path, monkeypatch):
    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("__init__.py", "ready = True\n")
    calls = []

    def execute(command, *, env, cwd, **kwargs):
        calls.append(env["OPERATOR_PROFILE_VARIANT"])
        assert env["OPERATOR_PROFILE_WORKLOAD"] == "native-conditions"
        assert "NativeConditionList" in (Path(cwd) / "test_event_profile.py").read_text()
        Path(env["OPERATOR_PROFILE_OUTPUT"]).with_suffix(".json").write_text(
            json.dumps(
                {"cpu_seconds": 1, "wall_seconds": 1, "counts": {"condition_evaluations": 20}}
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("scripts.profile_event_load.subprocess.run", execute)
    destination = tmp_path / "probe"
    run(
        SimpleNamespace(
            output=destination,
            suite="native-conditions",
            workloads=None,
            measurements=["timing"],
            repeats=2,
            baseline=None,
            candidate=package,
            batches=3,
            warmup_batches=1,
        )
    )
    evidence = json.loads((destination / "comparison.json").read_text())
    assert calls == ["candidate", "candidate"]
    assert evidence["baseline_sha256"] is None
    assert evidence["comparison"] == []
    assert evidence["comparison_scope"] == "candidate-only native-policy probe"


def test_legacy_detector_probe_refuses_incompatible_native_candidate(tmp_path):
    import pytest

    package = tmp_path / "new.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"version": "0.4.0"}))
    with pytest.raises(ValueError, match="not comparable"):
        run(
            SimpleNamespace(
                output=tmp_path / "result",
                suite="native-policies",
                baseline=package,
                candidate=package,
            )
        )
