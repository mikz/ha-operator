"""Exercise explicit safety-guard mutants in a disposable source copy.

This intentionally bounded suite tests four identified failure mechanisms, rather
than reporting a misleading whole-program mutation score. Changed source patterns,
collection errors, skips, timeouts, and surviving mutants fail the gate.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    name: str
    file: str
    original: str
    replacement: str
    tests: tuple[str, ...]


MUTANTS = (
    Mutant(
        "expired_manual_remains_active",
        "custom_components/ha_operator/core.py",
        "return self.expires_at is None or now < self.expires_at",
        "return self.expires_at is None or now <= self.expires_at",
        (
            "tests/unit/test_core.py::"
            "test_expiry_exactly_at_deadline_releases_ownership_without_catchup",
        ),
    ),
    Mutant(
        "unconfirmed_provider_satisfies_airflow",
        "custom_components/ha_operator/core.py",
        "if provider.confirmed is True]",
        "if provider.confirmed is not None]",
        ("tests/unit/test_core.py",),
    ),
    Mutant(
        "stale_generation_dispatches",
        "custom_components/ha_operator/adapters.py",
        "# Re-read restrictions immediately before every individual physical command.\n"
        "        if not current():\n            return False",
        "# Mutant: omit the generation guard before physical dispatch.\n"
        "        if False:\n            return False",
        (
            "tests/integration/test_adapters.py::test_generation_and_restrictions_checked_at_each_call",
            "tests/integration/test_adapters.py::test_fan_stale_generation_between_direction_and_on",
            "tests/integration/test_runtime_reconciliation.py::"
            "test_superseded_generation_cannot_send_after_async_boundary",
        ),
    ),
    Mutant(
        "publish_before_atomic_save",
        "custom_components/ha_operator/storage.py",
        "candidate = deepcopy(candidate)\n            try:",
        "candidate = deepcopy(candidate)\n            self._state = candidate\n            try:",
        (
            "tests/unit/test_storage.py::test_write_failure_never_acknowledges_or_overwrites_in_memory",
            "tests/unit/test_storage.py::test_real_atomic_writer_failure_boundaries",
            "tests/unit/test_storage.py::"
            "test_cancellation_serializes_writes_and_publishes_committed_revision",
        ),
    ),
)


def run_tests(root: Path, tests: tuple[str, ...], evidence: Path, name: str, timeout: int) -> dict:
    """Require executed test results so crashes cannot masquerade as kills."""
    report = evidence / f"{name}.xml"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("COVERAGE_PROCESS_START", None)
    command = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        f"--junitxml={report}",
        *tests,
    ]
    started = time.monotonic()
    try:
        process = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "seconds": time.monotonic() - started, "tests": list(tests)}
    (evidence / f"{name}.log").write_text(process.stdout + process.stderr)
    result = {
        "returncode": process.returncode,
        "seconds": time.monotonic() - started,
        "test_selectors": list(tests),
        "status": "error",
    }
    if not report.exists():
        return result
    document = ET.parse(report)
    suites = document.getroot().iter("testsuite")
    counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, 0))
    result.update(counts)
    assertion_failures = sum(
        (failure.get("message", "").startswith(("assert ", "AssertionError")))
        for failure in document.iter("failure")
    )
    result["assertion_failures"] = assertion_failures
    if counts["tests"] > 0 and counts["errors"] == 0 and counts["skipped"] == 0:
        if process.returncode == 0 and counts["failures"] == 0:
            result["status"] = "passed"
        elif process.returncode == 1 and assertion_failures > 0:
            result["status"] = "failed_assertions"
    return result


def mutation_gate(root: Path, output: Path, timeout: int) -> dict:
    """Patch only a temporary copy; preserve and fingerprint the source tree."""
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = output.parent / f"{output.stem}-logs"
    evidence.mkdir(parents=True, exist_ok=True)
    originals = {mutant.file: (root / mutant.file).read_bytes() for mutant in MUTANTS}
    production = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "custom_components/ha_operator").rglob("*.py")
    }
    results = {"schema_version": 1, "scope": "four explicit safety guards", "status": "failed"}
    results["source_sha256"] = {name: sha256(data).hexdigest() for name, data in production.items()}
    try:
        with TemporaryDirectory(prefix="ha-operator-mutations-") as temporary:
            checkout = Path(temporary)
            for directory in ("custom_components", "tests", "scripts"):
                shutil.copytree(
                    root / directory,
                    checkout / directory,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".hypothesis"),
                )
            shutil.copy2(root / "pyproject.toml", checkout / "pyproject.toml")
            targets = tuple(sorted({test for mutant in MUTANTS for test in mutant.tests}))
            baseline = run_tests(checkout, targets, evidence, "baseline", timeout)
            results["baseline"] = baseline
            if baseline["status"] != "passed":
                return results
            mutations = []
            results["mutants"] = mutations
            for mutant in MUTANTS:
                path = checkout / mutant.file
                source = originals[mutant.file].decode()
                if source.count(mutant.original) != 1:
                    mutations.append({"name": mutant.name, "status": "guard_pattern_changed"})
                    continue
                changed = source.replace(mutant.original, mutant.replacement)
                ast.parse(changed)
                path.write_text(changed)
                try:
                    result = run_tests(checkout, mutant.tests, evidence, mutant.name, timeout)
                    result["name"] = mutant.name
                    result["mutant_sha256"] = sha256(changed.encode()).hexdigest()
                    result["status"] = {"failed_assertions": "killed", "passed": "survived"}.get(
                        result["status"], result["status"]
                    )
                    mutations.append(result)
                finally:
                    path.write_bytes(originals[mutant.file])
            if all(result["status"] == "killed" for result in mutations):
                results["status"] = "passed"
    finally:
        changed_files = [
            name
            for name, data in production.items()
            if not (root / name).is_file() or (root / name).read_bytes() != data
        ]
        if changed_files:
            results["status"] = "source_changed_during_gate"
            results["changed_files"] = changed_files
        output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPOSITORY)
    parser.add_argument("--output", type=Path, default=Path("artifacts/mutations.json"))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    result = mutation_gate(args.root.resolve(), args.output.resolve(), args.timeout)
    print(json.dumps({"status": result["status"], "evidence": str(args.output)}))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
