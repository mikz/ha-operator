"""Compare exact release ZIPs in separate offline native HA fixture processes."""

from __future__ import annotations

import argparse
import json
import os
import pstats
import shutil
import statistics
import subprocess
import sys
import tempfile
import zipfile
from hashlib import sha256
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


def run(args):
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    disk_suite = args.suite == "trace-disk"
    probe = (
        REPOSITORY
        / "tests/performance"
        / ("test_trace_disk_profile.py" if disk_suite else "test_event_profile.py")
    )
    traces = (False,) if disk_suite else (False, True)
    workloads = (
        ("disk",)
        if disk_suite
        else ("unchanged", "changed_burst", "trace_only", "followers_commands", "followers_reports")
    )
    receipts = []
    # Alternate versions to reduce machine-load and thermal-order bias.
    for measurement in ("timing", "cprofile", "memory"):
        for trace in traces:
            for workload in workloads:
                repeats = range(args.repeats if measurement == "timing" else 1)
                for repeat in repeats:
                    variants = [("baseline", args.baseline), ("candidate", args.candidate)]
                    if workload.startswith("followers_"):
                        variants = [("candidate", args.candidate)]
                    if repeat % 2:
                        variants.reverse()
                    for label, package in variants:
                        name = f"{label}-{workload}-trace{int(trace)}-{measurement}-{repeat}"
                        output = destination / name
                        with tempfile.TemporaryDirectory(
                            prefix="ha-operator-profile-"
                        ) as temporary:
                            root = Path(temporary)
                            component = root / "custom_components/ha_operator"
                            component.mkdir(parents=True)
                            with zipfile.ZipFile(package) as archive:
                                # release.py validates releases; refuse traversal here too.
                                for member in archive.namelist():
                                    if Path(member).is_absolute() or ".." in Path(member).parts:
                                        raise ValueError("Unsafe release member")
                                archive.extractall(component)
                            shutil.copyfile(probe, root / "test_event_profile.py")
                            (root / "pytest.ini").write_text(
                                "[pytest]\nasyncio_mode=auto\nfilterwarnings=ignore::DeprecationWarning\n"
                            )
                            environment = {
                                **os.environ,
                                "PYTHONPATH": str(root),
                                "PYTHONDONTWRITEBYTECODE": "1",
                                "OPERATOR_PROFILE_OUTPUT": str(output),
                                "OPERATOR_PROFILE_WORKLOAD": workload,
                                "OPERATOR_PROFILE_MEASURE": measurement,
                                "OPERATOR_PROFILE_COUNT": str(args.batches),
                                "OPERATOR_PROFILE_TRACE": str(int(trace)),
                            }
                            with output.with_suffix(".log").open("w") as log:
                                result = subprocess.run(
                                    [
                                        sys.executable,
                                        "-m",
                                        "pytest",
                                        "-q",
                                        "test_event_profile.py",
                                        "--disable-socket",
                                        "--allow-unix-socket",
                                        "--tb=short",
                                    ],
                                    cwd=root,
                                    env=environment,
                                    stdout=log,
                                    stderr=subprocess.STDOUT,
                                    check=False,
                                )
                            if result.returncode:
                                raise RuntimeError(
                                    f"Profile run failed: {output.with_suffix('.log')}"
                                )
                        receipt = json.loads(output.with_suffix(".json").read_text())
                        receipt.update(label=label, repeat=repeat, file=output.name)
                        receipts.append(receipt)
                        if measurement == "cprofile":
                            with output.with_suffix(".txt").open("w") as report:
                                stats = pstats.Stats(
                                    str(output.with_suffix(".cprof")), stream=report
                                )
                                stats.sort_stats("cumulative").print_stats(45)
                                stats.print_stats("ha_operator", 35)
                        print(name, f"CPU {receipt['cpu_seconds']:.4f}s", flush=True)
    comparison = []
    for trace in traces:
        for workload in (name for name in workloads if not name.startswith("followers_")):
            row = {"workload": workload, "trace_enabled": trace}
            for label in ("baseline", "candidate"):
                rows = [
                    r
                    for r in receipts
                    if r["label"] == label
                    and r["measurement"] == "timing"
                    and r["workload"] == workload
                    and r["trace_enabled"] == trace
                ]
                row[label] = {
                    field: statistics.median(r[field] for r in rows)
                    for field in ("cpu_seconds", "wall_seconds", "median_batch_ms", "p95_batch_ms")
                }
                row[label]["counts_per_run"] = [r["counts"] for r in rows]
            row["cpu_reduction_percent"] = 100 * (
                1 - row["candidate"]["cpu_seconds"] / row["baseline"]["cpu_seconds"]
            )
            comparison.append(row)
    result = {
        "suite": args.suite,
        "baseline_sha256": sha256(args.baseline.read_bytes()).hexdigest(),
        "candidate_sha256": sha256(args.candidate.read_bytes()).hexdigest(),
        "probe_sha256": sha256(probe.read_bytes()).hexdigest(),
        "batches": args.batches,
        "timing_repeats": args.repeats,
        "comparison": comparison,
        "runs": receipts,
    }
    (destination / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batches", type=int, default=300)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--suite", choices=("events", "trace-disk"), default="events")
    run(parser.parse_args())
