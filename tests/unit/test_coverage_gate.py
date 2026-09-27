"""Coverage is checked per file, never hidden by the aggregate percentage."""

from scripts.coverage_gate import PREFIX, check_coverage


def report(covered=96, total=100, **kwargs):
    return {"summary": {"covered_lines": covered, "num_statements": total}, **kwargs}


def source(tmp_path, *names):
    for name in names:
        path = tmp_path / PREFIX / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n")
    return tmp_path


def test_requires_strictly_over_95_percent_per_module(tmp_path):
    root = source(tmp_path, "engine.py", "storage.py")
    data = {"files": {PREFIX + "engine.py": report(100), PREFIX + "storage.py": report(95)}}
    assert check_coverage(data, root) == [
        PREFIX + "storage.py: 95/100 lines; requires strictly over 95%"
    ]
    data["files"][PREFIX + "storage.py"] = report(96)
    assert check_coverage(data, root) == []


def test_missing_modules_are_not_ignored(tmp_path):
    root = source(tmp_path, "new_module.py")
    assert "absent" in check_coverage({"files": {}}, root)[0]


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
    assert "full line and branch" in check_coverage(data, root)[0]


def test_rejects_excluded_lines_and_accepts_absolute_report_paths(tmp_path):
    root = source(tmp_path, "engine.py")
    data = {"files": {str(root / PREFIX / "engine.py"): report(excluded_lines=[10])}}
    assert "excluded executable" in check_coverage(data, root)[0]


def test_only_typing_imports_can_be_excluded(tmp_path):
    root = source(tmp_path, "engine.py")
    module = root / PREFIX / "engine.py"
    module.write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import example\n")
    data = {"files": {PREFIX + "engine.py": report(excluded_lines=[2, 3])}}
    assert check_coverage(data, root) == []
    module.write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    do_real_work()\n")
    assert "excluded executable" in check_coverage(data, root)[0]


def test_no_source_and_invalid_counts_fail(tmp_path):
    assert "No production" in check_coverage({}, tmp_path)[0]
    root = source(tmp_path, "engine.py")
    assert "invalid" in check_coverage({"files": {PREFIX + "engine.py": report(101)}}, root)[0]
    assert "invalid" in check_coverage({"files": {PREFIX + "engine.py": {}}}, root)[0]


def test_empty_module_needs_report_but_no_statement_percentage(tmp_path):
    root = source(tmp_path, "__init__.py")
    assert check_coverage({"files": {PREFIX + "__init__.py": report(0, 0)}}, root) == []
