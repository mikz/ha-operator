"""Release evidence refuses incomplete, stale, or mixed-artifact gates."""

import json
import zipfile
from pathlib import Path

import pytest

from scripts.evidence import (
    ALL_SCENARIOS,
    CELLAR_SCENARIOS,
    INITIAL_SCENARIOS,
    LAB_CASES,
    MUTANTS,
    SHADOW_FILES,
    SHADOW_SCENARIOS,
    TEST_FILES,
    build_evidence,
    validate_lab,
)
from scripts.release import archive_bytes, build, digest


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def complete_evidence(tmp_path):
    root = tmp_path / "repository"
    source = root / "custom_components/ha_operator"
    (source / "brand").mkdir(parents=True)
    (source / "__init__.py").write_text("ready = True\n")
    (source / "brand/icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    write_json(source / "manifest.json", {"domain": "ha_operator", "version": "0.1.0"})
    (root / "uv.lock").write_text("locked")
    manifest = build(root, root / "dist")
    manifest["source_commit"] = "a" * 40
    write_json(root / "dist/ha_operator.manifest.json", manifest)
    sources = {
        f"custom_components/ha_operator/{name}": data["sha256"]
        for name, data in manifest["files"].items()
    }
    labs = []
    for version, scenario in sorted(LAB_CASES):
        run = root / "artifacts/lab" / f"{version}-{scenario}"
        run.mkdir(parents=True)
        write_json(
            run / "summary.json",
            {
                "ha_version": version,
                "scenario": scenario,
                "run_id": run.name,
                "status": "passed",
                "isolation": "passed",
                "cleanup": "passed",
                "completed_at": 600,
                "artifact_sha256": manifest["sha256"],
            },
        )
        write_json(run / "sanitized.json", {"run_id": run.name, "completed_at": 601})
        write_json(
            run / "cleanup-verification.json",
            {
                "run_id": run.name,
                "status": "passed",
                "completed_at": 601,
                "containers": [],
                "networks": [],
                "volumes": [],
            },
        )
        write_json(
            run / "prepared.json",
            {
                "ha_version": version,
                "artifact_sha256": manifest["sha256"],
                "uv_lock_sha256": manifest["locks"]["uv.lock"],
                "images": dict.fromkeys(("ha", "runner", "simulator"), "sha256:" + "0" * 64),
            },
        )
        names = sorted(
            ALL_SCENARIOS if scenario == "all" else INITIAL_SCENARIOS | {"LAB-REALISTIC-SOAK"}
        )
        write_json(
            run / "scenarios.json",
            [
                {"id": name, "status": "passed", "started_at": 0, "completed_at": 600}
                for name in names
            ],
        )
        for name in (
            "compose.json",
            "inspect.json",
            "routes-ha.json",
            "routes-runner.json",
            "routes-simulator.json",
            "simulator-journal.json",
            "hap-accessories.json",
            "downloaded-diagnostics.json",
            *sorted(SHADOW_FILES),
        ):
            write_json(run / name, {})
        for name in ("hap-transcript.jsonl", "crash-events.jsonl", "ha.log"):
            (run / name).write_text("Bearer accidental-test-token\n")
        write_json(
            run / "shadow-trace.json",
            {
                "schema": 1,
                "pages": [{
                    "schema": 1,
                    "integration_version": manifest["version"],
                    "component_sha256": manifest["component_sha256"],
                    "config_hash": "a" * 64,
                    "gap": False,
                    "more": False,
                    "health": {"enabled": True, "healthy": True},
                    "records": [{"sequence": 1, "kind": "decision"}],
                }],
            },
        )
        write_json(
            run / "shadow-replay.json",
            {
                "schema": 1,
                "status": "passed",
                "complete": True,
                "gaps": [],
                "trace_sha256": digest((run / "shadow-trace.json").read_bytes()),
                "artifact_sha256": manifest["sha256"],
                "component_sha256": manifest["component_sha256"],
                "config_hash": "a" * 64,
                "provenance": "recorded_engine_snapshot_replay",
                "clock": "recorded_epoch_per_snapshot",
                "physical_effects": "not_observed",
                "comparisons": [{"sequence": 1, "matched": True}],
            },
        )
        write_json(
            run / "shadow-lock-journal.json",
            {
                "schema": 1,
                "status": "passed",
                "provenance": "simulator_generated",
                "physical_effects": "simulator_observed",
                "started_sequence": 1,
                "completed_sequence": 2,
                "command_count": 0,
                "boundaries": ["locked", "reload", "restart"],
                "states": [
                    {"boundary": boundary, "state": {"mode": "observe"}}
                    for boundary in ("locked", "reload", "restart")
                ],
                "events": [{"kind": "report", "sequence": 2}],
            },
        )
        write_json(
            run / "cellar-evidence.json",
            {
                "schema": 1,
                "provenance": "simulator_generated",
                "physical_effects": "simulator_observed",
                "scenarios": [{"id": name} for name in sorted(CELLAR_SCENARIOS)],
            },
        )
        (run / "trace.zip").write_bytes(
            archive_bytes({"trace.trace": b"Bearer browser-test-token"})
        )
        (run / "control").mkdir()
        (run / "control/secrets.json").write_text('["never-package-control-secret"]')
        labs.append(run)
    tests = []
    for version in ("2026.9.3", "2026.9.4"):
        directory = root / "artifacts" / f"tests-{version}"
        directory.mkdir()
        (directory / "junit.xml").write_text(
            '<testsuites><testsuite tests="10" failures="0" errors="0" skipped="0"/></testsuites>'
        )
        (directory / "pytest.log").write_text("10 passed")
        (directory / "dependencies.txt").write_text(f"homeassistant=={version}")
        write_json(
            directory / "coverage.json",
            {
                "files": {
                    "custom_components/ha_operator/__init__.py": {
                        "summary": {"covered_lines": 1, "num_statements": 1}
                    }
                }
            },
        )
        write_json(
            directory / "result.json",
            {
                "ha_version": version,
                "status": "passed",
                "exit_code": 0,
                "artifact_sha256": manifest["sha256"],
                "files": {
                    name: digest((directory / name).read_bytes())
                    for name in TEST_FILES - {"result.json"}
                },
            },
        )
        tests.append(directory)
    mutations = root / "artifacts/mutations.json"
    write_json(
        mutations,
        {
            "status": "passed",
            "baseline": {"status": "passed"},
            "source_sha256": {
                name: value for name, value in sources.items() if name.endswith(".py")
            },
            "mutants": [
                {"name": item.name, "status": "killed", "assertion_failures": 1} for item in MUTANTS
            ],
        },
    )
    logs = root / "artifacts/mutations-logs"
    logs.mkdir()
    for name in ("baseline", *(item.name for item in MUTANTS)):
        for suffix in ("log", "xml"):
            (logs / f"{name}.{suffix}").write_text("mutation evidence fixture")
    validators = root / "artifacts/validators"
    validators.mkdir()
    for name in ("hacs", "hassfest"):
        (validators / f"{name}.log").write_text("validator evidence fixture")
        write_json(
            validators / f"{name}.json",
            {
                "status": "passed",
                "exit_code": 0,
                "image": f"{name}@sha256:" + "a" * 64,
                "source_sha256": sources,
                "log_sha256": digest((validators / f"{name}.log").read_bytes()),
            },
        )
    return dict(
        root=root,
        labs=labs,
        tests=tests,
        mutations=mutations,
        validators=validators,
        output=root / "dist/evidence.zip",
    )


def change(path: Path, **updates):
    data = json.loads(path.read_text())
    data.update(updates)
    write_json(path, data)


def test_bundle_contains_only_selected_successes_and_preserves_release_bytes(complete_evidence):
    arguments = complete_evidence
    first = build_evidence(**arguments)
    content = arguments["output"].read_bytes()
    assert first["status"] == "passed"
    assert len(first["checks"]["lab"]) == 3
    assert build_evidence(**arguments) == first
    assert arguments["output"].read_bytes() == content
    with zipfile.ZipFile(arguments["output"]) as bundle:
        assert not any("control/" in name for name in bundle.namelist())
        assert (
            bundle.read("ha_operator.zip")
            == (arguments["root"] / "dist/ha_operator.zip").read_bytes()
        )
        for name in bundle.namelist():
            if name.endswith("ha.log"):
                assert b"accidental-test-token" not in bundle.read(name)
                assert b"[REDACTED]" in bundle.read(name)
        for run in arguments["labs"]:
            for name in SHADOW_FILES:
                assert f"evidence/lab/{run.name}/{name}" in bundle.namelist()


@pytest.mark.parametrize("status", ["failed", "running", "skipped"])
def test_rejects_nonpassing_lab_status(complete_evidence, status):
    change(complete_evidence["labs"][0] / "summary.json", status=status)
    with pytest.raises(ValueError, match="Lab run did not pass"):
        build_evidence(**complete_evidence)


def test_rejects_mixed_archive_and_missing_required_runs(complete_evidence):
    run = complete_evidence["labs"][0]
    change(run / "prepared.json", artifact_sha256="old archive")
    with pytest.raises(ValueError, match="another archive"):
        build_evidence(**complete_evidence)
    complete_evidence["labs"] = complete_evidence["labs"][1:]
    with pytest.raises(ValueError, match="exactly both"):
        build_evidence(**complete_evidence)


def test_rejects_shortened_soak(complete_evidence):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("soak"))
    cases = json.loads((run / "scenarios.json").read_text())
    next(case for case in cases if case["id"] == "LAB-REALISTIC-SOAK")["completed_at"] = 5
    write_json(run / "scenarios.json", cases)
    manifest = json.loads(
        (complete_evidence["root"] / "dist/ha_operator.manifest.json").read_text()
    )
    with pytest.raises(ValueError, match="two default retry intervals"):
        validate_lab(run, manifest)


