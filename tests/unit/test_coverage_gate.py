"""Coverage is checked per file, never hidden by the aggregate percentage."""

import pytest

from scripts.coverage_gate import PREFIX, check_coverage, typing_only_lines


def report(covered=96, total=100, *, branches=0, covered_branches=0, **kwargs):
    return {
        "summary": {
            "covered_lines": covered,
            "num_statements": total,
            "num_branches": branches,
            "covered_branches": covered_branches,
            "missing_branches": branches - covered_branches,
        },
        **kwargs,
    }


def source(tmp_path, *names):
    for name in names:
        path = tmp_path / PREFIX / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n")
    return tmp_path


def test_requires_strictly_over_95_percent_per_module(tmp_path):
    root = source(tmp_path, "engine.py", "storage.py")
    data = {
        "meta": {"branch_coverage": True},
        "files": {PREFIX + "engine.py": report(100), PREFIX + "storage.py": report(95)},
    }
    assert check_coverage(data, root) == [
        PREFIX + "storage.py: 95/100 lines; requires strictly over 95%"
    ]
    data["files"][PREFIX + "storage.py"] = report(96)
    assert check_coverage(data, root) == []


def test_missing_modules_are_not_ignored(tmp_path):
    root = source(tmp_path, "new_module.py")
    assert "absent" in check_coverage({"meta": {"branch_coverage": True}, "files": {}}, root)[0]


def test_config_flow_requires_every_line_and_branch(tmp_path):
    root = source(tmp_path, "config_flow.py")
    data = {
        "meta": {"branch_coverage": True},
        "files": {PREFIX + "config_flow.py": report(99)},
    }
    assert "full line and branch" in check_coverage(data, root)[0]
    data["files"][PREFIX + "config_flow.py"] = report(100, missing_branches=[[5, 8]])
    assert "full line and branch" in check_coverage(data, root)[0]
    data["files"][PREFIX + "config_flow.py"] = report(100)
    assert check_coverage(data, root) == []
    data["meta"]["branch_coverage"] = False
    assert "Branch coverage metadata" in check_coverage(data, root)[0]


def test_rejects_excluded_lines_and_accepts_absolute_report_paths(tmp_path):
    root = source(tmp_path, "engine.py")
    data = {
        "meta": {"branch_coverage": True},
        "files": {str(root / PREFIX / "engine.py"): report(excluded_lines=[10])},
    }
    assert "excluded executable" in check_coverage(data, root)[0]


def test_only_typing_imports_can_be_excluded(tmp_path):
    root = source(tmp_path, "engine.py")
    module = root / PREFIX / "engine.py"
    module.write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import example\n")
    data = {
        "meta": {"branch_coverage": True},
        "files": {PREFIX + "engine.py": report(excluded_lines=[2, 3])},
    }
    assert check_coverage(data, root) == []
    module.write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    do_real_work()\n")
    assert "excluded executable" in check_coverage(data, root)[0]


def test_no_source_and_invalid_counts_fail(tmp_path):
    assert "No production" in check_coverage({}, tmp_path)[0]
    root = source(tmp_path, "engine.py")
    assert (
        "invalid"
        in check_coverage(
            {"meta": {"branch_coverage": True}, "files": {PREFIX + "engine.py": report(101)}}, root
        )[0]
    )
    assert (
        "invalid"
        in check_coverage(
            {"meta": {"branch_coverage": True}, "files": {PREFIX + "engine.py": {}}}, root
        )[0]
    )


def test_empty_module_needs_report_but_no_statement_percentage(tmp_path):
    root = source(tmp_path, "__init__.py")
    assert (
        check_coverage(
            {"meta": {"branch_coverage": True}, "files": {PREFIX + "__init__.py": report(0, 0)}},
            root,
        )
        == []
    )


def test_combined_coverage_cannot_hide_untested_branches(tmp_path):
    root = source(tmp_path, "engine.py")
    data = {
        "meta": {"branch_coverage": True},
        "files": {PREFIX + "engine.py": report(100, branches=100, covered_branches=90)},
    }
    assert "190/200 combined" in check_coverage(data, root)[0]
    data["files"][PREFIX + "engine.py"] = report(100, branches=100, covered_branches=91)
    assert check_coverage(data, root) == []


def test_global_branch_metadata_is_required_for_every_module(tmp_path):
    root = source(tmp_path, "engine.py")
    for metadata in ({}, {"branch_coverage": False}, {"branch_coverage": 1}):
        data = {"meta": metadata, "files": {PREFIX + "engine.py": report()}}
        assert "Branch coverage metadata" in check_coverage(data, root)[0]


def test_branch_counts_must_be_complete_and_consistent(tmp_path):
    root = source(tmp_path, "engine.py")
    for changes in (
        {"num_branches": None},
        {"num_branches": True},
        {"num_branches": -1},
        {"covered_branches": -1},
        {"missing_branches": -1},
        {"num_branches": 3, "covered_branches": 4},
        {"num_branches": 3, "covered_branches": 2, "missing_branches": 0},
    ):
        entry = report()
        entry["summary"].update(changes)
        data = {"meta": {"branch_coverage": True}, "files": {PREFIX + "engine.py": entry}}
        assert "invalid branch coverage counts" in check_coverage(data, root)[0]


@pytest.mark.parametrize("scope", ["module", "class"])
def test_inert_overloads_with_same_scope_implementation_are_typing_only(scope):
    source = (
        "@overload\ndef example(value: str, limit: int = 0) -> str: ...\n"
        "def example(value, limit=0):\n    return value\n"
    )
    if scope == "class":
        source = "class Example:\n" + "".join("    " + line for line in source.splitlines(True))
    allowed = typing_only_lines(source)
    assert allowed == ({1, 2} if scope == "module" else {2, 3})


@pytest.mark.parametrize(
    "declaration",
    [
        "@overload\ndef example(value):\n    do_real_work()\n",
        "@overload\ndef example(value):\n    return None\n",
        "@overload\ndef example(value=do_real_work()): ...\n",
        "@overload\n@other_decorator\ndef example(value): ...\n",
        "@overload\ndef example(value: do_real_work()): ...\n",
    ],
)
def test_overload_exclusions_cannot_hide_executable_logic(declaration):
    assert typing_only_lines(declaration + "def example(value):\n    return value\n") == set()


@pytest.mark.parametrize(
    "source",
    [
        "@overload\ndef example(value): ...\n",
        "@overload\ndef example(value): ...\ndef example(value): ...\n",
        "@overload\ndef example(value): ...\n"
        "class Other:\n    def example(value):\n        return value\n",
    ],
)
def test_orphan_overloads_and_other_scope_implementations_are_rejected(source):
    assert typing_only_lines(source) == set()


def test_coverage_stub_exclusion_may_include_only_blank_separators():
    source = "@overload\ndef example(value): ...\n\n\ndef example(value):\n    return value\n"
    assert typing_only_lines(source) == {1, 2, 3, 4}
