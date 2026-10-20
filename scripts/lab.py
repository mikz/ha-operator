#!/usr/bin/env python3
"""Prepare and exercise a release in a private, disposable Home Assistant lab."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.lab.isolation import validate_compose, validate_inspect, validate_routes  # noqa: E402
from tests.lab.redaction import sanitize_artifacts  # noqa: E402

SUPPORTED = ("2026.10.2",)


def command(args, *, env=None, capture=True, timeout=900, check=True):
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=capture,
        check=check,
        timeout=timeout,
    )


def output(args, **kwargs):
    return command(args, **kwargs).stdout.strip()


def json_output(args, **kwargs):
    return json.loads(output(args, **kwargs))


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(value)
    temporary.replace(path)


def write_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def image_id(reference):
    return output(["docker", "image", "inspect", "--format", "{{.Id}}", reference])


def pinned_image(reference):
    command(["docker", "pull", reference], capture=False)
    data = json_output(["docker", "image", "inspect", reference])[0]
    return data["RepoDigests"][0]


def image_references(ha_version, digest):
    """Keep each prepared version reachable when another version is prepared."""
    return {
        role: f"ha-operator-lab-{role}:{ha_version}-{digest[:12]}"
        for role in ("ha", "simulator", "runner")
    }


def lab_source_hashes():
    """Bind prepared images to the harness that runtime will actually execute."""
    files = [ROOT / "scripts/lab.py", ROOT / "tests/__init__.py", ROOT / "uv.lock"]
    files.extend(
        path
        for path in (ROOT / "tests/lab").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }


def release_metadata():
    """Carry the exact release version and manifest hash in lab output."""
    content = (ROOT / "dist/ha_operator.manifest.json").read_bytes()
    manifest = json.loads(content)
    return {
        "integration_version": manifest["version"],
        "release_manifest_sha256": hashlib.sha256(content).hexdigest(),
    }


def prepare(args):
    """Only this phase is allowed to fetch/build dependencies."""
    command([sys.executable, "scripts/release.py", "verify"], capture=False)
    archive = ROOT / "dist/ha_operator.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    prepared_dir = ROOT / ".lab"
    prepared_dir.mkdir(exist_ok=True)
    sources = lab_source_hashes()
    requirements = output(
        [
            "uv",
            "export",
            "--locked",
            "--only-group",
            "lab",
            "--no-hashes",
            "--no-emit-project",
            "--format",
            "requirements.txt",
        ]
    )
    (prepared_dir / "requirements-lab.txt").write_text(requirements + "\n")
    (prepared_dir / "requirements-sim.txt").write_text(requirements + "\n")
    ha_base = pinned_image(f"ghcr.io/home-assistant/home-assistant:{args.ha_version}")
    python_base = pinned_image("python:3.14.2-slim-bookworm")
    references = image_references(args.ha_version, digest)
    for role, reference in references.items():
        command(
            [
                "docker",
                "build",
                "--pull=false",
                "-f",
                f"tests/lab/Dockerfile.{role}",
                "--build-arg",
                f"HA_BASE={ha_base}",
                "--build-arg",
                f"PYTHON_BASE={python_base}",
                "-t",
                reference,
                ".",
            ],
            capture=False,
            timeout=1800,
        )
    if sources != lab_source_hashes():
        raise RuntimeError("Lab sources changed during image preparation; run prepare again")
    receipt = {
        "ha_version": args.ha_version,
        **release_metadata(),
        "artifact_sha256": digest,
        "base_images": {"ha": ha_base, "python": python_base},
        "images": {role: image_id(ref) for role, ref in references.items()},
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "lab_source_hashes": sources,
        "prepared_at": time.time(),
    }
    write_json(prepared_dir / f"prepared-{args.ha_version}.json", receipt)
    print(json.dumps(receipt, indent=2))


def select_subnet():
    """Use a nonoverlapping private subnet, never an existing external network."""
    ids = output(["docker", "network", "ls", "-q"]).splitlines()
    existing = json_output(["docker", "network", "inspect", *ids]) if ids else []
    used = [
        ipaddress.ip_network(config["Subnet"])
        for network in existing
        for config in network.get("IPAM", {}).get("Config") or []
        if config.get("Subnet")
    ]
    candidates = list(range(16, 240))
    secrets.SystemRandom().shuffle(candidates)
    for third in candidates:
        subnet = ipaddress.ip_network(f"172.30.{third}.0/24")
        if not any(subnet.overlaps(item) for item in used if item.version == 4):
            return str(subnet), str(subnet.network_address + 10)
    raise RuntimeError("No unused lab subnet available")


def wait_saved_request(container, entry_id, expected, *, timeout=15):
    """Wait for this lab request's ordinary Store save before SIGKILL."""
    if not entry_id.isalnum() or expected["kind"] not in {"manual", "timer", "sleep_pair"}:
        raise RuntimeError("Invalid lab Store precondition")
    probe = """import hashlib, json, sys
from pathlib import Path
path = Path('/config/.storage') / ('ha_operator.' + sys.argv[1])
expected = json.loads(sys.argv[2])
try:
    state = json.loads(path.read_text())['data']
    if expected['kind'] == 'sleep_pair':
        ready = (state['intents'].get(expected['central']) is True
                 and state['intents'].get(expected['room']) is False)
    elif expected['kind'] == 'manual':
        record = state['manuals'].get(expected['key'], {})
        identity = record.get('request_id')
    else:
        record = state['policy_inputs'].get(expected['key'], {}).get('state', {})
        identity = record.get('episode_id')
        identity_hash = hashlib.sha256(str(identity).encode()).hexdigest()[:12]
        ready = (identity_hash == expected['identity_hash']
                 and record.get('expires_at') == expected['expires_at']
                 and record.get('phase') == 'accepted')
    if expected['kind'] == 'manual':
        ready = (identity == expected['identity']
                 and record.get('expires_at') == expected['expires_at'])
except (OSError, ValueError, KeyError):
    ready = False
print(json.dumps(ready))
"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if json_output(
            ["docker", "exec", container, "python3", "-c", probe, entry_id, json.dumps(expected)]
        ):
            return {"verified_at": time.time(), **expected}
        time.sleep(0.1)
    raise RuntimeError("Expected lab request did not reach native Store before SIGKILL")


def migration_snapshot(container, entry_id, staging, run_id):
    """Read this synthetic entry and its Store; never copy authentication storage."""
    if not isinstance(entry_id, str) or not entry_id.isalnum():
        raise RuntimeError("Invalid migration entry identity")
    inspected = json_output(["docker", "inspect", container])[0]
    labels = inspected["Config"].get("Labels", {})
    if (
        labels.get("io.ha-operator.lab.run") != run_id
        or labels.get("io.ha-operator.lab.role") != "ha"
    ):
        raise RuntimeError("Migration snapshot target identity changed")
    with tempfile.TemporaryDirectory(prefix="migration-snapshot-", dir=staging) as temporary:
        root = Path(temporary)
        entries_path = root / "config_entries.json"
        command(
            ["docker", "cp", f"{container}:/config/.storage/core.config_entries", str(entries_path)]
        )
        entries = json.loads(entries_path.read_text())["data"]["entries"]
        entry = next(item for item in entries if item["entry_id"] == entry_id)
        if entry.get("domain") != "ha_operator":
            raise RuntimeError("Migration snapshot must select the lab Operator entry")
        store_path = root / "operator.json"
        copied = command(
            [
                "docker",
                "cp",
                f"{container}:/config/.storage/ha_operator.{entry_id}",
                str(store_path),
            ],
            check=False,
        )
        store = json.loads(store_path.read_text()) if copied.returncode == 0 else None
    return {"entry": entry, "operator_store": store}


def verify_installed_package(container, archive, manifest):
    """Check the full installed integration file set, excluding generated bytecode."""
    from scripts.release import verify

    evidence = verify(archive, manifest)
    code = (
        "from pathlib import Path; import hashlib,json; "
        "r=Path('/config/custom_components/ha_operator'); "
        "print(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() "
        "for p in r.rglob('*') if p.is_file() and '__pycache__' not in p.parts}))"
    )
    installed = json_output(["docker", "exec", container, "python3", "-c", code])
    if installed != {key: value["sha256"] for key, value in evidence["files"].items()}:
        raise RuntimeError("Installed package file hashes differ after restart")
    return {
        "sha256": evidence["sha256"],
        "version": evidence["version"],
        "installed_file_hashes_verified": True,
    }


def swap_package(container, run_id, image, archive, manifest, staging, image_digest):
    """Install one verified lab package while HA is stopped; preserve its config/Store."""
    from scripts.release import verify

    evidence = verify(archive, manifest)
    inspected = json_output(["docker", "inspect", container])[0]
    labels = inspected["Config"].get("Labels", {})
    if (
        labels.get("io.ha-operator.lab.run") != run_id
        or labels.get("io.ha-operator.lab.role") != "ha"
    ):
        raise RuntimeError("Package target identity changed")
    mounts = [
        item
        for item in inspected["Mounts"]
        if item["Destination"] == "/config" and item["Type"] == "volume"
    ]
    if len(mounts) != 1:
        raise RuntimeError("Package target must have exactly one named config volume")
    volume = mounts[0]["Name"]
    metadata = json_output(["docker", "volume", "inspect", volume])[0]
    if metadata.get("Labels", {}).get("com.docker.compose.project") != run_id:
        raise RuntimeError("Package config volume belongs to another lab")
    # Verification and safe extraction finish before the running HA is touched.
    with tempfile.TemporaryDirectory(prefix="package-", dir=staging) as temporary:
        root = Path(temporary)
        component = root / "ha_operator"
        component.mkdir()
        with zipfile.ZipFile(archive) as package:
            package.extractall(component)
        if any(path.is_symlink() for path in component.rglob("*")):
            raise RuntimeError("Extracted package contains a symlink")
        override = root / ".lab-package-swap.json"
        override.write_text(
            json.dumps({"image_sha256": image_digest, "installed_sha256": evidence["sha256"]})
        )
        receipt = root / ".lab-artifact-sha256"
        receipt.write_text(evidence["sha256"] + "\n")
        command(["docker", "stop", "--time", "60", container], timeout=90)
        helper = run_id + "-package-install"
        cleanup_code = (
            "from pathlib import Path; import shutil; "
            "parent=Path('/config/custom_components'); "
            "\nif parent.is_symlink() or not parent.is_dir(): "
            "raise RuntimeError('Unsafe custom_components path')\n"
            "p=parent/'ha_operator'; "
            "p.unlink() if p.is_symlink() else shutil.rmtree(p) if p.exists() else None"
        )
        command(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--name",
                helper,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                # docker cp can retain foreign ownership; all other capabilities stay dropped.
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges",
                "--label",
                f"io.ha-operator.lab.run={run_id}",
                "--label",
                "io.ha-operator.lab.role=package-install",
                "--mount",
                f"type=volume,source={volume},target=/config",
                "--entrypoint",
                "python3",
                image,
                "-c",
                cleanup_code,
            ],
            capture=False,
        )
        if output(["docker", "ps", "-aq", "--filter", f"name=^{helper}$"]):
            raise RuntimeError("Package installation helper survived removal")
        command(
            ["docker", "cp", str(component), f"{container}:/config/custom_components/ha_operator"]
        )
        for path in (override, receipt):
            command(["docker", "cp", str(path), f"{container}:/config/{path.name}"])
        command(["docker", "start", container])
        verify_installed_package(container, archive, manifest)
    return {
        "sha256": evidence["sha256"],
        "version": evidence["version"],
        "installed_file_hashes_verified": True,
        "maintenance_container": {
            "name": helper,
            "network": "none",
            "capabilities": ["DAC_OVERRIDE"],
            "config_volume": volume,
            "removed": True,
        },
        "preserved": "same HA config volume, config entries, Store and independent simulator",
    }


def collect_route_snapshot(container):
    fields = {
        "addresses": ["ip", "-j", "address", "show"],
        "ipv4_routes": ["ip", "-j", "-4", "route", "show", "table", "all"],
        "ipv6_routes": ["ip", "-j", "-6", "route", "show", "table", "all"],
        "ipv4_rules": ["ip", "-j", "-4", "rule", "show"],
        "ipv6_rules": ["ip", "-j", "-6", "rule", "show"],
    }
    return {
        name: json_output(["docker", "exec", container, *args]) for name, args in fields.items()
    }


def verify_cleanup(run_id):
    """Query scoped Docker objects after teardown; a successful down is insufficient."""
    containers = output(
        ["docker", "ps", "-aq", "--filter", f"label=io.ha-operator.lab.run={run_id}"]
    ).splitlines()
    networks = output(
        ["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={run_id}"]
    ).splitlines()
    volumes = output(
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={run_id}"]
    ).splitlines()
    receipt = {
        "run_id": run_id,
        "status": "failed" if containers or networks or volumes else "passed",
        "completed_at": time.time(),
        "containers": containers,
        "networks": networks,
        "volumes": volumes,
    }
    return receipt


def run_lab(args):
    """No pull/build/network fallback is permitted in this phase."""
    receipt = json.loads((ROOT / ".lab" / f"prepared-{args.ha_version}.json").read_text())
    archive_digest = hashlib.sha256((ROOT / "dist/ha_operator.zip").read_bytes()).hexdigest()
    if archive_digest != receipt["artifact_sha256"]:
        raise RuntimeError("Release differs from prepared image; run prepare again")
    if receipt.get("lab_source_hashes") != lab_source_hashes():
        raise RuntimeError("Lab sources differ from prepared images; run prepare again")
    metadata = release_metadata()
    if any(receipt.get(key) != value for key, value in metadata.items()):
        raise RuntimeError("Release manifest differs from prepared images; run prepare again")
    for image in receipt["images"].values():
        if image_id(image) != image:
            raise RuntimeError("Prepared image unavailable")
    engine = output(["docker", "version", "--format", "{{.Server.Version}}"])
    if tuple(int(x) for x in engine.split(".")[:2]) < (29, 4):
        raise RuntimeError("Docker Engine >=29.4 is required for the verified isolation baseline")
    run_id = f"lab-{args.ha_version.replace('.', '-')}-{secrets.token_hex(4)}"
    published = ROOT / "artifacts/lab" / run_id
    staging = ROOT / ".lab/runs" / run_id
    artifacts = staging / "artifacts"
    control = staging / "control"
    staging.mkdir(parents=True, mode=0o700)
    control.mkdir(mode=0o700)
    artifacts.mkdir(mode=0o700)
    subnet, ha_address = select_subnet()
    env = dict(
        os.environ,
        **{
            "LAB_RUN_ID": run_id,
            "LAB_HA_VERSION": args.ha_version,
            "LAB_ARTIFACT_DIR": str(artifacts),
            "LAB_CONTROL_DIR": str(control),
            "LAB_SUBNET": subnet,
            "LAB_HA_ADDRESS": ha_address,
            "LAB_SCENARIO": args.scenario,
            "LAB_ARTIFACT_SHA256": archive_digest,
            "HA_LAB_IMAGE": receipt["images"]["ha"],
            "SIM_LAB_IMAGE": receipt["images"]["simulator"],
            "RUNNER_LAB_IMAGE": receipt["images"]["runner"],
        },
    )
    compose = ["docker", "compose", "--project-name", run_id, "-f", "tests/lab/compose.yaml"]
    effective = json_output([*compose, "config", "--format", "json"], env=env)
    allowed_binds = (str(control), str(artifacts))
    validate_compose(effective, allowed_bind_mounts=allowed_binds)
    write_json(artifacts / "compose.json", effective)
    write_json(artifacts / "prepared.json", receipt)
    started = time.time()
    result = {
        "run_id": run_id,
        "status": "failed",
        "started_at": started,
        "ha_version": args.ha_version,
        **metadata,
        "artifact_sha256": archive_digest,
        "docker_engine": engine,
        "scenario": args.scenario,
    }
    containers = {}
    try:
        command(
            [*compose, "up", "--detach", "--no-build", "--pull", "never"],
            env=env,
            capture=False,
            timeout=120,
        )
        containers = {
            role: output([*compose, "ps", "-q", role], env=env)
            for role in ("ha", "simulator", "runner")
        }
        if not all(containers.values()):
            raise RuntimeError("Lab failed to create all expected containers")
        inspected = json_output(["docker", "inspect", *containers.values()])
        network = json_output(["docker", "network", "inspect", f"{run_id}_lab"])[0]
        volumes = tuple(f"{run_id}_{name}" for name in ("ha_config", "sim_data", "runner_state"))
        validate_inspect(
            network,
            inspected,
            expected_project=run_id,
            expected_container_ids=set(containers.values()),
            allowed_bind_mounts=allowed_binds,
            allowed_volume_names=volumes,
        )
        write_json(artifacts / "inspect.json", {"network": network, "containers": inspected})
        for role, container in containers.items():
            address = next(item for item in inspected if item["Id"] == container)[
                "NetworkSettings"
            ]["Networks"][f"{run_id}_lab"]["IPAddress"]
            snapshot = collect_route_snapshot(container)
            validate_routes(snapshot, subnet=subnet, ipv4_address=address)
            write_json(artifacts / f"routes-{role}.json", snapshot)
            # A route lookup does not send a packet toward any external destination.
            for destination in ("1.1.1.1", "192.168.1.1", "192.168.65.1", "10.0.0.1"):
                check = command(
                    ["docker", "exec", container, "ip", "route", "get", destination], check=False
                )
                if check.returncode == 0:
                    raise RuntimeError(f"{role} has an external route to {destination}")
        result["isolation"] = "passed"
        for role in ("simulator", "ha", "runner"):
            atomic_text(control / f"start-{role}", run_id + "\n")
        deadline = time.monotonic() + args.timeout
        processed = set()
        while time.monotonic() < deadline:
            for request in sorted(control.glob("request-*.json")):
                if request.name in processed:
                    continue
                message = json.loads(request.read_text())
                if message.get("run_id") != run_id or message.get("action") not in {
                    "restart",
                    "kill",
                    "start",
                    "package-baseline",
                    "package-candidate",
                    "migration-snapshot",
                    "backup-restore",
                }:
                    raise RuntimeError("Invalid host crash request")
                ha = json_output(["docker", "inspect", containers["ha"]])[0]
                labels = ha["Config"].get("Labels", {})
                if (
                    labels.get("io.ha-operator.lab.run") != run_id
                    or labels.get("io.ha-operator.lab.role") != "ha"
                ):
                    raise RuntimeError("Crash target identity changed")
                action = message["action"]
                save_precondition = None
                package_evidence = None
                migration_evidence = None
                if action == "backup-restore":
                    backup_id, agent_id = message.get("backup_id"), message.get("agent_id")
                    if (
                        not isinstance(backup_id, str)
                        or not backup_id.replace("-", "").replace("_", "").isalnum()
                        or agent_id != "backup.local"
                    ):
                        raise RuntimeError("Invalid synthetic native backup identity")
                    archive = getattr(args, "baseline_artifact", None)
                    manifest = getattr(args, "baseline_manifest", None)
                    if archive is None or manifest is None:
                        raise RuntimeError("Native rollback requires the matched baseline package")
                    from scripts.release import verify

                    verify(archive, manifest)
                    command(
                        [
                            "docker",
                            "exec",
                            containers["runner"],
                            "python",
                            "-m",
                            "tests.lab.restore_backup",
                            backup_id,
                            agent_id,
                        ],
                        timeout=240,
                    )
                    restore_deadline = time.monotonic() + 120
                    while True:
                        try:
                            package_evidence = verify_installed_package(
                                containers["ha"], archive, manifest
                            )
                            break
                        except RuntimeError, subprocess.CalledProcessError:
                            if time.monotonic() > restore_deadline:
                                raise RuntimeError(
                                    "Native rollback package was not restored"
                                ) from None
                            time.sleep(0.5)
                    package_evidence["native_restore_requested"] = True
                elif action == "migration-snapshot":
                    migration_evidence = migration_snapshot(
                        containers["ha"], message["entry_id"], staging, run_id
                    )
                elif action in {"package-baseline", "package-candidate"}:
                    baseline = action == "package-baseline"
                    archive = (
                        getattr(args, "baseline_artifact", None)
                        if baseline
                        else ROOT / "dist/ha_operator.zip"
                    )
                    manifest = (
                        getattr(args, "baseline_manifest", None)
                        if baseline
                        else ROOT / "dist/ha_operator.manifest.json"
                    )
                    if archive is None or manifest is None:
                        raise RuntimeError(
                            "Baseline package and manifest must be supplied explicitly"
                        )
                    package_evidence = swap_package(
                        containers["ha"],
                        run_id,
                        receipt["images"]["ha"],
                        archive.resolve(),
                        manifest.resolve(),
                        staging,
                        archive_digest,
                    )
                elif action == "kill":
                    save_precondition = wait_saved_request(
                        containers["ha"], message["entry_id"], message["saved_request"]
                    )
                    command(["docker", "kill", "--signal", "KILL", containers["ha"]])
                elif action == "restart":
                    command(["docker", "restart", "--time", "60", containers["ha"]], timeout=90)
                else:
                    command(["docker", "start", containers["ha"]])
                ack = {**message, "completed_at": time.time(), "container_id": containers["ha"]}
                if migration_evidence is not None:
                    ack["migration"] = migration_evidence
                if package_evidence is not None:
                    ack["package"] = package_evidence
                if save_precondition is not None:
                    ack["normal_store_save_verified"] = save_precondition
                write_json(control / request.name.replace("request-", "ack-"), ack)
                with (artifacts / "crash-events.jsonl").open("a") as stream:
                    stream.write(json.dumps(ack) + "\n")
                processed.add(request.name)
            runner = json_output(["docker", "inspect", containers["runner"]])[0]
            if not runner["State"]["Running"]:
                if runner["State"]["ExitCode"]:
                    raise RuntimeError(f"Runner exited {runner['State']['ExitCode']}")
                result["status"] = "passed"
                break
            time.sleep(0.5)
        else:
            raise TimeoutError("Lab runner exceeded its deadline")
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        secret_receipt = control / "secrets.json"
        try:
            # Raw evidence stays outside CI upload paths until redaction succeeds.
            if containers.get("runner"):
                command(["docker", "stop", "--time", "10", containers["runner"]])
            for role, container in containers.items():
                log = command(["docker", "logs", container], check=False)
                (artifacts / f"{role}.log").write_text(log.stdout + log.stderr)
            result["completed_at"] = time.time()
            write_json(artifacts / "summary.json", result)
            if not args.keep:
                disposed = command(
                    [*compose, "down", "--volumes", "--remove-orphans"],
                    env=env,
                    capture=False,
                    check=False,
                    timeout=120,
                )
                if disposed.returncode:
                    result.update(status="failed", cleanup="failed", error="Lab disposal failed")
                    write_json(artifacts / "summary.json", result)
                    raise RuntimeError("Lab disposal failed; evidence remains in private staging")
                result["cleanup"] = "passed"
                cleanup = verify_cleanup(run_id)
                write_json(artifacts / "cleanup-verification.json", cleanup)
                if cleanup["status"] != "passed":
                    result.update(status="failed", cleanup="failed")
                    write_json(artifacts / "summary.json", result)
                    raise RuntimeError("Scoped Docker objects survived teardown")
            else:
                result["cleanup"] = "retained_by_request"
            write_json(artifacts / "summary.json", result)
            secret_values = (
                json.loads(secret_receipt.read_text()) if secret_receipt.exists() else []
            )
            try:
                sanitize_artifacts(artifacts, secret_values)
            except Exception:
                result.update(status="failed", error="Evidence redaction did not complete")
                write_json(artifacts / "summary.json", result)
                raise
            write_json(
                artifacts / "sanitized.json", {"run_id": run_id, "completed_at": time.time()}
            )
            published.parent.mkdir(parents=True, exist_ok=True)
            artifacts.replace(published)
            print(f"Lab {result['status']}: {published}")
        finally:
            (control / "lifecycle-session.json").unlink(missing_ok=True)
            secret_receipt.unlink(missing_ok=True)
            (control / ".secrets.tmp").unlink(missing_ok=True)
            if not args.keep:
                (control / "preview-login.json").unlink(missing_ok=True)
    return published


def main():
    if Path.cwd().resolve() != ROOT:
        raise SystemExit(f"Run this command from {ROOT}")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "test"))
    parser.add_argument("--ha-version", choices=SUPPORTED, default=SUPPORTED[0])
    parser.add_argument(
        "--scenario",
        choices=(
            "smoke",
            "cover",
            "all",
            "soak",
            "cellar",
            "windows",
            "observe",
            "observability",
            "sleep",
            "conditions",
            "typed",
            "migration",
        ),
        default="all",
    )
    parser.add_argument("--baseline-artifact", type=Path)
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--keep", action="store_true", help="Keep failed lab for inspection")
    args = parser.parse_args()
    (prepare if args.command == "prepare" else run_lab)(args)


if __name__ == "__main__":
    main()