def test_rejects_modified_test_evidence(complete_evidence):
    (complete_evidence["tests"][0] / "pytest.log").write_text("modified")
    with pytest.raises(ValueError, match="Source-test evidence changed"):
        build_evidence(**complete_evidence)


def test_rejects_surviving_mutant_or_stale_validator_source(complete_evidence):
    change(complete_evidence["mutations"], status="failed")
    with pytest.raises(ValueError, match="Mutation gate did not pass"):
        build_evidence(**complete_evidence)
    change(complete_evidence["mutations"], status="passed")
    change(complete_evidence["validators"] / "hacs.json", source_sha256={})
    with pytest.raises(ValueError, match="hacs checked different source"):
        build_evidence(**complete_evidence)


def test_refuses_symlink_evidence(complete_evidence, tmp_path):
    log = complete_evidence["labs"][0] / "ha.log"
    log.unlink()
    target = tmp_path / "private.log"
    target.write_text("not an evidence file")
    log.symlink_to(target)
    with pytest.raises(ValueError, match="plain file"):
        build_evidence(**complete_evidence)


def test_partial_all_without_airflow_cannot_pass(complete_evidence):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    scenarios = json.loads((run / "scenarios.json").read_text())
    write_json(
        run / "scenarios.json",
        [scenario for scenario in scenarios if scenario["id"] != "AIRFLOW-MANUAL-CLOSE-LAST-INLET"],
    )
    with pytest.raises(ValueError, match="Missing lab scenarios.*AIRFLOW-MANUAL-CLOSE-LAST-INLET"):
        build_evidence(**complete_evidence)


