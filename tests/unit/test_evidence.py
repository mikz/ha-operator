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
    OBSERVABILITY_FILES,
    OBSERVABILITY_SCENARIOS,
    OBSERVE_FILES,
    OBSERVE_SCENARIOS,
    SLEEP_FILES,
    SLEEP_SCENARIOS,
    STATIC_FILES,
    TEST_FILES,
    WINDOW_FILES,
    WINDOW_SCENARIOS,
    build_evidence,
    run_static_checks,
    static_commands,
    static_identity,
    static_success,
    validate_lab,
    validate_static,
    validate_tests,
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
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.1.0"\n'
        '[dependency-groups]\ndev = ["mypy==2.3.1", "ruff==0.16.9"]\n'
        '[tool.mypy]\npython_version = "3.14"\nstrict = true\n'
        'follow_imports = "silent"\nignore_missing_imports = false\n'
        '[tool.ruff]\ntarget-version = "py314"\nline-length = 100\n'
        '[tool.ruff.lint]\nselect = ["E", "F", "I", "UP", "B", "ASYNC"]\n'
    )
    compat = root / "compat/ha2026.9.4"
    compat.mkdir(parents=True)
    (compat / "pyproject.toml").write_text(
        '[project]\nname = "compat-fixture"\nversion = "0.1.0"\n'
        'dependencies = ["mypy==2.3.1", "ruff==0.16.9"]\n'
    )
    (compat / "uv.lock").write_text("compat-locked")
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
                "integration_version": manifest["version"],
                "release_manifest_sha256": digest(
                    (root / "dist/ha_operator.manifest.json").read_bytes()
                ),
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
                "integration_version": manifest["version"],
                "release_manifest_sha256": digest(
                    (root / "dist/ha_operator.manifest.json").read_bytes()
                ),
                "uv_lock_sha256": manifest["locks"]["uv.lock"],
                "images": dict.fromkeys(("ha", "runner", "simulator"), "sha256:" + "0" * 64),
            },
        )
        names = sorted(
            ALL_SCENARIOS
            if scenario == "all"
            else OBSERVABILITY_SCENARIOS
            if scenario == "observability"
            else SLEEP_SCENARIOS
            if scenario == "sleep"
            else WINDOW_SCENARIOS
            if scenario == "windows"
            else INITIAL_SCENARIOS | {"LAB-REALISTIC-SOAK"}
        )
        for name in OBSERVABILITY_FILES | SLEEP_FILES | WINDOW_FILES:
            (run / name).write_text("{}")
        write_json(
            run / "window-native-evidence.json",
            {
                "schema": 1,
                "status": "passed",
                "clock": "real wall clock, accelerated configured durations",
                "snapshots": [
                    {
                        "label": label,
                        "at": index,
                        "explain": {
                            "decision": {"target": {"position": desired}},
                            "observation": {"target": {"position": observed}},
                        },
                        "timer": {"expires_at": 20},
                        "return": {"due_at": 30, "overdue": label == "overdue"},
                    }
                    for index, (label, desired, observed) in enumerate(
                        (
                            ("opening", 100, 7),
                            ("refusal", 100, 7),
                            ("baseline", 7, 100),
                            ("overdue", 7, 100),
                            ("recovered", 7, 7),
                        )
                    )
                ],
            },
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
            *sorted(OBSERVE_FILES),
        ):
            write_json(run / name, {})
        for name in ("hap-transcript.jsonl", "crash-events.jsonl", "ha.log"):
            (run / name).write_text("Bearer accidental-test-token\n")
        write_json(
            run / "observe-lock-effects.json",
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
                "meta": {"branch_coverage": True},
                "files": {
                    "custom_components/ha_operator/__init__.py": {
                        "summary": {
                            "covered_lines": 1,
                            "num_statements": 1,
                            "num_branches": 0,
                            "covered_branches": 0,
                            "missing_branches": 0,
                        }
                    }
                },
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
    static = []
    identity = static_identity(root, manifest)
    for version in ("2026.9.3", "2026.9.4"):
        directory = root / "artifacts" / f"static-{version}"
        directory.mkdir()
        for name, contents in static_success(identity["modules"]).items():
            (directory / name).write_text(contents + "\n")
        (directory / "dependencies.txt").write_text(
            f"homeassistant=={version}\nmypy==2.3.1\nruff==0.16.9\n"
        )
        write_json(
            directory / "result.json",
            {
                "ha_version": version,
                "status": "passed",
                "exit_code": 0,
                "exit_codes": dict.fromkeys(static_commands(identity["modules"]), 0),
                "artifact_sha256": manifest["sha256"],
                "source_commit": manifest["source_commit"],
                "identity": identity,
                "commands": static_commands(identity["modules"]),
                "environment": {
                    "ha": version,
                    "mypy": "2.3.1",
                    "ruff": "0.16.9",
                    "python": "3.14.7",
                },
                "files": {
                    name: digest((directory / name).read_bytes())
                    for name in STATIC_FILES - {"result.json"}
                },
            },
        )
        static.append(directory)
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
        static=static,
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
    assert len(first["checks"]["lab"]) == len(LAB_CASES)
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
        assert first["checks"]["strict_static"] == ["2026.9.3", "2026.9.4"]
        for version in ("2026.9.3", "2026.9.4"):
            for name in STATIC_FILES:
                assert f"evidence/static/{version}/{name}" in bundle.namelist()
        for run in arguments["labs"]:
            if "observability" in run.name:
                for name in OBSERVABILITY_FILES:
                    assert f"evidence/lab/{run.name}/{name}" in bundle.namelist()
            for name in OBSERVE_FILES:
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
    with pytest.raises(ValueError, match="Need both"):
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
        validate_lab(
            run,
            manifest,
            digest((complete_evidence["root"] / "dist/ha_operator.manifest.json").read_bytes()),
        )


def test_rejects_modified_test_evidence(complete_evidence):
    (complete_evidence["tests"][0] / "pytest.log").write_text("modified")
    with pytest.raises(ValueError, match="Source-test evidence changed"):
        build_evidence(**complete_evidence)


def test_sanitized_junit_has_explicit_provenance_and_valid_delivered_hashes(complete_evidence):
    directory = complete_evidence["tests"][0]
    junit = directory / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="10" failures="0" errors="0" skipped="0">'
        '<testcase name="test_redaction[123-45-678]"/></testsuite></testsuites>'
    )
    original = json.loads((directory / "result.json").read_text())
    original["files"]["junit.xml"] = digest(junit.read_bytes())
    original["counts"] = {"tests": 10, "failures": 0, "errors": 0, "skipped": 0}
    write_json(directory / "result.json", original)
    original_receipt = (directory / "result.json").read_bytes()
    original_junit = junit.read_bytes()
    index = build_evidence(**complete_evidence)
    with zipfile.ZipFile(complete_evidence["output"]) as bundle:
        prefix = f"evidence/tests/{original['ha_version']}/"
        delivered = json.loads(bundle.read(prefix + "result.json"))
        assert delivered["files"]["junit.xml"] == digest(bundle.read(prefix + "junit.xml"))
        assert b"[REDACTED]" in bundle.read(prefix + "junit.xml")
        provenance = delivered.pop("sanitization")
        assert provenance == {
            "schema": 1,
            "method": "tests.lab.redaction.sanitize_artifacts",
            "producer_receipt_sha256": digest(original_receipt),
            "original_files": original["files"],
            "changed_files": ["junit.xml"],
            "execution_results_changed": False,
        }
        delivered["files"] = provenance["original_files"]
        assert delivered == original
        extracted = complete_evidence["root"] / "delivered"
        bundle.extractall(extracted)
        manifest = json.loads((extracted / "ha_operator.manifest.json").read_text())
        assert validate_tests(extracted / prefix, manifest, complete_evidence["root"]) == "2026.9.3"
    assert (directory / "result.json").read_bytes() == original_receipt
    assert junit.read_bytes() == original_junit
    assert len(index["evidence_builder"]["source_sha256"]) == 5


