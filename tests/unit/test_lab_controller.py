"""Host evidence must prove only the current lab's Docker objects were removed."""

import argparse
import hashlib
import json

import pytest

from scripts import lab


@pytest.mark.parametrize("change", ["edit", "add", "delete", "unbound_receipt"])
def test_runtime_rejects_stale_harness_before_docker(monkeypatch, tmp_path, change):
    monkeypatch.setattr(lab, "ROOT", tmp_path)
    for name in ("scripts/lab.py", "tests/__init__.py", "uv.lock", "tests/lab/recipe.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial")
    archive = tmp_path / "dist/ha_operator.zip"
    archive.parent.mkdir()
    archive.write_bytes(b"same release")
    receipt = {
        "artifact_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "lab_source_hashes": lab.lab_source_hashes(),
    }
    if change == "edit":
        (tmp_path / "tests/lab/recipe.py").write_text("new assertions")
    elif change == "add":
        (tmp_path / "tests/lab/new_scenario.py").write_text("new scenario")
    elif change == "delete":
        (tmp_path / "tests/lab/recipe.py").unlink()
    else:
        del receipt["lab_source_hashes"]
    prepared = tmp_path / ".lab/prepared-2026.9.3.json"
    prepared.parent.mkdir()
    prepared.write_text(json.dumps(receipt))
    monkeypatch.setattr(lab, "command", lambda *a, **kw: pytest.fail("Docker must not start"))
    with pytest.raises(RuntimeError, match="Lab sources differ"):
        lab.run_lab(argparse.Namespace(ha_version="2026.9.3"))


def test_prepared_versions_and_artifacts_do_not_replace_each_others_image_tags():
    versions = [lab.image_references(version, "a" * 64) for version in lab.SUPPORTED]
    assert set(versions[0].values()).isdisjoint(versions[1].values())
    assert set(versions[0].values()).isdisjoint(
        lab.image_references(lab.SUPPORTED[0], "b" * 64).values()
    )
    assert set(versions[0]) == {"ha", "simulator", "runner"}


@pytest.mark.parametrize("remaining", [None, "containers", "networks", "volumes"])
def test_cleanup_receipt_queries_scoped_objects(monkeypatch, remaining):
    calls = []

    def output(args):
        calls.append(args)
        kind = {"ps": "containers", "network": "networks", "volume": "volumes"}[args[1]]
        return "residual-lab-object" if kind == remaining else ""

    monkeypatch.setattr(lab, "output", output)
    receipt = lab.verify_cleanup("lab-2026-9-3-test")
    assert receipt["status"] == ("passed" if remaining is None else "failed")
    assert receipt["run_id"] == "lab-2026-9-3-test"
    assert receipt["completed_at"] > 0
    for key in ("containers", "networks", "volumes"):
        assert receipt[key] == (["residual-lab-object"] if key == remaining else [])
    assert all("--filter" in command for command in calls)
    assert all(command[-1].endswith("=lab-2026-9-3-test") for command in calls)
