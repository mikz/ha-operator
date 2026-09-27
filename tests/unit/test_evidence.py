"""Release evidence refuses incomplete, stale, or mixed-artifact gates."""

import json
import zipfile
from pathlib import Path

import pytest

from scripts.evidence import LAB_CASES, MUTANTS, TEST_FILES, build_evidence, validate_lab
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
                "artifact_sha256": manifest["sha256"],
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
        names = ["LAB-ONBOARDING", "LAB-NATIVE-CONFIG-FLOW"]
        if scenario == "all":
            names += ["LAB-HAP-PAIR", "LAB-HAP-RESTART-DURABLE", "LAB-NATIVE-COVER"]
        else:
            names += ["LAB-REALISTIC-SOAK"]
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
        ):
            write_json(run / name, {})
        for name in ("hap-transcript.jsonl", "crash-events.jsonl", "ha.log"):
            (run / name).write_text("Bearer accidental-test-token\n")
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
    cases[-1]["completed_at"] = 5
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