@pytest.mark.parametrize("validator", ["hacs", "hassfest"])
def test_sanitized_validator_log_preserves_producer_claims(complete_evidence, validator):
    directory = complete_evidence["validators"]
    log = directory / f"{validator}.log"
    log.write_text("Bearer validator-fixture-secret\n")
    receipt = directory / f"{validator}.json"
    original = json.loads(receipt.read_text())
    original["log_sha256"] = digest(log.read_bytes())
    write_json(receipt, original)
    original_receipt = receipt.read_bytes()
    build_evidence(**complete_evidence)
    with zipfile.ZipFile(complete_evidence["output"]) as bundle:
        prefix = "evidence/validators/"
        delivered = json.loads(bundle.read(prefix + receipt.name))
        assert delivered["log_sha256"] == digest(bundle.read(prefix + log.name))
        assert b"validator-fixture-secret" not in bundle.read(prefix + log.name)
        provenance = delivered.pop("sanitization")
        assert provenance["producer_receipt_sha256"] == digest(original_receipt)
        assert provenance["original_files"] == {log.name: original["log_sha256"]}
        assert provenance["changed_files"] == [log.name]
        assert provenance["execution_results_changed"] is False
        delivered["log_sha256"] = provenance["original_files"][log.name]
        assert delivered == original
    assert receipt.read_bytes() == original_receipt
    assert log.read_text() == "Bearer validator-fixture-secret\n"


