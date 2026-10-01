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


@pytest.mark.parametrize("kind", ["manual", "timer"])
def test_kill_precondition_waits_for_matching_normal_store_save(monkeypatch, kind):
    probes = []
    readiness = iter([False, True])

    def inspect(args):
        probes.append(args)
        return next(readiness)

    monkeypatch.setattr(lab, "json_output", inspect)
    monkeypatch.setattr(lab.time, "sleep", lambda _: None)
    identity_key = "identity_hash" if kind == "timer" else "identity"
    expected = {"kind": kind, "key": "synthetic", identity_key: "episode", "expires_at": 100}
    result = lab.wait_saved_request("owned-lab-ha", "SyntheticEntry", expected)
    assert len(probes) == 2 and all(p[:3] == ["docker", "exec", "owned-lab-ha"] for p in probes)
    assert result[identity_key] == "episode" and result["expires_at"] == 100
    assert result["verified_at"] > 0


def test_kill_precondition_timeout_does_not_claim_saved_state(monkeypatch):
    monkeypatch.setattr(lab, "json_output", lambda _: False)
    monkeypatch.setattr(lab.time, "sleep", lambda _: None)
    expected = {"kind": "manual", "key": "synthetic", "identity": "request", "expires_at": 100}
    with pytest.raises(RuntimeError, match="did not reach native Store"):
        lab.wait_saved_request("owned-lab-ha", "SyntheticEntry", expected, timeout=0)


def test_timer_kill_requires_saved_accepted_phase(monkeypatch, tmp_path):
    import hashlib
    import subprocess
    import sys

    expected = {
        "kind": "timer",
        "key": "timer",
        "identity_hash": hashlib.sha256(b"episode").hexdigest()[:12],
        "expires_at": 100,
    }
    state = {
        "policy_inputs": {
            "timer": {"state": {"episode_id": "episode", "expires_at": 100, "phase": "qualifying"}}
        }
    }
    path = tmp_path / "ha_operator.SyntheticEntry"
    path.write_text(json.dumps({"data": state}))
    probes = []

    def inspect(args):
        probe = args[5].replace("/config/.storage", str(tmp_path))
        result = json.loads(subprocess.check_output([sys.executable, "-c", probe, *args[6:]]))
        probes.append(result)
        return result

    def ordinary_save(_):
        record = state["policy_inputs"]["timer"]["state"]
        record["phase"] = "accepted"
        record["episode_id"] = "different" if len(probes) == 1 else "episode"
        path.write_text(json.dumps({"data": state}))

    monkeypatch.setattr(lab, "json_output", inspect)
    monkeypatch.setattr(lab.time, "sleep", ordinary_save)
    result = lab.wait_saved_request("owned-lab-ha", "SyntheticEntry", expected)
    assert probes == [False, False, True]
    assert result["identity_hash"] == expected["identity_hash"] and result["expires_at"] == 100


def test_sleep_kill_requires_saved_detached_pair(monkeypatch, tmp_path):
    import subprocess
    import sys

    expected = {"kind": "sleep_pair", "central": "central", "room": "room"}
    state = {"intents": {"central": True, "room": True}}
    path = tmp_path / "ha_operator.SyntheticEntry"
    path.write_text(json.dumps({"data": state}))
    probes = []

    def inspect(args):
        probe = args[5].replace("/config/.storage", str(tmp_path))
        result = json.loads(subprocess.check_output([sys.executable, "-c", probe, *args[6:]]))
        probes.append(result)
        return result

    def ordinary_save(_):
        state["intents"]["room"] = False
        path.write_text(json.dumps({"data": state}))

    monkeypatch.setattr(lab, "json_output", inspect)
    monkeypatch.setattr(lab.time, "sleep", ordinary_save)
    result = lab.wait_saved_request("owned-lab-ha", "SyntheticEntry", expected)
    assert probes == [False, True]
    assert result["central"] == "central" and result["room"] == "room"
