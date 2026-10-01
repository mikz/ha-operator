"""Build and verify a deterministic, flat HACS integration release archive.

This command packages source; it never asserts tests or release gates passed.
Build once, transfer the ZIP and manifest together, then verify before each test.
"""

from __future__ import annotations

import argparse
import ast
import json
import stat
import subprocess
import tomllib
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath

DOMAIN = "ha_operator"
ARCHIVE_NAME = f"{DOMAIN}.zip"
MANIFEST_NAME = f"{DOMAIN}.manifest.json"
REPOSITORY = Path(__file__).resolve().parents[1]
ALLOWED_SUFFIXES = {".py", ".json", ".yaml", ".png", ".svg", ".webp", ".md"}
IGNORED_PARTS = {"__pycache__", "tests", "test", "sim", "simulator"}


def digest(data: bytes) -> str:
    """Return the content digest used by every release evidence consumer."""
    return sha256(data).hexdigest()


def component_digest(files: dict[str, bytes]) -> str:
    """Match the installed component fingerprint without ZIP metadata."""
    source = {
        name: digest(content)
        for name, content in files.items()
        if PurePosixPath(name).suffix in {".py", ".json", ".yaml"}
    }
    return digest(json.dumps(source, sort_keys=True, separators=(",", ":")).encode())


def integration_files(root: Path) -> dict[str, bytes]:
    """Read production files only; reject symlinks and ambiguous layouts."""
    components = root / "custom_components"
    names = sorted(
        p.name for p in components.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if names != [DOMAIN]:
        raise ValueError(f"Expected exactly custom_components/{DOMAIN}; found {names}")
    source = components / DOMAIN
    if source.is_symlink():
        raise ValueError("The integration directory must not be a symlink")
    files = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith(".") or part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink is not allowed in a release: {relative}")
        if not path.is_file():
            continue
        if path.suffix not in ALLOWED_SUFFIXES and path.name != "LICENSE":
            raise ValueError(f"Unexpected production file: {relative}")
        files[relative.as_posix()] = path.read_bytes()
    if (root / "LICENSE").is_file():
        files["LICENSE"] = (root / "LICENSE").read_bytes()
    validate_files(files)
    version = json.loads(files["manifest.json"])["version"]
    for project in (root / "pyproject.toml", *sorted(root.glob("compat/*/pyproject.toml"))):
        if project.exists() and tomllib.loads(project.read_text())["project"]["version"] != version:
            raise ValueError("Project and integration versions differ")
    if "const.py" in files:
        constants = ast.parse(files["const.py"])
        versions = [
            node.value.value
            for node in constants.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ]
        if versions != [version]:
            raise ValueError("Runtime and integration versions differ")
    return files


def validate_files(files: dict[str, bytes]) -> dict:
    """Reject incomplete or unsafe archives before handing them to the lab."""
    required = {"__init__.py", "manifest.json", "brand/icon.png"}
    if not required.issubset(files):
        raise ValueError(f"Missing required release files: {sorted(required - files.keys())}")
    for name in files:
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or str(path) != name
            or "\\" in name
            or any(part.startswith(".") or part in IGNORED_PARTS for part in path.parts)
            or (path.suffix not in ALLOWED_SUFFIXES and path.name != "LICENSE")
        ):
            raise ValueError(f"Unsafe or excluded archive member: {name}")
    manifest = json.loads(files["manifest.json"])
    if manifest.get("domain") != DOMAIN or not manifest.get("version"):
        raise ValueError("Release manifest has the wrong domain or no version")
    if manifest.get("config_flow") and "config_flow.py" not in files:
        raise ValueError("Manifest advertises a missing config flow")
    if not files["brand/icon.png"].startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Brand icon is not a PNG")
    return manifest


def archive_bytes(files: dict[str, bytes]) -> bytes:
    """Use stable ordering, modes, timestamps, and uncompressed content."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def build(root: Path, output: Path, *, replace: bool = False) -> dict:
    """Build the release once; replacing a changed artifact must be explicit."""
    files = integration_files(root)
    content = archive_bytes(files)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ARCHIVE_NAME
    if archive.exists() and archive.read_bytes() != content and not replace:
        raise ValueError("Release archive already differs; use a new output directory or --replace")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    locks = sorted(
        {*root.glob("*.lock"), *root.glob("requirements/*.lock"), *root.glob("compat/*/uv.lock")}
    )
    evidence = {
        "schema_version": 2,
        "domain": DOMAIN,
        "version": json.loads(files["manifest.json"])["version"],
        "archive": ARCHIVE_NAME,
        "sha256": digest(content),
        "component_sha256": component_digest(files),
        "source_commit": revision.stdout.strip() if revision.returncode == 0 else None,
        "files": {
            name: {"sha256": digest(data), "size": len(data)}
            for name, data in sorted(files.items())
        },
        "locks": {str(path.relative_to(root)): digest(path.read_bytes()) for path in locks},
    }
    manifest = output / MANIFEST_NAME
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if manifest.exists() and manifest.read_text() != rendered and not replace:
        raise ValueError(
            "Release manifest already differs; use a new output directory or --replace"
        )
    archive.write_bytes(content)
    manifest.write_text(rendered)
    return evidence


def verify(archive: Path, manifest: Path, *, root: Path | None = None) -> dict:
    """Verify transferred bytes and optionally ensure they match current source."""
    evidence = json.loads(manifest.read_text())
    content = archive.read_bytes()
    if evidence.get("schema_version") not in (1, 2) or evidence.get("domain") != DOMAIN:
        raise ValueError("Unknown release evidence schema or domain")
    if evidence.get("sha256") != digest(content) or evidence.get("archive") != archive.name:
        raise ValueError("Archive digest or filename differs from release manifest")
    with zipfile.ZipFile(BytesIO(content)) as package:
        entries = package.infolist()
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError("Duplicate archive members")
        if any(stat.S_ISLNK(item.external_attr >> 16) or item.is_dir() for item in entries):
            raise ValueError("Archive contains a directory or symlink")
        files = {entry.filename: package.read(entry) for entry in entries}
    integration = validate_files(files)
    actual = {name: {"sha256": digest(data), "size": len(data)} for name, data in files.items()}
    if evidence.get("files") != actual or evidence.get("version") != integration["version"]:
        raise ValueError("Archive member evidence or version differs")
    if evidence["schema_version"] == 2 and evidence.get("component_sha256") != component_digest(
        files
    ):
        raise ValueError("Installed component fingerprint differs from release manifest")
    if root is not None and files != integration_files(root):
        raise ValueError("Archive differs from current integration source")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--root", type=Path, default=REPOSITORY)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--against-source", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build(args.root, args.output, replace=args.replace)
        else:
            archive = args.artifact or args.output / ARCHIVE_NAME
            manifest = args.manifest or archive.with_name(MANIFEST_NAME)
            result = verify(archive, manifest, root=args.root if args.against_source else None)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Release {args.command} failed: {error}\n")
    print(json.dumps({"archive": result["archive"], "sha256": result["sha256"]}))


if __name__ == "__main__":
    main()