@pytest.mark.parametrize("target", ["receipt", "counts"])
def test_sanitization_cannot_change_executed_results(complete_evidence, monkeypatch, target):
    from scripts import evidence

    original = evidence.sanitize_artifacts

    def faulty_sanitizer(directory, secrets):
        original(directory, secrets)
        tests = directory / "tests/2026.9.3"
        if target == "receipt":
            change(tests / "result.json", status="failed")
        else:
            path = tests / "junit.xml"
            path.write_text(path.read_text().replace('tests="10"', 'tests="9"'))

    monkeypatch.setattr(evidence, "sanitize_artifacts", faulty_sanitizer)
    with pytest.raises(ValueError, match="Sanitization (modified|changed executed)"):
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
    assert OBSERVE_SCENARIOS == {
        "SHADOW-LOCK-ZERO-COMMANDS",
        "SHADOW-LOCK-RELOAD-RESTART",
    }
    assert CELLAR_SCENARIOS == {
        "CELLAR-HAP-FAN-COMPOSITION",
        "CELLAR-CONFIGURATION",
        "CELLAR-REFUSED-POWER-DIRECTION",
        "CELLAR-OFF-FEEDBACK-REVERSAL",
        "CELLAR-STALE-VIRTUAL-AVAILABILITY",
        "CELLAR-FALLBACK-KEEP-EXTRACTING",
        "CELLAR-MANUAL-SCOPE-EXPIRY",
    }
    assert OBSERVE_SCENARIOS | CELLAR_SCENARIOS <= ALL_SCENARIOS


