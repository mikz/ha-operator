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
                env["OPERATOR_PROFILE_TRACE"],
                env["OPERATOR_PROFILE_VARIANT"],
            )
        )
        Path(env["OPERATOR_PROFILE_OUTPUT"]).with_suffix(".json").write_text(
            json.dumps(
                {
                    "measurement": "timing",
                    "workload": env["OPERATOR_PROFILE_WORKLOAD"],
                    "trace_enabled": env["OPERATOR_PROFILE_TRACE"] == "1",
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
    assert len(calls) == 24
    for trace in ("0", "1"):
        for workload in ("followers_commands", "followers_reports"):
            assert [
                label for kind, enabled, label in calls if kind == workload and enabled == trace
            ] == [
                "baseline",
                "candidate",
                "candidate",
                "baseline",
                "baseline",
                "candidate",
            ]
    evidence = json.loads((output / "comparison.json").read_text())
    assert len(evidence["comparison"]) == 4
    for row in evidence["comparison"]:
        assert (
            len(row["baseline"]["counts_per_run"]) == len(row["candidate"]["counts_per_run"]) == 3
        )
        assert row["baseline"]["counts_per_run"] == row["candidate"]["counts_per_run"]
