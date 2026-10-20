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
    prepared = tmp_path / ".lab/prepared-2026.10.2.json"
    prepared.parent.mkdir()
    prepared.write_text(json.dumps(receipt))
    monkeypatch.setattr(lab, "command", lambda *a, **kw: pytest.fail("Docker must not start"))
    with pytest.raises(RuntimeError, match="Lab sources differ"):
        lab.run_lab(argparse.Namespace(ha_version="2026.10.2"))


def test_prepared_versions_and_artifacts_do_not_replace_each_others_image_tags():
    versions = [lab.image_references(version, "a" * 64) for version in lab.SUPPORTED]
    assert set(versions[0].values()).isdisjoint(
        lab.image_references("2026.10.3", "a" * 64).values()
    )
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
    receipt = lab.verify_cleanup("lab-2026-10-2-test")
    assert receipt["status"] == ("passed" if remaining is None else "failed")
    assert receipt["run_id"] == "lab-2026-10-2-test"
    assert receipt["completed_at"] > 0
    for key in ("containers", "networks", "volumes"):
        assert receipt[key] == (["residual-lab-object"] if key == remaining else [])
    assert all("--filter" in command for command in calls)
    assert all(command[-1].endswith("=lab-2026-10-2-test") for command in calls)


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


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "bad_archive",
        "foreign_container",
        "foreign_volume",
        "lingering_helper",
        "changed_files",
    ],
)
def test_package_swap_verifies_and_scopes_stopped_install(monkeypatch, tmp_path, fault):
    from scripts.release import build

    source = tmp_path / "source"
    component = source / "custom_components/ha_operator"
    (component / "brand").mkdir(parents=True)
    (component / "__init__.py").write_text("fixture = True\n")
    (component / "manifest.json").write_text(
        json.dumps({"domain": "ha_operator", "version": "0.3.0"})
    )
    (component / "brand/icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    manifest = build(source, tmp_path / "package")
    archive = tmp_path / "package/ha_operator.zip"
    if fault == "bad_archive":
        archive.write_bytes(b"invalid")
    commands = []
    monkeypatch.setattr(lab, "command", lambda args, **kw: commands.append(args))
    monkeypatch.setattr(
        lab, "output", lambda args: "leftover" if fault == "lingering_helper" else ""
    )

    def query(args):
        if args[1] == "inspect":
            return [
                {
                    "Config": {
                        "Labels": {
                            "io.ha-operator.lab.run": "foreign"
                            if fault == "foreign_container"
                            else "lab-test",
                            "io.ha-operator.lab.role": "ha",
                        }
                    },
                    "Mounts": [
                        {"Destination": "/config", "Type": "volume", "Name": "lab-test_ha_config"}
                    ],
                }
            ]
        if args[1:3] == ["volume", "inspect"]:
            return [
                {
                    "Labels": {
                        "com.docker.compose.project": "foreign"
                        if fault == "foreign_volume"
                        else "lab-test"
                    }
                }
            ]
        assert args[1] == "exec"
        return (
            {}
            if fault == "changed_files"
            else {key: value["sha256"] for key, value in manifest["files"].items()}
        )

    monkeypatch.setattr(lab, "json_output", query)
    arguments = (
        "ha-container",
        "lab-test",
        "sha256:cached-image",
        archive,
        archive.with_suffix(".manifest.json"),
        tmp_path,
        "image-digest",
    )
    if fault:
        with pytest.raises((RuntimeError, ValueError)):
            lab.swap_package(*arguments)
        if fault in {"bad_archive", "foreign_container", "foreign_volume"}:
            assert commands == []
        if fault == "lingering_helper":
            assert not any(args[1] == "start" for args in commands)
    else:
        receipt = lab.swap_package(*arguments)
        assert receipt["installed_file_hashes_verified"]
        assert commands[0][1] == "stop"
        helper = next(args for args in commands if args[1] == "run")
        assert helper[helper.index("--network") + 1] == "none"
        assert "--rm" in helper and "--read-only" in helper and "--pull=never" in helper
        assert helper.count("--mount") == 1
        assert helper[helper.index("--cap-drop") + 1] == "ALL"
        assert helper.count("--cap-add") == 1
        assert helper[helper.index("--cap-add") + 1] == "DAC_OVERRIDE"
        assert helper[helper.index("--security-opt") + 1] == "no-new-privileges"
        assert receipt["maintenance_container"]["capabilities"] == ["DAC_OVERRIDE"]
        assert commands[-1] == ["docker", "start", "ha-container"]


@pytest.mark.parametrize("fault", [None, "invalid_id", "foreign_lab", "foreign_domain"])
def test_migration_snapshot_only_reads_selected_synthetic_entry(monkeypatch, tmp_path, fault):
    from types import SimpleNamespace

    selected = {
        "entry_id": "entry123",
        "domain": "other" if fault == "foreign_domain" else "ha_operator",
        "version": 3,
    }
    monkeypatch.setattr(
        lab,
        "json_output",
        lambda args: [
            {
                "Config": {
                    "Labels": {
                        "io.ha-operator.lab.run": "foreign"
                        if fault == "foreign_lab"
                        else "lab-test",
                        "io.ha-operator.lab.role": "ha",
                    }
                }
            }
        ],
    )
    copied = []

    def command(args, **kwargs):
        copied.append(args[2])
        path = args[3]
        data = (
            {
                "data": {
                    "entries": [
                        {
                            "entry_id": "unrelated",
                            "domain": "secret",
                            "password": "must-not-return",
                        },
                        selected,
                    ]
                }
            }
            if args[2].endswith("core.config_entries")
            else {"data": {"manuals": {"resource": {"request_id": "stable"}}}}
        )
        from pathlib import Path

        Path(path).write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(lab, "command", command)
    identifier = "../auth" if fault == "invalid_id" else "entry123"
    if fault:
        with pytest.raises(RuntimeError):
            lab.migration_snapshot("container", identifier, tmp_path, "lab-test")
        if fault in {"invalid_id", "foreign_lab"}:
            assert copied == []
    else:
        snapshot = lab.migration_snapshot("container", identifier, tmp_path, "lab-test")
        assert snapshot["entry"] == selected
        assert "must-not-return" not in json.dumps(snapshot)
        assert copied == [
            "container:/config/.storage/core.config_entries",
            "container:/config/.storage/ha_operator.entry123",
        ]
        assert list(tmp_path.iterdir()) == []


def test_prepare_acquires_pinned_adaptive_source_from_clean_checkout(tmp_path, monkeypatch):
    import io
    import tarfile

    payload = io.BytesIO()
    prefix = f"adaptive-lighting-{lab.ADAPTIVE_COMMIT}/"
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, content in (
            ("LICENSE", b"Test upstream license"),
            ("custom_components/adaptive_lighting/manifest.json", b'{"version":"1.32.0"}'),
            ("custom_components/adaptive_lighting/__init__.py", b"upstream = True\n"),
        ):
            member = tarfile.TarInfo(prefix + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    data = payload.getvalue()
    requests = []

    def download(url, **kwargs):
        requests.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(lab, "ROOT", tmp_path)
    monkeypatch.setattr(lab, "ADAPTIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(lab, "urlopen", download)
    lab.prepare_adaptive_lighting()
    source = tmp_path / ".lab/adaptive-lighting"
    assert (source / "LICENSE").read_bytes() == b"Test upstream license"
    component = source / "adaptive_lighting/__init__.py"
    component.write_text("altered cache")
    lab.prepare_adaptive_lighting()
    assert component.read_bytes() == b"upstream = True\n"
    assert len(requests) == 1 and lab.ADAPTIVE_COMMIT in requests[0]
    assert json.loads((source / "source.json").read_text())["archive_sha256"] == lab.ADAPTIVE_SHA256
    (tmp_path / ".lab/adaptive-lighting-source.tar.gz").unlink()
    monkeypatch.setattr(lab, "ADAPTIVE_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        lab.prepare_adaptive_lighting()
    assert component.read_bytes() == b"upstream = True\n"
