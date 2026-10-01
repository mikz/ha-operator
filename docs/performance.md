# Profile an isolated release

Use `scripts/profile_event_load.py` to compare two packaged integration archives
with identical inputs in native Home Assistant fixtures. The event suite covers
unchanged reports, changed bursts, follower commands, and follower reports. The
native-policy suite covers idle, unchanged, changing, timer, and reload workloads.
The v0.1.6 baseline runs with Operator tracing disabled.

Run timing, cProfile, and tracemalloc separately. Timing repeats alternate archive
order; summaries use medians. cProfile attributes profiled call time, and tracemalloc
reports retained allocations after garbage collection and peak Python allocation.
Default cProfile timings can include elapsed waits. The uninstrumented
`process_time` metric measures CPU. Neither profiler’s timings are an
uninstrumented latency measurement.

```sh
.venv/bin/python scripts/profile_event_load.py \
  --baseline /private/path/baseline.zip --candidate dist/ha_operator.zip \
  --output /private/path/profile-events
.venv/bin/python scripts/profile_event_load.py \
  --baseline /private/path/baseline.zip --candidate dist/ha_operator.zip \
  --output /private/path/profile-policies --suite native-policies
```

Each run extracts one archive into a disposable directory. Operator Store keys
use real isolated filesystem I/O; other HA fixture storage retains its normal
mock behavior. The baseline uses its own snapshot writer. Both saved files are
verified after ordinary unload. Workload measurements exclude final unload save
and result serialization. Delayed saves that occur within the workload remain
inside that interval. Native-policy runs count actual Operator writes.

Receipts bind archive hashes, workloads, warmup, dependency versions, event and
command counts, latency samples, and the measurement boundary. These fixtures
exclude Recorder, network devices, and full-house load. Use the packaged lab for
physical effects and browser acceptance. A smoke run verifies the harness; it
does not establish a release performance result.
