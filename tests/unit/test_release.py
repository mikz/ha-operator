"""Release bytes are reproducible, complete, and independently verified."""

import json
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.release import (
    ARCHIVE_NAME,
    MANIFEST_NAME,
    build,
    component_digest,
    integration_files,
    verify,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "repository"
    integration = root / "custom_components/ha_operator"
    (integration / "brand").mkdir(parents=True)
    (integration / "__init__.py").write_text('"""Example."""\n')
    (integration / "manifest.json").write_text(
        json.dumps({"domain": "ha_operator", "version": "0.1.0"})
    )
    (integration / "brand/icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    return root


def test_reproducible_flat_archive_and_source_verification(source, tmp_path):
    output = tmp_path / "dist"
    first = build(source, output)
    content = (output / ARCHIVE_NAME).read_bytes()
    second = build(source, output)
    assert first == second
    assert content == (output / ARCHIVE_NAME).read_bytes()
    assert first["sha256"] == sha256(content).hexdigest()
    assert first["schema_version"] == 2
    assert first["component_sha256"] == component_digest(integration_files(source))
    assert verify(output / ARCHIVE_NAME, output / MANIFEST_NAME, root=source) == first
    with zipfile.ZipFile(output / ARCHIVE_NAME) as archive:
        assert set(archive.namelist()) == {"__init__.py", "manifest.json", "brand/icon.png"}
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_changed_artifact_requires_explicit_rebuild(source, tmp_path):
    output = tmp_path / "dist"
    build(source, output)
    (source / "custom_components/ha_operator/__init__.py").write_text("changed = True\n")
    with pytest.raises(ValueError, match="differs"):
        verify(output / ARCHIVE_NAME, output / MANIFEST_NAME, root=source)
    with pytest.raises(ValueError, match="already differs"):
        build(source, output)
    build(source, output, replace=True)
    verify(output / ARCHIVE_NAME, output / MANIFEST_NAME, root=source)


def test_corrupted_archive_or_evidence_is_rejected(source, tmp_path):
    output = tmp_path / "dist"
    result = build(source, output)
    result["files"]["__init__.py"]["sha256"] = "incorrect"
    (output / MANIFEST_NAME).write_text(json.dumps(result))
    with pytest.raises(ValueError, match="member evidence"):
        verify(output / ARCHIVE_NAME, output / MANIFEST_NAME)
    build(source, output, replace=True)
    with (output / ARCHIVE_NAME).open("ab") as archive:
        archive.write(b"tampered")
    with pytest.raises(ValueError, match="digest"):
        verify(output / ARCHIVE_NAME, output / MANIFEST_NAME)


def test_excludes_caches_and_simulator_code(source):
    production = source / "custom_components/ha_operator"
    for name in ["__pycache__/test.pyc", "tests/test_bad.py", "sim/server.py", ".hidden"]:
        path = production / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("not production")
    simulator = source / "tests/lab/custom_components/ha_operator_sim"
    simulator.mkdir(parents=True)
    (simulator / "__init__.py").write_text("test_only = True")
    assert set(integration_files(source)) == {"__init__.py", "manifest.json", "brand/icon.png"}


def test_symlinks_and_extra_integrations_are_rejected(source):
    production = source / "custom_components/ha_operator"
    (production / "external.py").symlink_to(Path(__file__))
    with pytest.raises(ValueError, match="Symlink"):
        integration_files(source)
    (production / "external.py").unlink()
    (source / "custom_components/other").mkdir()
    with pytest.raises(ValueError, match="exactly"):
        integration_files(source)


def test_package_rejects_incomplete_or_wrong_metadata(source):
    production = source / "custom_components/ha_operator"
    (production / "manifest.json").write_text(
        json.dumps({"domain": "ha_operator", "version": "0.1.0", "config_flow": True})
    )
    with pytest.raises(ValueError, match="missing config flow"):
        integration_files(source)
    (production / "config_flow.py").write_text("pass\n")
    (source / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
    with pytest.raises(ValueError, match="versions differ"):
        integration_files(source)


def test_dependency_locks_are_hashed(source, tmp_path):
    (source / "uv.lock").write_text("lock fixture")
    compatibility = source / "compat/ha2026.9.4"
    compatibility.mkdir(parents=True)
    (compatibility / "uv.lock").write_text("compat fixture")
    unrelated = compatibility / ".venv/package/unrelated.lock"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("exclude installed dependency internals")
    build_result = build(source, tmp_path / "dist")
    assert build_result["locks"] == {
        "uv.lock": sha256(b"lock fixture").hexdigest(),
        "compat/ha2026.9.4/uv.lock": sha256(b"compat fixture").hexdigest(),
    }


def test_unchanged_archive_cannot_silently_relabel_dependency_lock(source, tmp_path):
    output = tmp_path / "dist"
    lock = source / "uv.lock"
    lock.write_text("first lock")
    original = build(source, output)
    archive = (output / ARCHIVE_NAME).read_bytes()
    receipt = (output / MANIFEST_NAME).read_bytes()
    lock.write_text("changed lock")
    with pytest.raises(ValueError, match="manifest already differs"):
        build(source, output)
    assert (output / ARCHIVE_NAME).read_bytes() == archive
    assert (output / MANIFEST_NAME).read_bytes() == receipt
    replaced = build(source, output, replace=True)
    assert replaced["sha256"] == original["sha256"]
    assert replaced["locks"] != original["locks"]


def test_runtime_and_compatibility_versions_must_match_release(source):
    production = source / "custom_components/ha_operator"
    (production / "const.py").write_text('VERSION = "0.0.9"\n')
    with pytest.raises(ValueError, match="Runtime and integration versions differ"):
        integration_files(source)
    (production / "const.py").write_text('VERSION = "0.1.0"\n')
    compatibility = source / "compat/ha2026.9.4"
    compatibility.mkdir(parents=True)
    (compatibility / "pyproject.toml").write_text('[project]\nversion = "0.0.9"\n')
    with pytest.raises(ValueError, match="Project and integration versions differ"):
        integration_files(source)
    (compatibility / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    assert "const.py" in integration_files(source)


def test_component_fingerprint_covers_code_configuration_and_excludes_branding():
    files = {"__init__.py": b"source", "translations/en.json": b"{}", "services.yaml": b"{}"}
    hashes = {name: sha256(content).hexdigest() for name, content in files.items()}
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    expected = sha256(encoded).hexdigest()
    assert component_digest(files) == expected
    decorated = {**files, "brand/icon.png": b"image", "LICENSE": b"license"}
    assert component_digest(decorated) == expected
    assert component_digest({**files, "services.yaml": b"changed"}) != expected


def test_new_manifest_requires_component_fingerprint_but_legacy_manifests_remain_readable(source):
    output = source / "dist"
    evidence = build(source, output)
    evidence["component_sha256"] = "incorrect"
    (output / MANIFEST_NAME).write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="component fingerprint differs"):
        verify(output / ARCHIVE_NAME, output / MANIFEST_NAME)
    del evidence["component_sha256"]
    evidence["schema_version"] = 1
    (output / MANIFEST_NAME).write_text(json.dumps(evidence))
    assert verify(output / ARCHIVE_NAME, output / MANIFEST_NAME) == evidence


@pytest.mark.parametrize("name", ["../escape.py", "/escape.py", "a\\escape.py", "tests/evil.py"])
def test_rejects_unsafe_members_even_when_evidence_matches(source, tmp_path, name):
    output = tmp_path / "dist"
    evidence = build(source, output)
    archive = output / ARCHIVE_NAME
    with zipfile.ZipFile(archive, "a") as package:
        package.writestr(name, "injected")
    evidence["sha256"] = sha256(archive.read_bytes()).hexdigest()
    (output / MANIFEST_NAME).write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="Unsafe"):
        verify(archive, output / MANIFEST_NAME)