def test_diagnostics_download_must_exist(complete_evidence):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    (run / "downloaded-diagnostics.json").unlink()
    with pytest.raises(ValueError, match="Missing downloaded-diagnostics.json"):
        build_evidence(**complete_evidence)


def test_cleanup_and_matching_sanitization_are_required(complete_evidence):
    run = complete_evidence["labs"][0]
    change(run / "summary.json", cleanup="retained_by_request")
    with pytest.raises(ValueError, match="Lab cleanup did not complete"):
        build_evidence(**complete_evidence)
    change(run / "summary.json", cleanup="passed")
    change(run / "sanitized.json", run_id="another-run")
    with pytest.raises(ValueError, match="Sanitization receipt"):
        build_evidence(**complete_evidence)
    (run / "sanitized.json").unlink()
    with pytest.raises(FileNotFoundError, match="sanitized.json"):
        build_evidence(**complete_evidence)


def test_shadow_and_cellar_scenarios_are_mandatory():
    assert SHADOW_SCENARIOS == {
        "SHADOW-LOCK-ZERO-COMMANDS",
        "SHADOW-LOCK-RELOAD-RESTART",
        "SHADOW-TRACE-EXPORT",
        "SHADOW-TRACE-REPLAY",
    }
    assert CELLAR_SCENARIOS == {
        "CELLAR-CONFIGURATION",
        "CELLAR-REFUSED-POWER-DIRECTION",
        "CELLAR-OFF-FEEDBACK-REVERSAL",
        "CELLAR-STALE-VIRTUAL-AVAILABILITY",
        "CELLAR-FALLBACK-KEEP-EXTRACTING",
        "CELLAR-MANUAL-SCOPE-EXPIRY",
    }
    assert SHADOW_SCENARIOS | CELLAR_SCENARIOS <= ALL_SCENARIOS


