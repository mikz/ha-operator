"""Run source checks against release bytes and bundle only completed release gates."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.coverage_gate import check_coverage  # noqa: E402
from scripts.mutation_gate import MUTANTS  # noqa: E402
from scripts.release import archive_bytes, digest, integration_files, verify  # noqa: E402
from tests.lab.redaction import sanitize_artifacts  # noqa: E402

VERSIONS = {"2026.9.3", "2026.9.4"}
LAB_CASES = {("2026.9.3", "all"), ("2026.9.4", "all"), ("2026.9.3", "soak")}
INITIAL_SCENARIOS = {"LAB-ONBOARDING", "LAB-NATIVE-CONFIG-FLOW", "LAB-RESOURCE-CONFIGURATION"}
SHADOW_SCENARIOS = {
    "SHADOW-LOCK-ZERO-COMMANDS",
    "SHADOW-LOCK-RELOAD-RESTART",
    "SHADOW-TRACE-EXPORT",
    "SHADOW-TRACE-REPLAY",
}
CELLAR_SCENARIOS = {
    "CELLAR-CONFIGURATION",
    "CELLAR-REFUSED-POWER-DIRECTION",
    "CELLAR-OFF-FEEDBACK-REVERSAL",
    "CELLAR-STALE-VIRTUAL-AVAILABILITY",
    "CELLAR-FALLBACK-KEEP-EXTRACTING",
    "CELLAR-MANUAL-SCOPE-EXPIRY",
}
SHADOW_FILES = {
    "shadow-trace.json",
    "shadow-replay.json",
    "shadow-lock-journal.json",
    "cellar-evidence.json",
}
ALL_SCENARIOS = INITIAL_SCENARIOS | SHADOW_SCENARIOS | CELLAR_SCENARIOS | {
    "OBSERVE-ZERO",
    "COVER-RAIN-RETRY",
    "COVER-SUPERSESSION",
    "COVER-STOP",
    "COVER-EXPIRY",
    "COVER-RESTART",
    "COVER-KILL",
    "SCHEDULE-SLEEP-IN-DURABLE",
    "SCHEDULE-ADJACENT-NATIVE-BLOCKS",
    "OCCURRENCE-DST-IDENTITY-EXPIRY",
    "LAB-HAP-PAIR",
    "LAB-HAP-RESTART-DURABLE",
    "LAB-NATIVE-COVER",
    "LAB-DIAGNOSTICS",
    "AIRFLOW-CONFIGURATION",
    "FAN-OFF-DIRECTION-NO-AIRFLOW",
    "AIRFLOW-UNMET-ALTERNATIVES-KEEP-EXTRACTING",
    "AIRFLOW-MANUAL-CLOSE-LAST-INLET",
    "AIRFLOW-MAKE-BEFORE-BREAK-PHYSICAL-CONFIRMATION",
    "RELAY-REVERSAL-CONFIRMED-DEAD-TIME",
}
LAB_FILES = SHADOW_FILES | {
    "summary.json",
    "sanitized.json",
    "cleanup-verification.json",
    "prepared.json",
    "compose.json",
    "inspect.json",
    "scenarios.json",
    "routes-ha.json",
    "routes-simulator.json",
    "routes-runner.json",
    "simulator-journal.json",
    "crash-events.jsonl",
    "trace.zip",
    "hap-transcript.jsonl",
    "hap-accessories.json",
    "hap-durable-receipt.json",
    "downloaded-diagnostics.json",
    "ha.log",
    "simulator.log",
    "runner.log",
    "onboarding-complete.png",
    "integration-configured.png",
    "native-cover-closed.png",
}
TEST_FILES = {"result.json", "coverage.json", "junit.xml", "pytest.log", "dependencies.txt"}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def junit_counts(path: Path) -> dict[str, int]:
    counts = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for suite in ET.parse(path).getroot().iter("testsuite"):
        for key in counts:
            counts[key] += int(suite.get(key, 0))
    return counts


def release_manifest(root: Path) -> dict:
    return verify(root / "dist/ha_operator.zip", root / "dist/ha_operator.manifest.json", root=root)


def run_validator(root: Path, name: str, directory: Path, ref: str | None = None) -> dict:
    """Run an official read-only validator and record its exact source and image."""
    images = {
        "hacs": "ghcr.io/hacs/action:main",
        "hassfest": "ghcr.io/home-assistant/hassfest:latest",
    }
    require(name in images, "Unknown validator")
    source = integration_files(root)
    source_hashes = {
        f"custom_components/ha_operator/{path}": digest(data)
        for path, data in source.items()
        if path != "LICENSE"
    }
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(["docker", "pull", images[name]], check=True, capture_output=True)
    image = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", images[name], "--format", "{{json .RepoDigests}}"],
            text=True,
        )
    )[0]
    environment = os.environ.copy()
    command = ["docker", "run", "--rm"]
    token = None
    if name == "hacs":
        ref = (
            ref
            or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        )
        require(
            subprocess.run(
                ["git", "diff", "--quiet", ref, "--", "custom_components/ha_operator"],
                cwd=root,
                check=False,
            ).returncode
            == 0,
            "HACS must validate the committed integration source",
        )
        require(
            not subprocess.check_output(
                [
                    "git",
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "custom_components/ha_operator",
                ],
                cwd=root,
                text=True,
            ).strip(),
            "HACS cannot validate uncommitted integration files",
        )
        token = (
            environment.get("INPUT_GITHUB_TOKEN")
            or subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        )
        environment.update(
            {
                "INPUT_GITHUB_TOKEN": token,
                "INPUT_REPOSITORY": "mikz/ha-operator",
                "INPUT_CATEGORY": "integration",
                "INPUT_COMMENT": "false",
                "REPOSITORY_REF": ref,
                "GITHUB_REPOSITORY": "mikz/ha-operator",
            }
        )
        for key in (
            "INPUT_GITHUB_TOKEN",
            "INPUT_REPOSITORY",
            "INPUT_CATEGORY",
            "INPUT_COMMENT",
            "REPOSITORY_REF",
            "GITHUB_REPOSITORY",
        ):
            command.extend(("-e", key))
    else:
        command.extend(("-v", f"{root.resolve()}:/github/workspace:ro"))
    command.append(image)
    process = subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, check=False
    )
    log = process.stdout + process.stderr
    if token:
        log = log.replace(token, "[REDACTED]")
    logfile = directory / f"{name}.log"
    logfile.write_text(log)
    result = {
        "status": "passed"
        if process.returncode == 0 and integration_files(root) == source
        else "failed",
        "exit_code": process.returncode,
        "image": image,
        "source_sha256": source_hashes,
        "log_sha256": digest(log.encode()),
        "ref": ref,
    }
    write_json(directory / f"{name}.json", result)
    require(result["status"] == "passed", f"{name} failed or source changed; see {logfile}")
    return result


def run_source_tests(root: Path, python: Path, version: str, directory: Path) -> dict:
    """Record actual pytest/coverage execution before and after source verification."""
    manifest = release_manifest(root)
    require(version in VERSIONS, "Unsupported Home Assistant test version")
    directory.mkdir(parents=True, exist_ok=True)
    require(not (directory / "result.json").exists(), "Use a fresh test evidence directory")
    environment = subprocess.check_output(
        [
            str(python),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('homeassistant'))",
        ],
        cwd=root,
        text=True,
    ).strip()
    require(environment == version, f"Expected HA {version}; interpreter has {environment}")
    result = {
        "status": "failed",
        "ha_version": version,
        "artifact_sha256": manifest["sha256"],
        "source_commit": manifest["source_commit"],
        "started_at": time.time(),
        "execution": "source tests with release/source identity verified before and after",
    }
    try:
        with (directory / "pytest.log").open("w") as log:
            process = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pytest",
                    "--cov=custom_components/ha_operator",
                    "--cov-report=term-missing",
                    f"--cov-report=json:{directory / 'coverage.json'}",
                    f"--junitxml={directory / 'junit.xml'}",
                ],
                cwd=root,
                env={**os.environ, "COVERAGE_FILE": str(directory / ".coverage")},
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        result["exit_code"] = process.returncode
        require(process.returncode == 0, f"pytest failed; see {directory / 'pytest.log'}")
        counts = junit_counts(directory / "junit.xml")
        require(
            counts["tests"] > 0 and not any(counts[k] for k in ("failures", "errors", "skipped")),
            "Tests must execute successfully without skipped cases",
        )
        failures = check_coverage(read_json(directory / "coverage.json"), root)
        require(not failures, "Coverage gate failed: " + "; ".join(failures))
        require(release_manifest(root) == manifest, "Release changed while tests ran")
        with (directory / "dependencies.txt").open("w") as dependencies:
            subprocess.run(
                ["uv", "pip", "freeze", "--python", str(python)],
                cwd=root,
                stdout=dependencies,
                check=True,
            )
        result.update(status="passed", counts=counts, coverage_gate="passed")
        result["files"] = {
            name: digest((directory / name).read_bytes()) for name in TEST_FILES - {"result.json"}
        }
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        result["completed_at"] = time.time()
        write_json(directory / "result.json", result)
    return result


def validate_lab(directory: Path, manifest: dict) -> tuple[str, str]:
    summary = read_json(directory / "summary.json")
    prepared = read_json(directory / "prepared.json")
    sanitized = read_json(directory / "sanitized.json")
    cleanup = read_json(directory / "cleanup-verification.json")
    case = (summary["ha_version"], summary["scenario"])
    require(case in LAB_CASES, f"Unexpected release lab case: {case}")
    require(
        summary.get("status") == "passed" and summary.get("isolation") == "passed",
        f"Lab run did not pass: {directory}",
    )
    require(summary.get("cleanup") == "passed", "Lab cleanup did not complete")
    require(
        cleanup.get("status") == "passed"
        and cleanup.get("run_id") == summary.get("run_id")
        and isinstance(cleanup.get("completed_at"), (int, float))
        and cleanup["completed_at"] >= summary.get("completed_at", 0)
        and all(cleanup.get(name) == [] for name in ("containers", "networks", "volumes")),
        "Cleanup receipt must prove this run left no scoped Docker resources",
    )
    require(
        sanitized.get("run_id") == summary.get("run_id")
        and isinstance(sanitized.get("completed_at"), (int, float))
        and sanitized["completed_at"] >= summary.get("completed_at", 0),
        "Sanitization receipt does not match the completed lab run",
    )
    require(summary.get("artifact_sha256") == manifest["sha256"], "Lab tested another archive")
    require(
        prepared.get("artifact_sha256") == manifest["sha256"], "Prepared image uses another archive"
    )
    require(prepared.get("ha_version") == case[0], "Prepared HA version differs from run")
    require(
        prepared.get("uv_lock_sha256") == manifest["locks"].get("uv.lock"),
        "Lab was prepared with a different dependency lock",
    )
    require(set(prepared.get("images", {})) == {"ha", "simulator", "runner"}, "Missing image IDs")
    require(
        all(value.startswith("sha256:") for value in prepared["images"].values()),
        "Lab images are not pinned by digest",
    )
    scenarios = read_json(directory / "scenarios.json")
    require(
        bool(scenarios) and all(case.get("status") == "passed" for case in scenarios),
        "Lab contains an incomplete or failed scenario",
    )
    names = {case["id"] for case in scenarios}
    require(len(names) == len(scenarios), "Lab scenario IDs must be unique")
    required = INITIAL_SCENARIOS.copy()
    if case[1] == "all":
        required = ALL_SCENARIOS
        for name in (
            "hap-transcript.jsonl",
            "hap-accessories.json",
            "crash-events.jsonl",
            "downloaded-diagnostics.json",
            *sorted(SHADOW_FILES),
        ):
            require((directory / name).is_file(), f"Missing {name}")
        validate_shadow_replay(directory, manifest)
    else:
        required.add("LAB-REALISTIC-SOAK")
        soak = next((item for item in scenarios if item["id"] == "LAB-REALISTIC-SOAK"), {})
        require(
            soak.get("completed_at", 0) - soak.get("started_at", 0) >= 598,
            "Soak did not span two default retry intervals",
        )
    require(required <= names, f"Missing lab scenarios: {sorted(required - names)}")
    for name in (
        "compose.json",
        "inspect.json",
        "routes-ha.json",
        "routes-simulator.json",
        "routes-runner.json",
        "trace.zip",
        "simulator-journal.json",
        "ha.log",
    ):
        require((directory / name).is_file(), f"Missing lab evidence: {name}")
    return case


def validate_shadow_replay(directory: Path, manifest: dict) -> None:
    """Keep recorder replay claims bound to complete, exact-source evidence."""
    trace = read_json(directory / "shadow-trace.json")
    pages = trace.get("pages")
    require(
        trace.get("schema") == 1 and isinstance(pages, list) and bool(pages),
        "Shadow trace must preserve native export pages",
    )
    require(
        all(
            page.get("schema") == 1
            and page.get("integration_version") == manifest["version"]
            and page.get("component_sha256") == manifest["component_sha256"]
            and page.get("gap") is False
            and page.get("health", {}).get("enabled") is True
            and page.get("health", {}).get("healthy") is True
            for page in pages
        )
        and pages[-1].get("more") is False,
        "Shadow trace pages must be healthy, complete, and match the installed component",
    )
    replay = read_json(directory / "shadow-replay.json")
    require(
        replay.get("schema") == 1
        and replay.get("status") == "passed"
        and replay.get("complete") is True
        and replay.get("gaps") == [],
        "Shadow replay is incomplete or did not pass",
    )
    require(
        replay.get("trace_sha256") == digest((directory / "shadow-trace.json").read_bytes()),
        "Shadow replay covers another trace",
    )
    require(
        replay.get("artifact_sha256") == manifest["sha256"]
        and replay.get("component_sha256") == manifest["component_sha256"]
        and replay.get("config_hash") == pages[-1].get("config_hash")
        and isinstance(replay.get("config_hash"), str),
        "Shadow replay covers another artifact or installed component",
    )
    require(
        replay.get("provenance") == "recorded_engine_snapshot_replay"
        and replay.get("clock") == "recorded_epoch_per_snapshot"
        and replay.get("physical_effects") == "not_observed",
        "Shadow replay must declare recorded snapshots and unobserved physical effects",
    )
    comparisons = replay.get("comparisons")
    require(
        isinstance(comparisons, list)
        and bool(comparisons)
        and all(case.get("matched") is True for case in comparisons),
        "Shadow replay must compare matching engine snapshots",
    )
    lock = read_json(directory / "shadow-lock-journal.json")
    boundaries = {"locked", "reload", "restart"}
    states = lock.get("states", [])
    require(
        lock.get("schema") == 1
        and lock.get("status") == "passed"
        and lock.get("provenance") == "simulator_generated"
        and lock.get("physical_effects") == "simulator_observed"
        and lock.get("command_count") == 0
        and isinstance(lock.get("events"), list)
        and not any(event.get("kind") == "command" for event in lock["events"])
        and set(lock.get("boundaries", [])) == boundaries
        and {state.get("boundary") for state in states} == boundaries
        and all(state.get("state", {}).get("mode") == "observe" for state in states)
        and type(lock.get("started_sequence")) is int
        and type(lock.get("completed_sequence")) is int
        and 0 <= lock["started_sequence"] <= lock["completed_sequence"],
        "Shadow lock evidence must prove zero commands through reload and restart",
    )
    cellar = read_json(directory / "cellar-evidence.json")
    require(
        cellar.get("schema") == 1
        and cellar.get("provenance") == "simulator_generated"
        and cellar.get("physical_effects") == "simulator_observed",
        "Cellar evidence must identify simulator-generated effects",
    )
    cellar_ids = [case.get("id") for case in cellar.get("scenarios", [])]
    require(
        len(cellar_ids) == len(set(cellar_ids)) and CELLAR_SCENARIOS <= set(cellar_ids),
        "Cellar evidence must cover every required fault scenario",
    )


def validate_tests(directory: Path, manifest: dict, root: Path) -> str:
    result = read_json(directory / "result.json")
    version = result.get("ha_version")
    require(version in VERSIONS, "Unknown source-test HA version")
    require(
        result.get("status") == "passed" and result.get("exit_code") == 0,
        "Source tests did not pass",
    )
    require(
        result.get("artifact_sha256") == manifest["sha256"], "Source tests cover another archive"
    )
    for name in TEST_FILES - {"result.json"}:
        require(
            digest((directory / name).read_bytes()) == result.get("files", {}).get(name),
            f"Source-test evidence changed: {name}",
        )
    counts = junit_counts(directory / "junit.xml")
    require(
        counts["tests"] > 0 and not any(counts[k] for k in ("failures", "errors", "skipped")),
        "Source tests did not complete without failures and skips",
    )
    require(
        not check_coverage(read_json(directory / "coverage.json"), root), "Coverage gate failed"
    )
    return version


def safe_copy(source: Path, destination: Path) -> None:
    require(not source.is_symlink() and source.is_file(), f"Evidence is not a plain file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def bind_sanitized_files(
    original: Path, staged: Path, files: dict[str, Path], *, log: bool = False
) -> None:
    """Bind delivered bytes while preserving the producer's execution claims."""
    original_bytes = original.read_bytes()
    receipt = json.loads(original_bytes)
    require(read_json(staged) == receipt, "Sanitization modified an execution receipt")
    before = {next(iter(files)): receipt["log_sha256"]} if log else receipt["files"]
    after = {name: digest(path.read_bytes()) for name, path in files.items()}
    require(set(before) == set(after), "Sanitized receipt file set changed")
    changed = sorted(name for name in before if before[name] != after[name])
    if not changed:
        return
    require("sanitization" not in receipt, "Cannot overwrite existing sanitization provenance")
    receipt["sanitization"] = {
        "schema": 1,
        "method": "tests.lab.redaction.sanitize_artifacts",
        "producer_receipt_sha256": digest(original_bytes),
        "original_files": before,
        "changed_files": changed,
        "execution_results_changed": False,
    }
    if log:
        receipt["log_sha256"] = next(iter(after.values()))
    else:
        receipt["files"] = after
    write_json(staged, receipt)


