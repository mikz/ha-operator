"""Run source checks against release bytes and bundle only completed release gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
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
LAB_CASES = {
    ("2026.9.3", "all"),
    ("2026.9.4", "all"),
    ("2026.9.3", "soak"),
    ("2026.9.3", "observability"),
    ("2026.9.4", "observability"),
    ("2026.9.3", "sleep"),
    ("2026.9.4", "sleep"),
    ("2026.9.3", "windows"),
    ("2026.9.4", "windows"),
}
INITIAL_SCENARIOS = {"LAB-ONBOARDING", "LAB-NATIVE-CONFIG-FLOW", "LAB-RESOURCE-CONFIGURATION"}
SLEEP_SCENARIOS = INITIAL_SCENARIOS | {
    "SLEEP-EXISTING-GROUPS-HAP",
    "SLEEP-NATIVE-SETUP-OBSERVE",
    "SLEEP-FRESH-COMMANDS-LIVE",
    "SLEEP-SOURCE-ONLY-GROUP-ATTACHMENT",
    "SLEEP-RESTART-DETACHED-PAIR",
    "SLEEP-HAP-PUBLIC-IDENTITY",
    "SLEEP-HAP-DIRECT-REPLACEMENT",
    "SLEEP-NATIVE-DASHBOARD",
    "LAB-DIAGNOSTICS",
}
SLEEP_FILES = {"sleep-evidence.json", "sleep-controls.png", "hap-transcript.jsonl"}
WINDOW_SCENARIOS = INITIAL_SCENARIOS | {
    "WINDOW-CONFIGURATION",
    "WINDOW-OBSERVE-PREFLIGHT-ZERO",
    "WINDOW-NATIVE-SCENE-NOOP-AND-EXPLICIT-INTENT",
    "WINDOW-RAIN-CLEAR-BEFORE-EXPIRY",
    "WINDOW-RAIN-CLEAR-AFTER-EXPIRY",
    "WINDOW-INDEPENDENT-EXPIRY-IDEMPOTENCY-SUPERSESSION",
    "WINDOW-MULTI-PREFLIGHT-AND-GROUP-AVERAGE",
    "WINDOW-RAW-HELPER-UNKNOWN",
    "WINDOW-NO-PHYSICAL-STOP",
    "WINDOW-TIMER-OCCURRENCE-CANCEL-RESTART",
    "WINDOW-COLD-POLICY-MANUAL-PRECEDENCE",
    "WINDOW-EXPIRED-OPENING-RESTART",
    "WINDOW-EXPIRED-OPENING-KILL",
    "WINDOW-PAUSED-TIMER-RESTORATION",
    "WINDOW-TIMER-PAUSE-WITHDRAWAL-FAILURE",
    "WINDOW-SAME-VALUE-HAP-REPLACES-INTENT",
    "WINDOW-BOUNDED-AUTOMATIC-OCCURRENCE",
    "WINDOW-OVERDUE-RETURN-AND-RECOVERY",
    "WINDOW-REGISTRY-OWNER-TRANSFER",
    "WINDOW-RELOAD-PRESERVES-RELAY-FAN",
    "WINDOW-NATIVE-CONFIGURATION",
    "WINDOW-NATIVE-NUMERIC-QUALIFICATION",
    "WINDOW-NATIVE-NUMERIC-UNKNOWN-RECOVERY",
    "WINDOW-NATIVE-TIMER-LIFECYCLE",
    "WINDOW-NATIVE-TIMER-PRECEDENCE",
    "WINDOW-NATIVE-TIMER-RESTART",
    "WINDOW-NATIVE-TIMER-KILL",
    "WINDOW-NATIVE-PENDING-RELOAD",
    "WINDOW-NATIVE-OBSERVE-ZERO",
    "WINDOW-NATIVE-DASHBOARD",
    "WINDOW-NATIVE-RETURN-DEMO",
    "WINDOW-NATIVE-RETURN-UNKNOWN-VIRTUAL",
    "LAB-HAP-PAIR",
    "LAB-HAP-RESTART-SAVED",
    "LAB-DIAGNOSTICS",
}
WINDOW_FILES = {
    "window-native-evidence.json",
    "window-native-dashboard.json",
    "window-native-opening.png",
    "window-native-refusal.png",
    "window-native-baseline.png",
    "window-native-overdue.png",
    "window-native-recovered.png",
    "crash-events.jsonl",
    "hap-transcript.jsonl",
    "window-hap-request-result.json",
}
OBSERVABILITY_SCENARIOS = INITIAL_SCENARIOS | {
    "OBS-NATIVE-ENTITIES",
    "OBS-NATIVE-TRANSLATED-PRESENTATION",
    "OBS-REASON-CHANGE-NO-DISPATCH",
    "OBS-EXPIRY-AND-RECORDER",
    "OBS-FAN-PROFILE-REASON",
    "OBS-NATIVE-DASHBOARD",
    "LAB-DIAGNOSTICS",
}
OBSERVABILITY_FILES = {
    "observability-explain-responses.json",
    "observability-history.json",
    "observability-activity.json",
    "observability-dashboard.json",
    "observability-dashboard.png",
    "gold-native-mode.png",
    "gold-native-dom.json",
}
OBSERVE_SCENARIOS = {
    "SHADOW-LOCK-ZERO-COMMANDS",
    "SHADOW-LOCK-RELOAD-RESTART",
}
CELLAR_SCENARIOS = {
    "CELLAR-CONFIGURATION",
    "CELLAR-REFUSED-POWER-DIRECTION",
    "CELLAR-OFF-FEEDBACK-REVERSAL",
    "CELLAR-STALE-VIRTUAL-AVAILABILITY",
    "CELLAR-FALLBACK-KEEP-EXTRACTING",
    "CELLAR-MANUAL-SCOPE-EXPIRY",
    "CELLAR-HAP-FAN-COMPOSITION",
}
OBSERVE_FILES = {
    "observe-lock-effects.json",
    "cellar-evidence.json",
}
ALL_SCENARIOS = (
    INITIAL_SCENARIOS
    | OBSERVE_SCENARIOS
    | CELLAR_SCENARIOS
    | {
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
        "LAB-HAP-RESTART-SAVED",
        "LAB-NATIVE-COVER",
        "LAB-DIAGNOSTICS",
        "AIRFLOW-CONFIGURATION",
        "FAN-OFF-DIRECTION-NO-AIRFLOW",
        "AIRFLOW-UNMET-ALTERNATIVES-KEEP-EXTRACTING",
        "AIRFLOW-MANUAL-CLOSE-LAST-INLET",
        "AIRFLOW-MAKE-BEFORE-BREAK-PHYSICAL-CONFIRMATION",
        "RELAY-REVERSAL-CONFIRMED-DEAD-TIME",
    }
)
LAB_FILES = (
    OBSERVE_FILES
    | OBSERVABILITY_FILES
    | SLEEP_FILES
    | WINDOW_FILES
    | {
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
        "hap-request-result.json",
        "downloaded-diagnostics.json",
        "ha.log",
        "simulator.log",
        "runner.log",
        "onboarding-complete.png",
        "integration-configured.png",
        "native-cover-closed.png",
    }
)
STATIC_FILES = {"result.json", "static.log", "ruff-lint.log", "ruff-format.log", "dependencies.txt"}
MYPY_VERSION = "2.3.1"
RUFF_VERSION = "0.16.9"
STATIC_CONFIGS = ("pyproject.toml", "compat/ha2026.9.4/pyproject.toml")

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
    sanitize_artifacts(directory, [])
    log = logfile.read_text()
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
        sanitize_artifacts(directory, [])
        result["files"] = {
            name: digest((directory / name).read_bytes()) for name in TEST_FILES - {"result.json"}
        }
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        result["completed_at"] = time.time()
        write_json(directory / "result.json", result)
        sanitize_artifacts(directory, [])
    return result


def static_identity(root: Path, manifest: dict) -> dict:
    """Bind the checked module set, strict configuration and locked tooling."""
    config = tomllib.loads((root / "pyproject.toml").read_text())
    require(
        config.get("tool", {}).get("mypy")
        == {
            "python_version": "3.14",
            "strict": True,
            "follow_imports": "silent",
            "ignore_missing_imports": False,
        },
        "Static proof requires the complete strict mypy configuration without overrides",
    )
    require(
        config.get("tool", {}).get("ruff")
        == {
            "target-version": "py314",
            "line-length": 100,
            "lint": {"select": ["E", "F", "I", "UP", "B", "ASYNC"]},
        },
        "Static proof requires full Ruff lint/format configuration without scope exclusions",
    )
    projects = {name: tomllib.loads((root / name).read_text()) for name in STATIC_CONFIGS}
    require(
        f"mypy=={MYPY_VERSION}" in projects["pyproject.toml"]["dependency-groups"]["dev"]
        and f"mypy=={MYPY_VERSION}" in projects[STATIC_CONFIGS[1]]["project"]["dependencies"],
        "Both native environments must pin the static checker",
    )
    require(
        f"ruff=={RUFF_VERSION}" in projects["pyproject.toml"]["dependency-groups"]["dev"]
        and f"ruff=={RUFF_VERSION}" in projects[STATIC_CONFIGS[1]]["project"]["dependencies"],
        "Both native environments must pin the lint/format checker",
    )
    require(
        {"uv.lock", "compat/ha2026.9.4/uv.lock"} <= set(manifest["locks"]),
        "Static proof requires both native environment locks",
    )
    for name, expected in manifest["locks"].items():
        require(
            digest((root / name).read_bytes()) == expected,
            f"Static dependency lock changed: {name}",
        )
    modules = {
        f"custom_components/ha_operator/{name}": data["sha256"]
        for name, data in manifest["files"].items()
        if name.endswith(".py")
    }
    require(bool(modules), "Static proof requires production modules")
    actual_modules = {
        path.relative_to(root).as_posix(): digest(path.read_bytes())
        for path in (root / "custom_components/ha_operator").rglob("*.py")
    }
    require(
        modules == actual_modules, "Static archive omits or differs from current production modules"
    )
    return {
        "modules": modules,
        "locks": manifest["locks"],
        "configs": {name: digest((root / name).read_bytes()) for name in STATIC_CONFIGS},
        "producer_sha256": digest((ROOT / "scripts/evidence.py").read_bytes()),
    }


def static_commands(modules: dict) -> dict[str, list[str]]:
    scope = sorted(modules)
    return {
        "mypy": ["-m", "mypy", "--no-incremental", "--config-file", "pyproject.toml", *scope],
        "ruff_lint": ["-m", "ruff", "check", *scope],
        "ruff_format": ["-m", "ruff", "format", "--check", *scope],
    }


def static_success(modules: dict) -> dict[str, str]:
    return {
        "static.log": f"Success: no issues found in {len(modules)} source file"
        + ("s" if len(modules) != 1 else ""),
        "ruff-lint.log": "All checks passed!",
        "ruff-format.log": f"{len(modules)} file"
        + ("s" if len(modules) != 1 else "")
        + " already formatted",
    }


def run_static_checks(root: Path, python: Path, version: str, directory: Path) -> dict:
    """Execute the pinned checker against every source module bound to release bytes."""
    manifest = release_manifest(root)
    require(version in VERSIONS, "Unsupported static-check HA version")
    identity = static_identity(root, manifest)
    environment = json.loads(
        subprocess.check_output(
            [
                str(python),
                "-c",
                "import importlib.metadata as m, json, platform; "
                "print(json.dumps({'ha':m.version('homeassistant'), "
                "'mypy':m.version('mypy'), 'ruff':m.version('ruff'), "
                "'python':platform.python_version()}))",
            ],
            cwd=root,
            text=True,
        )
    )
    require(
        environment["ha"] == version
        and environment["mypy"] == MYPY_VERSION
        and environment["ruff"] == RUFF_VERSION,
        "Static interpreter must contain the expected native HA and pinned mypy",
    )
    directory.mkdir(parents=True, exist_ok=True)
    require(not (directory / "result.json").exists(), "Use a fresh static evidence directory")
    commands = static_commands(identity["modules"])
    result = {
        "status": "failed",
        "ha_version": version,
        "environment": environment,
        "artifact_sha256": manifest["sha256"],
        "source_commit": manifest["source_commit"],
        "identity": identity,
        "commands": commands,
        "started_at": time.time(),
        "execution": "uncached strict/lint/format source checks; "
        "release/source identity verified before and after",
    }
    try:
        result["exit_codes"] = {}
        for name, command in commands.items():
            filename = {
                "mypy": "static.log",
                "ruff_lint": "ruff-lint.log",
                "ruff_format": "ruff-format.log",
            }[name]
            with (directory / filename).open("w") as log:
                process = subprocess.run(
                    [str(python), *command],
                    cwd=root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            result["exit_codes"][name] = process.returncode
            result["exit_code"] = process.returncode
            require(process.returncode == 0, f"Static check failed: {name}")
        for filename, expected in static_success(identity["modules"]).items():
            require(
                (directory / filename).read_text().strip() == expected,
                f"Static checker did not confirm complete scope: {filename}",
            )
        require(
            release_manifest(root) == manifest and static_identity(root, manifest) == identity,
            "Static source/configuration/locks changed while checking",
        )
        with (directory / "dependencies.txt").open("w") as dependencies:
            subprocess.run(
                ["uv", "pip", "freeze", "--python", str(python)],
                cwd=root,
                stdout=dependencies,
                check=True,
            )
        result["status"] = "passed"
        sanitize_artifacts(directory, [])
        result["files"] = {
            name: digest((directory / name).read_bytes()) for name in STATIC_FILES - {"result.json"}
        }
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        result["completed_at"] = time.time()
        write_json(directory / "result.json", result)
        sanitize_artifacts(directory, [])
    return result


def validate_static(directory: Path, manifest: dict, root: Path) -> str:
    result = read_json(directory / "result.json")
    version = result.get("ha_version")
    require(version in VERSIONS, "Unknown static-check HA version")
    require(
        result.get("status") == "passed" and result.get("exit_code") == 0,
        "Static checks did not pass",
    )
    require(
        result.get("artifact_sha256") == manifest["sha256"]
        and result.get("source_commit") == manifest["source_commit"],
        "Static checks cover another archive/source identity",
    )
    identity = static_identity(root, manifest)
    require(
        result.get("identity") == identity, "Static source/configuration/lock/module scope is stale"
    )
    require(
        result.get("commands") == static_commands(identity["modules"]),
        "Static checker command omits or overrides required scope",
    )
    environment = result.get("environment", {})
    require(
        environment.get("ha") == version
        and environment.get("mypy") == MYPY_VERSION
        and environment.get("ruff") == RUFF_VERSION
        and environment.get("python", "").startswith("3.14."),
        "Static checker environment is incompatible",
    )
    for name in STATIC_FILES - {"result.json"}:
        require(
            digest((directory / name).read_bytes()) == result.get("files", {}).get(name),
            f"Static evidence changed: {name}",
        )
    require(
        result.get("exit_codes") == dict.fromkeys(static_commands(identity["modules"]), 0),
        "Static proof requires passing mypy, Ruff lint and format results",
    )
    for filename, expected in static_success(identity["modules"]).items():
        require(
            (directory / filename).read_text().strip() == expected,
            f"Static checker did not check every production module: {filename}",
        )
    dependencies = (directory / "dependencies.txt").read_text().splitlines()
    require(
        f"homeassistant=={version}" in dependencies
        and f"mypy=={MYPY_VERSION}" in dependencies
        and f"ruff=={RUFF_VERSION}" in dependencies,
        "Static installed dependencies do not match the receipt",
    )
    return version


def validate_lab(directory: Path, manifest: dict) -> tuple[str, str]:
    summary = read_json(directory / "summary.json")
    prepared = read_json(directory / "prepared.json")
    sanitized = read_json(directory / "sanitized.json")
    cleanup = read_json(directory / "cleanup-verification.json")
    require(
        summary.get("integration_version") == manifest["version"],
        "Lab integration version differs from the release",
    )
    require(
        summary.get("release_manifest_sha256") == prepared.get("release_manifest_sha256")
        and isinstance(summary.get("release_manifest_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", summary["release_manifest_sha256"]),
        "Lab must carry its prepared release manifest hash",
    )
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
            *sorted(OBSERVE_FILES),
        ):
            require((directory / name).is_file(), f"Missing {name}")
        validate_observe_effects(directory, manifest)
    elif case[1] == "observability":
        required = OBSERVABILITY_SCENARIOS
        for name in OBSERVABILITY_FILES:
            require((directory / name).is_file(), f"Missing {name}")
    elif case[1] == "sleep":
        required = SLEEP_SCENARIOS
        for name in SLEEP_FILES:
            require((directory / name).is_file(), f"Missing {name}")
    elif case[1] == "windows":
        required = WINDOW_SCENARIOS
        for name in WINDOW_FILES:
            require((directory / name).is_file(), f"Missing {name}")
        native = read_json(directory / "window-native-evidence.json")
        require(
            native.get("schema") == 1
            and native.get("status") == "passed"
            and native.get("clock") == "real wall clock, accelerated configured durations",
            "Native window evidence must identify the accelerated clock and completed run",
        )
        labels = {item.get("label") for item in native.get("snapshots", [])}
        require(
            {"opening", "refusal", "baseline", "overdue", "recovered"} <= labels,
            "Native window evidence must retain all five demonstration states",
        )
        demo = {
            item["label"]: item
            for item in native["snapshots"]
            if item["label"] in {"opening", "refusal", "baseline", "overdue", "recovered"}
        }
        ordered = [
            demo[label] for label in ("opening", "refusal", "baseline", "overdue", "recovered")
        ]
        require(
            all(isinstance(item.get("at"), (int, float)) for item in ordered)
            and [item["at"] for item in ordered] == sorted(item["at"] for item in ordered),
            "Native window evidence must preserve demonstration chronology",
        )
        require(
            [
                item.get("explain", {}).get("decision", {}).get("target", {}).get("position")
                for item in ordered
            ]
            == [100, 100, 7, 7, 7],
            "Native window evidence must show opening and effective baseline targets",
        )
        require(
            [
                item.get("explain", {}).get("observation", {}).get("target", {}).get("position")
                for item in ordered[1:]
            ]
            == [7, 100, 100, 7],
            "Native window evidence must separate refused targets from raw return feedback",
        )
        require(
            demo["opening"].get("timer", {}).get("expires_at") is not None
            and demo["opening"]["timer"]["expires_at"]
            == demo["refusal"].get("timer", {}).get("expires_at")
            and demo["baseline"].get("return", {}).get("due_at") is not None
            and demo["baseline"]["return"]["due_at"]
            == demo["overdue"].get("return", {}).get("due_at")
            and demo["overdue"].get("return", {}).get("overdue") is True
            and demo["recovered"].get("return", {}).get("overdue") is False,
            "Native window evidence must retain expiry and warning deadline until recovery",
        )
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


def validate_observe_effects(directory: Path, manifest: dict) -> None:
    """Check independent simulator effects through lock, reload, and restart."""
    lock = read_json(directory / "observe-lock-effects.json")
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
    root: Path,
    labs: list[Path],
    tests: list[Path],
    mutations: Path,
    validators: Path,
    output: Path,
    static: list[Path] | None = None,
) -> dict:
    manifest = release_manifest(root)
    require(bool(manifest.get("source_commit")), "Release must identify its source commit")
    selected = [validate_lab(directory, manifest) for directory in labs]
    require(
        len(selected) == len(LAB_CASES) and set(selected) == LAB_CASES,
        "Need both HA all/windows/observability/sleep runs and the HA 2026.9.3 soak (nine runs)",
    )
    versions = [validate_tests(directory, manifest, root) for directory in tests]
    require(
        len(versions) == 2 and set(versions) == VERSIONS, "Need both compatibility test results"
    )
    static = static or []
    static_versions = [validate_static(directory, manifest, root) for directory in static]
    require(
        len(static_versions) == 2 and set(static_versions) == VERSIONS,
        "Need both native HA strict static results",
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
        "All nine guards must be killed by assertions",
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
        for version, directory in zip(static_versions, static, strict=True):
            for name in STATIC_FILES:
                safe_copy(directory / name, evidence / "static" / version / name)
        for name in STATIC_CONFIGS:
            safe_copy(root / name, evidence / "static-config" / name)
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
        for version, directory in zip(static_versions, static, strict=True):
            delivered = evidence / "static" / version
            bind_sanitized_files(
                directory / "result.json",
                delivered / "result.json",
                {name: delivered / name for name in STATIC_FILES - {"result.json"}},
            )
            validate_static(delivered, manifest, root)
        for name in STATIC_CONFIGS:
            require(
                digest((evidence / "static-config" / name).read_bytes())
                == digest((root / name).read_bytes()),
                "Sanitization changed the strict configuration",
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
        sanitize_artifacts(evidence, [], check=True)
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
                "strict_static": sorted(static_versions),
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
    parser.add_argument("command", choices=("tests", "static", "validator", "build"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--ha-version", choices=sorted(VERSIONS))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--validator", choices=("hacs", "hassfest"))
    parser.add_argument("--ref")
    parser.add_argument("--lab-run", type=Path, action="append", default=[])
    parser.add_argument("--static", type=Path, action="append", default=[])
    parser.add_argument("--tests", type=Path, action="append", default=[])
    parser.add_argument("--mutations", type=Path, default=Path("artifacts/mutations.json"))
    parser.add_argument("--validators", type=Path, default=Path("artifacts/validators"))
    parser.add_argument("--output", type=Path, default=Path("dist/ha_operator-evidence.zip"))
    args = parser.parse_args()
    try:
        if args.command == "validator":
            require(args.validator is not None, "validator requires --validator")
            result = run_validator(args.root, args.validator, args.validators, args.ref)
        elif args.command in {"tests", "static"}:
            require(
                args.ha_version is not None and args.directory is not None,
                "tests/static requires --ha-version and --directory",
            )
            runner = run_source_tests if args.command == "tests" else run_static_checks
            result = runner(
                args.root, args.python.absolute(), args.ha_version, args.directory.absolute()
            )
        else:
            result = build_evidence(
                args.root,
                args.lab_run,
                args.tests,
                args.mutations,
                args.validators,
                args.output,
                args.static,
            )
    except (ValueError, OSError, KeyError, ET.ParseError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Evidence gate failed: {error}\n")
    print(
        json.dumps({"status": result["status"], "artifact_sha256": result.get("artifact_sha256")})
    )


if __name__ == "__main__":
    main()