@pytest.mark.parametrize("missing", sorted(OBSERVE_SCENARIOS | CELLAR_SCENARIOS))
def test_missing_shadow_or_cellar_scenario_cannot_pass(complete_evidence, missing):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    scenarios = json.loads((run / "scenarios.json").read_text())
    write_json(run / "scenarios.json", [case for case in scenarios if case["id"] != missing])
    with pytest.raises(ValueError, match=f"Missing lab scenarios.*{missing}"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize("missing", sorted(OBSERVE_FILES))
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
        {"command_count": 1},
        {"events": [{"kind": "command"}]},
        {"boundaries": ["locked", "reload"]},
        {"completed_sequence": 0},
        {"states": [{"boundary": "locked", "state": {"mode": "live"}}]},
    ],
)
def test_lock_receipt_must_prove_no_commands_at_all_lifecycle_boundaries(complete_evidence, update):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("all"))
    change(run / "observe-lock-effects.json", **update)
    with pytest.raises(ValueError, match="zero commands through reload and restart"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize("version", ["2026.9.3", "2026.9.4"])
def test_native_windows_are_required_on_both_pins(complete_evidence, version):
    run = next(path for path in complete_evidence["labs"] if path.name == version + "-windows")
    scenarios = json.loads((run / "scenarios.json").read_text())
    missing = "WINDOW-NATIVE-RETURN-DEMO"
    write_json(run / "scenarios.json", [case for case in scenarios if case["id"] != missing])
    with pytest.raises(ValueError, match="Missing lab scenarios.*" + missing):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize("missing", sorted(WINDOW_FILES))
def test_windows_native_evidence_files_are_required(complete_evidence, missing):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("windows"))
    (run / missing).unlink()
    with pytest.raises(ValueError, match="Missing " + missing):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    "update", [{"status": "pending"}, {"clock": "unspecified"}, {"snapshots": []}]
)
def test_windows_incomplete_demonstration_cannot_pass(complete_evidence, update):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("windows"))
    native = json.loads((run / "window-native-evidence.json").read_text())
    write_json(run / "window-native-evidence.json", {**native, **update})
    with pytest.raises(ValueError, match="Native window evidence"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize("field", ["chronology", "desired", "raw", "expiry", "warning"])
def test_native_windows_inconsistent_snapshots_cannot_pass(complete_evidence, field):
    run = next(path for path in complete_evidence["labs"] if path.name.endswith("windows"))
    native = json.loads((run / "window-native-evidence.json").read_text())
    snapshots = native["snapshots"]
    if field == "chronology":
        snapshots[1]["at"] = -1
    elif field == "desired":
        snapshots[2]["explain"]["decision"]["target"]["position"] = 100
    elif field == "raw":
        snapshots[3]["explain"]["observation"]["target"]["position"] = 7
    elif field == "expiry":
        snapshots[1]["timer"]["expires_at"] += 10
    else:
        snapshots[3]["return"]["due_at"] += 10
    write_json(run / "window-native-evidence.json", native)
    with pytest.raises(ValueError, match="Native window evidence"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize("version", ["2026.9.3", "2026.9.4"])
def test_both_native_static_results_are_required(complete_evidence, version):
    complete_evidence["static"] = [p for p in complete_evidence["static"] if version not in p.name]
    with pytest.raises(ValueError, match="both native HA strict static"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    "update",
    [
        {"status": "failed"},
        {"exit_code": 1},
        {"artifact_sha256": "stale"},
        {"source_commit": "other"},
        {"identity": {}},
        {"commands": {}},
        {"environment": {"ha": "2026.9.3", "mypy": "1.19"}},
        {"exit_codes": {"mypy": 0}},
        {"exit_codes": {"mypy": 0, "ruff_lint": 1, "ruff_format": 0}},
    ],
)
def test_static_proof_rejects_failure_staleness_and_scope(complete_evidence, update):
    path = complete_evidence["static"][0] / "result.json"
    change(path, **update)
    with pytest.raises(ValueError):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize("missing", sorted(STATIC_FILES))
def test_static_proof_requires_every_receipt_and_log(complete_evidence, missing):
    (complete_evidence["static"][0] / missing).unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    "name", ["static.log", "ruff-lint.log", "ruff-format.log", "dependencies.txt"]
)
def test_static_proof_rejects_altered_execution_evidence(complete_evidence, name):
    (complete_evidence["static"][0] / name).write_text("stale")
    with pytest.raises(ValueError, match="Static evidence changed"):
        build_evidence(**complete_evidence)


@pytest.mark.parametrize(
    "name", ["gold-native-mode.png", "gold-native-dom.json", "observability-explain-responses.json"]
)
def test_translated_native_observability_files_are_required(complete_evidence, name):
    run = next(p for p in complete_evidence["labs"] if "observability" in p.name)
    (run / name).unlink()
    with pytest.raises(ValueError):
        build_evidence(**complete_evidence)


def test_translated_native_observability_scenario_is_required(complete_evidence):
    run = next(p for p in complete_evidence["labs"] if "observability" in p.name)
    scenarios = json.loads((run / "scenarios.json").read_text())
    write_json(
        run / "scenarios.json",
        [s for s in scenarios if s["id"] != "OBS-NATIVE-TRANSLATED-PRESENTATION"],
    )
    with pytest.raises(ValueError):
        build_evidence(**complete_evidence)


def test_static_runner_executes_real_pinned_checkers_against_disposable_artifact(
    complete_evidence, tmp_path
):
    import importlib.metadata
    import sys

    root = complete_evidence["root"]
    directory = tmp_path / "actual-static-run"
    version = importlib.metadata.version("homeassistant")
    result = run_static_checks(root, Path(sys.executable), version, directory)
    manifest = json.loads((root / "dist/ha_operator.manifest.json").read_text())
    assert result["status"] == "passed"
    assert validate_static(directory, manifest, root) == version
    assert set(result["commands"]) == {"mypy", "ruff_lint", "ruff_format"}
    assert result["identity"]["modules"] == {
        "custom_components/ha_operator/__init__.py": digest(
            (root / "custom_components/ha_operator/__init__.py").read_bytes()
        )
    }


@pytest.mark.parametrize(
    "change_config",
    [
        "strict = false",
        "ignore_missing_imports = true",
        'exclude = ["runtime.py"]',
        '[[tool.mypy.overrides]]\nmodule = "*"\nignore_errors = true',
    ],
)
def test_static_config_cannot_suppress_required_scope(complete_evidence, change_config):
    path = complete_evidence["root"] / "pyproject.toml"
    source = path.read_text()
    if change_config.startswith("strict"):
        source = source.replace("strict = true", change_config)
    elif change_config.startswith("ignore_missing"):
        source = source.replace("ignore_missing_imports = false", change_config)
    elif change_config.startswith("exclude"):
        source = source.replace("[tool.mypy]\n", "[tool.mypy]\n" + change_config + "\n")
    else:
        source += "\n" + change_config + "\n"
    path.write_text(source)
    with pytest.raises(ValueError, match="strict mypy configuration"):
        build_evidence(**complete_evidence)


def test_static_config_and_lock_binding_reject_changed_bytes(complete_evidence):
    path = complete_evidence["root"] / "pyproject.toml"
    path.write_text(path.read_text() + "\n# modified configuration\n")
    with pytest.raises(ValueError, match="scope is stale"):
        build_evidence(**complete_evidence)


def test_static_proof_rejects_forged_success_for_incomplete_modules(complete_evidence):
    directory = complete_evidence["static"][0]
    log = directory / "static.log"
    log.write_text("Success: no issues found in 0 source files\n")
    result = json.loads((directory / "result.json").read_text())
    result["files"]["static.log"] = digest(log.read_bytes())
    write_json(directory / "result.json", result)
    with pytest.raises(ValueError, match="every production module"):
        build_evidence(**complete_evidence)


def test_static_proof_rejects_unchecked_production_module(complete_evidence):
    root = complete_evidence["root"]
    (root / "custom_components/ha_operator/unchecked.py").write_text("ready = True\n")
    manifest = json.loads((root / "dist/ha_operator.manifest.json").read_text())
    with pytest.raises(ValueError, match="omits or differs"):
        validate_static(complete_evidence["static"][0], manifest, root)


def test_static_proof_rejects_changed_lock(complete_evidence):
    root = complete_evidence["root"]
    (root / "compat/ha2026.9.4/uv.lock").write_text("changed lock")
    manifest = json.loads((root / "dist/ha_operator.manifest.json").read_text())
    with pytest.raises(ValueError, match="dependency lock changed"):
        validate_static(complete_evidence["static"][0], manifest, root)


def test_static_runner_retains_actual_checker_failure(complete_evidence, tmp_path):
    import importlib.metadata
    import sys

    root = complete_evidence["root"]
    (root / "custom_components/ha_operator/__init__.py").write_text(
        "def missing_annotation():\n    return 1\n"
    )
    build(root, root / "dist", replace=True)
    directory = tmp_path / "failed-static-run"
    version = importlib.metadata.version("homeassistant")
    with pytest.raises(ValueError, match="Static check failed: mypy"):
        run_static_checks(root, Path(sys.executable), version, directory)
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "failed"
    assert result["exit_codes"] == {"mypy": 1}
    assert "no-untyped-def" in (directory / "static.log").read_text()


def test_lab_rejects_matching_stale_manifest_hashes(complete_evidence):
    root = complete_evidence["root"]
    run = complete_evidence["labs"][0]
    manifest_path = root / "dist/ha_operator.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name in ("summary.json", "prepared.json"):
        change(run / name, release_manifest_sha256="0" * 64)
    with pytest.raises(ValueError, match="prepared release manifest hash"):
        validate_lab(run, manifest, digest(manifest_path.read_bytes()))