def build_evidence(
    root: Path, labs: list[Path], tests: list[Path], mutations: Path, validators: Path, output: Path
) -> dict:
    manifest = release_manifest(root)
    require(bool(manifest.get("source_commit")), "Release must identify its source commit")
    selected = [validate_lab(directory, manifest) for directory in labs]
    require(
        len(selected) == 3 and set(selected) == LAB_CASES,
        "Need exactly both HA all runs and baseline soak",
    )
    versions = [validate_tests(directory, manifest, root) for directory in tests]
    require(
        len(versions) == 2 and set(versions) == VERSIONS, "Need both compatibility test results"
    )
    mutation = read_json(mutations)
    require(mutation.get("status") == "passed", "Mutation gate did not pass")
    require(
        mutation.get("baseline", {}).get("status") == "passed", "Mutation baseline did not pass"
    )
    mutants = mutation.get("mutants", [])
    require(
        {item.get("name") for item in mutants} == {item.name for item in MUTANTS}
        and len(mutants) == len(MUTANTS)
        and all(
            item.get("status") == "killed" and item.get("assertion_failures", 0) > 0
            for item in mutants
        ),
        "All four guards must be killed by assertions",
    )
    source_hashes = {
        f"custom_components/ha_operator/{name}": data["sha256"]
        for name, data in manifest["files"].items()
        if name != "LICENSE"
    }
    require(
        mutation.get("source_sha256")
        == {name: value for name, value in source_hashes.items() if name.endswith(".py")},
        "Mutation tests cover different production source",
    )
    for validator in ("hacs", "hassfest"):
        status = read_json(validators / f"{validator}.json")
        require(
            status.get("status") == "passed" and status.get("exit_code") == 0,
            f"{validator} did not pass",
        )
        require(
            status.get("image") and "@sha256:" in status["image"],
            f"{validator} image digest is missing",
        )
        require(
            status.get("log_sha256") == digest((validators / f"{validator}.log").read_bytes()),
            f"{validator} log does not match its receipt",
        )
        require(
            status.get("source_sha256") == source_hashes, f"{validator} checked different source"
        )
    for name, expected in manifest["locks"].items():
        require(digest((root / name).read_bytes()) == expected, f"Dependency lock changed: {name}")
    with TemporaryDirectory(prefix="ha-operator-evidence-") as temporary:
        staging = Path(temporary)
        evidence = staging / "evidence"
        for directory in labs:
            run_name = read_json(directory / "summary.json")["run_id"]
            require(
                Path(run_name).name == run_name and not run_name.startswith("."), "Unsafe run ID"
            )
            for name in LAB_FILES:
                if (directory / name).exists():
                    safe_copy(directory / name, evidence / "lab" / run_name / name)
        for version, directory in zip(versions, tests, strict=True):
            for name in TEST_FILES:
                safe_copy(directory / name, evidence / "tests" / version / name)
        safe_copy(mutations, evidence / "mutations.json")
        mutation_logs = mutations.parent / f"{mutations.stem}-logs"
        for name in ("baseline", *(item.name for item in MUTANTS)):
            for suffix in ("log", "xml"):
                safe_copy(
                    mutation_logs / f"{name}.{suffix}", evidence / "mutations" / f"{name}.{suffix}"
                )
        for validator in ("hacs", "hassfest"):
            for suffix in ("json", "log"):
                safe_copy(
                    validators / f"{validator}.{suffix}",
                    evidence / "validators" / f"{validator}.{suffix}",
                )
        for name in manifest["locks"]:
            safe_copy(root / name, evidence / "locks" / name)
        sanitize_artifacts(evidence, [])
        for version, directory in zip(versions, tests, strict=True):
            delivered = evidence / "tests" / version
            bind_sanitized_files(
                directory / "result.json",
                delivered / "result.json",
                {name: delivered / name for name in TEST_FILES - {"result.json"}},
            )
            validate_tests(delivered, manifest, root)
            require(
                junit_counts(delivered / "junit.xml") == junit_counts(directory / "junit.xml"),
                "Sanitization changed executed test results",
            )
        for validator in ("hacs", "hassfest"):
            name = f"{validator}.log"
            bind_sanitized_files(
                validators / f"{validator}.json",
                evidence / "validators" / f"{validator}.json",
                {name: evidence / "validators" / name},
                log=True,
            )
            delivered = read_json(evidence / "validators" / f"{validator}.json")
            require(
                delivered["log_sha256"] == digest((evidence / "validators" / name).read_bytes()),
                f"Sanitized {validator} log does not match its receipt",
            )
        for directory, case in zip(labs, selected, strict=True):
            run_name = read_json(directory / "summary.json")["run_id"]
            require(
                validate_lab(evidence / "lab" / run_name, manifest) == case,
                "Sanitization changed a lab identity",
            )
        safe_copy(root / "dist/ha_operator.zip", staging / "ha_operator.zip")
        safe_copy(root / "dist/ha_operator.manifest.json", staging / "ha_operator.manifest.json")
        files = {
            path.relative_to(staging).as_posix(): path.read_bytes()
            for path in staging.rglob("*")
            if path.is_file()
        }
        index = {
            "schema_version": 1,
            "status": "passed",
            "artifact_sha256": manifest["sha256"],
            "component_sha256": manifest["component_sha256"],
            "source_commit": manifest["source_commit"],
            "evidence_builder": {
                "source_sha256": {
                    name: digest((ROOT / name).read_bytes())
                    for name in (
                        "scripts/evidence.py",
                        "scripts/coverage_gate.py",
                        "scripts/mutation_gate.py",
                        "scripts/release.py",
                        "tests/lab/redaction.py",
                    )
                },
            },
            "checks": {
                "source_tests": sorted(versions),
                "coverage": "passed",
                "mutations": "passed",
                "hacs": "passed",
                "hassfest": "passed",
                "lab": [list(case) for case in sorted(selected)],
            },
            "files": {
                name: {"sha256": digest(data), "size": len(data)}
                for name, data in sorted(files.items())
            },
        }
        files["evidence.json"] = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode()
        content = archive_bytes(files)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        write_json(output.with_suffix(".json"), {**index, "bundle_sha256": digest(content)})
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tests", "validator", "build"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--ha-version", choices=sorted(VERSIONS))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--validator", choices=("hacs", "hassfest"))
    parser.add_argument("--ref")
    parser.add_argument("--lab-run", type=Path, action="append", default=[])
    parser.add_argument("--tests", type=Path, action="append", default=[])
    parser.add_argument("--mutations", type=Path, default=Path("artifacts/mutations.json"))
    parser.add_argument("--validators", type=Path, default=Path("artifacts/validators"))
    parser.add_argument("--output", type=Path, default=Path("dist/ha_operator-evidence.zip"))
    args = parser.parse_args()
    try:
        if args.command == "validator":
            require(args.validator is not None, "validator requires --validator")
            result = run_validator(args.root, args.validator, args.validators, args.ref)
        elif args.command == "tests":
            require(
                args.ha_version is not None and args.directory is not None,
                "tests requires --ha-version and --directory",
            )
            result = run_source_tests(
                args.root, args.python.absolute(), args.ha_version, args.directory.absolute()
            )
        else:
            result = build_evidence(
                args.root, args.lab_run, args.tests, args.mutations, args.validators, args.output
            )
    except (ValueError, OSError, KeyError, ET.ParseError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Evidence gate failed: {error}\n")
    print(
        json.dumps({"status": result["status"], "artifact_sha256": result.get("artifact_sha256")})
    )


if __name__ == "__main__":
    main()
