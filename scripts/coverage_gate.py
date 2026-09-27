"""Require strictly over 95% line coverage in every production Python module."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
PREFIX = "custom_components/ha_operator/"


def typing_only_lines(source: str) -> set[int]:
    """Permit import-only TYPE_CHECKING blocks, never executable logic exclusions."""
    allowed = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
            and not node.orelse
            and all(isinstance(child, (ast.Import, ast.ImportFrom)) for child in node.body)
        ):
            allowed.update(range(node.lineno, node.end_lineno + 1))
    return allowed


def check_coverage(report: dict, root: Path) -> list[str]:
    """Fail missing modules, hidden executable code, or insufficient coverage."""
    failures = []
    files = report.get("files", {})
    normalized = {}
    for name, data in files.items():
        name = name.replace("\\", "/")
        if PREFIX in name:
            normalized[PREFIX + name.split(PREFIX, 1)[1]] = data
    modules = sorted((root / PREFIX).rglob("*.py"))
    if not modules:
        return ["No production Python modules found"]
    for module in modules:
        name = module.relative_to(root).as_posix()
        entry = normalized.get(name)
        if entry is None:
            failures.append(f"{name}: absent from coverage report")
            continue
        summary = entry.get("summary", {})
        total = summary.get("num_statements")
        covered = summary.get("covered_lines")
        if set(entry.get("excluded_lines", ())) - typing_only_lines(module.read_text()):
            failures.append(f"{name}: excluded executable lines are forbidden by the release gate")
        if (
            type(total) is not int or type(covered) is not int
            or total < 0 or covered < 0 or covered > total
        ):
            failures.append(f"{name}: invalid coverage counts")
            continue
        if total and covered * 100 <= total * 95:
            failures.append(f"{name}: {covered}/{total} lines; requires strictly over 95%")
        if module.name == "config_flow.py" and (
            covered != total or entry.get("missing_branches") or entry.get("missing_lines")
            or report.get("meta", {}).get("branch_coverage") is not True
        ):
            failures.append(f"{name}: config flow requires full line and branch coverage")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--root", type=Path, default=REPOSITORY)
    args = parser.parse_args()
    try:
        failures = check_coverage(json.loads(args.report.read_text()), args.root)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"Unable to validate coverage: {error}\n")
    if failures:
        parser.exit(1, "\n".join(failures) + "\n")
    print("Coverage gate passed: every production module >95%; config flow 100%")


if __name__ == "__main__":
    main()
