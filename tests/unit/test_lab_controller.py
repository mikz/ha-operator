"""Host evidence must prove only the current lab's Docker objects were removed."""

import pytest

from scripts import lab


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