@pytest.mark.parametrize("missing", sorted(SHADOW_SCENARIOS | CELLAR_SCENARIOS))
def test_missing_shadow_or_cellar_scenario_cannot_pass(complete_evidence, missing):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    scenarios = json.loads((run / "scenarios.json").read_text())
    write_json(run / "scenarios.json", [case for case in scenarios if case["id"] != missing])
    with pytest.raises(ValueError, match=f"Missing lab scenarios.*{missing}"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize("missing", sorted(SHADOW_FILES))
def test_shadow_and_cellar_evidence_must_exist(complete_evidence, missing):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    (run / missing).unlink()
    with pytest.raises(ValueError, match=f"Missing {missing}"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    "update",
    [
        {"status": "failed"},
        {"run_id": "other-run"},
        {"completed_at": 1},
        {"containers": ["retained-ha"]},
        {"networks": ["retained-network"]},
        {"volumes": ["retained-data"]},
    ],
)
def test_cleanup_receipt_must_prove_empty_scoped_inventory(complete_evidence, update):
    change(complete_evidence["labs"][0] / "cleanup-verification.json", **update)
    with pytest.raises(ValueError, match="Cleanup receipt"):
        build_evidence(**complete_evidence)


def test_cleanup_receipt_is_required(complete_evidence):
    (complete_evidence["labs"][0] / "cleanup-verification.json").unlink()
    with pytest.raises(FileNotFoundError, match="cleanup-verification.json"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    ("update", "error"),
    [
        ({"status": "incomplete"}, "incomplete"),
        ({"complete": False}, "incomplete"),
        ({"gaps": ["missing session"]}, "incomplete"),
        ({"trace_sha256": "old trace"}, "another trace"),
        ({"artifact_sha256": "old archive"}, "another artifact"),
        ({"component_sha256": "other installation"}, "another artifact"),
        ({"provenance": "counterfactual"}, "recorded snapshots"),
        ({"clock": "wall_clock"}, "recorded snapshots"),
        ({"physical_effects": "observed"}, "unobserved physical effects"),
        ({"comparisons": []}, "compare matching"),
        ({"comparisons": [{"sequence": 1, "matched": False}]}, "compare matching"),
    ],
)
def test_replay_rejects_partial_mismatched_or_overstated_evidence(complete_evidence, update, error):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    change(run / "shadow-replay.json", **update)
    with pytest.raises(ValueError, match=error):
        build_evidence(**complete_evidence)


def test_redaction_cannot_silently_invalidate_replay_provenance(complete_evidence):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    change(run / "shadow-trace.json", accidental="Bearer sensitive-test-token")
    change(
        run / "shadow-replay.json",
        trace_sha256=digest((run / "shadow-trace.json").read_bytes()),
    )
    with pytest.raises(ValueError, match="another trace"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    ("update", "error"),
    [
        ({"provenance": "house_observed"}, "simulator-generated"),
        ({"physical_effects": "not_observed"}, "simulator-generated"),
        ({"scenarios": []}, "every required fault scenario"),
    ],
)
def test_cellar_receipt_requires_explicit_simulator_scope(complete_evidence, update, error):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    change(run / "cellar-evidence.json", **update)
    with pytest.raises(ValueError, match=error):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    "update",
    [
        {"component_sha256": "other component"},
        {"integration_version": "0.0.9"},
        {"gap": True},
        {"more": True},
        {"health": {"enabled": True, "healthy": False}},
    ],
)
def test_native_export_headers_must_match_release_and_be_healthy(complete_evidence, update):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    trace = json.loads((run / "shadow-trace.json").read_text())
    trace["pages"][0].update(update)
    write_json(run / "shadow-trace.json", trace)
    with pytest.raises(ValueError, match="Shadow trace pages"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    "update",
    [
        {"command_count": 1},
        {"events": [{"kind": "command"}]},
        {"boundaries": ["locked", "reload"]},
        {"completed_sequence": 0},
        {"states": [{"boundary": "locked", "state": {"mode": "live"}}]},
    ],
)
def test_lock_receipt_must_prove_no_commands_at_all_lifecycle_boundaries(complete_evidence, update):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    change(run / "shadow-lock-journal.json", **update)
    with pytest.raises(ValueError, match="zero commands through reload and restart"):
        build_evidence(**complete_evidence)
