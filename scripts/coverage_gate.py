"""Require strictly over 95% line and combined coverage in every production module."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
PREFIX = "custom_components/ha_operator/"


def typing_only_lines(source: str) -> set[int]:
    """Permit typing imports and inert overload stubs, never runtime logic exclusions."""
    allowed = set()
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
            and not node.orelse
            and all(isinstance(child, (ast.Import, ast.ImportFrom)) for child in node.body)
        ):
            allowed.update(range(node.lineno, node.end_lineno + 1))
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.Module, ast.ClassDef)):
            continue
        functions = [
            node for node in scope.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        def is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
            return any(
                isinstance(item, ast.Name) and item.id == "overload" for item in node.decorator_list
            )

        implementations = {
            node.name
            for node in functions
            if not is_overload(node)
            and not (
                len(node.body) == 1
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and node.body[0].value.value is Ellipsis
            )
        }
        for node in functions:
            if (
                node.name in implementations
                and len(node.decorator_list) == 1
                and is_overload(node)
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and node.body[0].value.value is Ellipsis
                and not any(isinstance(item, ast.Call) for item in ast.walk(node.args))
                and not (
                    node.returns is not None
                    and any(isinstance(item, ast.Call) for item in ast.walk(node.returns))
                )
            ):
                allowed.update(range(node.decorator_list[0].lineno, node.end_lineno + 1))
                # coverage.py includes trailing blank separators in its stub exclusion.
                end = node.end_lineno
                while end < len(lines) and not lines[end].strip():
                    end += 1
                    allowed.add(end)
    return allowed


def check_coverage(report: dict, root: Path) -> list[str]:
    """Fail missing modules, hidden executable code, or insufficient coverage."""
    failures = []
    if report.get("meta", {}).get("branch_coverage") is not True:
        failures.append("Branch coverage metadata is required")
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
        branches = summary.get("num_branches")
        covered_branches = summary.get("covered_branches")
        missing_branches = summary.get("missing_branches")
        if set(entry.get("excluded_lines", ())) - typing_only_lines(module.read_text()):
            failures.append(f"{name}: excluded executable lines are forbidden by the release gate")
        if (
            type(total) is not int
            or type(covered) is not int
            or total < 0
            or covered < 0
            or covered > total
        ):
            failures.append(f"{name}: invalid coverage counts")
            continue
        if (
            any(type(value) is not int for value in (branches, covered_branches, missing_branches))
            or branches < 0
            or covered_branches < 0
            or missing_branches < 0
            or covered_branches + missing_branches != branches
        ):
            failures.append(f"{name}: invalid branch coverage counts")
            continue
        if total and covered * 100 <= total * 95:
            failures.append(f"{name}: {covered}/{total} lines; requires strictly over 95%")
        elif total + branches and (covered + covered_branches) * 100 <= (total + branches) * 95:
            failures.append(
                f"{name}: {covered + covered_branches}/{total + branches} combined lines/branches; "
                "requires strictly over 95%"
            )
        if module.name == "config_flow.py" and (
            covered != total
            or covered_branches != branches
            or entry.get("missing_branches")
            or entry.get("missing_lines")
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
    print("Coverage gate passed: every module >95% lines and combined; config flow 100%")


if __name__ == "__main__":
    main()
