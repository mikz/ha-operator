"""Run source checks against release bytes and bundle only completed release gates."""

from __future__ import annotations

import argparse
import json
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
from scripts.release import archive_bytes, digest, verify  # noqa: E402
from tests.lab.redaction import sanitize_artifacts  # noqa: E402

VERSIONS = {"2026.9.3", "2026.9.4"}
LAB_CASES = {("2026.9.3", "all"), ("2026.9.4", "all"), ("2026.9.3", "soak")}
LAB_FILES = {
    "summary.json",
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
    case = (summary["ha_version"], summary["scenario"])
    require(case in LAB_CASES, f"Unexpected release lab case: {case}")
    require(
        summary.get("status") == "passed" and summary.get("isolation") == "passed",
        f"Lab run did not pass: {directory}",
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
    required = {"LAB-ONBOARDING", "LAB-NATIVE-CONFIG-FLOW"}
    if case[1] == "all":
        required |= {"LAB-HAP-PAIR", "LAB-HAP-RESTART-DURABLE", "LAB-NATIVE-COVER"}
        for name in ("hap-transcript.jsonl", "hap-accessories.json", "crash-events.jsonl"):
            require((directory / name).is_file(), f"Missing {name}")
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
            "source_commit": manifest["source_commit"],
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
    parser.add_argument("command", choices=("tests", "build"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--ha-version", choices=sorted(VERSIONS))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--lab-run", type=Path, action="append", default=[])
    parser.add_argument("--tests", type=Path, action="append", default=[])
    parser.add_argument("--mutations", type=Path, default=Path("artifacts/mutations.json"))
    parser.add_argument("--validators", type=Path, default=Path("artifacts/validators"))
    parser.add_argument("--output", type=Path, default=Path("dist/ha_operator-evidence.zip"))
    args = parser.parse_args()
    try:
        if args.command == "tests":
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
    print(json.dumps({"status": result["status"], "artifact_sha256": result["artifact_sha256"]}))


if __name__ == "__main__":
    main()
