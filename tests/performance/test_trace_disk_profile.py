"""Measure real trace file writes outside the event loop, with ordered readback."""

from __future__ import annotations

import cProfile
import gc
import json
import os
import platform
import statistics
import time
import tracemalloc
from importlib.metadata import version
from pathlib import Path

from custom_components.ha_operator.shadow import TraceDisk


def test_profile(tmp_path, monkeypatch):
    output = Path(os.environ["OPERATOR_PROFILE_OUTPUT"])
    measure = os.environ["OPERATOR_PROFILE_MEASURE"]
    count = int(os.environ["OPERATOR_PROFILE_COUNT"])
    disk = TraceDisk(tmp_path / "trace")
    disk.load()
    original_fsync = os.fsync
    syncs = 0

    def fsync(fd):
        nonlocal syncs
        syncs += 1
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)

    def batch(index):
        records = [
            {
                "schema": 1,
                "sequence": index * 64 + offset + 1,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "at": float(index),
                "kind": "input",
                "data": {"padding": "x" * (8000 if offset % 8 == 0 else 128)},
            }
            for offset in range(64)
        ]
        result = disk.append(records)
        assert result["durable"] == records[-1]["sequence"]
        assert not result["evictions"] and not result["oversized"]

    for index in range(10):
        batch(index)
    syncs = 0
    gc.collect()
    profiler = cProfile.Profile() if measure == "cprofile" else None
    if measure == "memory":
        tracemalloc.start()
        allocated_before = tracemalloc.get_traced_memory()[0]
    if profiler:
        profiler.enable()
    latencies = []
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    for index in range(10, count + 10):
        started = time.perf_counter()
        batch(index)
        latencies.append(time.perf_counter() - started)
    elapsed_cpu = time.process_time() - started_cpu
    elapsed_wall = time.perf_counter() - started_wall
    if profiler:
        profiler.disable()
    memory = None
    if measure == "memory":
        latencies.clear()
        gc.collect()
        allocated, peak = tracemalloc.get_traced_memory()
        memory = {"retained_bytes": allocated - allocated_before, "peak_bytes": peak}
        tracemalloc.stop()
    # Independent readback is outside the measured interval. Rotation must not
    # lose, duplicate or reorder any row, including the warm-up records.
    expected, after = 1, None
    while True:
        page = disk.export(after, 1000)
        assert not page["gap"]
        for record in page["records"]:
            assert record["sequence"] == expected
            expected += 1
        after = page["next_after"]
        if not page["more"]:
            break
    assert expected == (count + 10) * 64 + 1
    result = {
        "workload": "disk",
        "measurement": measure,
        "trace_enabled": False,
        "batches": count,
        "counts": {"records": count * 64, "fsync_calls": syncs},
        "wall_seconds": elapsed_wall,
        "cpu_seconds": elapsed_cpu,
        "median_batch_ms": statistics.median(latencies) * 1000 if latencies else None,
        "p95_batch_ms": sorted(latencies)[int(len(latencies) * 0.95)] * 1000 if latencies else None,
        "memory": memory,
        "python": platform.python_version(),
        "ha_version": version("homeassistant"),
        "fixture_version": version("pytest-homeassistant-custom-component"),
        "scope": "synchronous trace writer only; real fsync; 64 rows per append; "
        "readback outside timing; local host filesystem, not HA storage hardware",
    }
    output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    if profiler:
        profiler.dump_stats(str(output.with_suffix(".cprof")))
