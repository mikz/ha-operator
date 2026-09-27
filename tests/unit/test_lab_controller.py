"""Host evidence must prove only the current lab's Docker objects were removed."""

import pytest

from scripts import lab


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
