# Measure event handling

The event-load harness compares two exact integration ZIPs inside separate
disposable Home Assistant fixture processes. It accepts no production credentials
or configuration. Network sockets are disabled, with local Unix sockets allowed
for the fixture. The final image-based lab validates actuator behavior separately.

## Method

Use the same interpreter, dependencies, workload and host for both archives:

```sh
.venv/bin/python scripts/profile_event_load.py \
  --baseline baseline/ha_operator.zip \
  --candidate dist/ha_operator.zip \
  --output artifacts/profiles \
  --batches 100 --repeats 3
```

The output directory must not exist. The runner extracts a new temporary copy of
each archive for every subprocess. It records archive and probe hashes, Python,
Home Assistant and fixture versions, event counts, task counts and worker counts.
Timing runs alternate baseline/candidate order and use three repetitions.

After ten warm-up batches, the harness measures process CPU time, elapsed wall
time and end-to-end batch latency without a profiler. Separate runs use cProfile
for attribution and tracemalloc for Python allocations. Neither profiler's
overhead is included in the reported unprofiled timing comparison. Asyncio debug
is disabled to match ordinary HA scheduling.

Home Assistant's [profiler integration](https://www.home-assistant.io/integrations/profiler/)
uses cProfile on the event-loop thread. This harness uses the same profiler and
retains the native `.cprof` files plus cumulative-call reports. Executor thread
stacks are outside that profile. Process CPU timing includes their CPU use;
file-system waits also contribute to wall time. A live profile is not needed to
run this isolated comparison.

Use `--suite trace-disk` for the separate blocking-writer benchmark. It appends
64 complete rows per batch to real local files, counts actual `fsync` calls,
and verifies every sequence through paginated readback after timing. cProfile
then covers the writer thread's code directly. This isolates file-write cost;
it does not measure the async queue or the production storage hardware.

The common workload uses four cover resources and their enabled native entities:

- 100 unchanged raw reports, each followed by a drained HA task batch.
- 100 bursts of 25 changing raw positions: 2,500 input changes total.
- 100 unrelated trace-only input changes.

Each runs with integration tracing disabled and enabled. Additional candidate-only
workloads use two desired controls and 15 live simulated switch followers:
100 alternating central commands, or 100 batches of 15 unchanged follower reports.
These are load measurements; their immediate simulated feedback does not represent
KNX protocol, Adaptive Lighting, or physical lamp behavior.

## Implementation boundaries

Unchanged telemetry refreshes observation timestamps and remains eligible for
tracing. It does not trigger reconciliation unless semantics changed, a relevant
adapter is applying a command, or an evaluation deadline is due. Independent
deadline timers remain active. Same-turn changes coalesce into one HA-tracked
task that reads current state. Resource dependencies select which workers wake.
Every dispatch retains a fresh generation/restriction/expiry check.

Entity notifications compare their public values before writing state. A detached
snapshot is copied only after a change. This avoids repeated `state_reported`
events from unchanged Operator entities without hiding capability, availability,
attribute or entity-link changes. The engine remains a pure snapshot evaluator.

The trace writer groups its existing bounded batch into complete JSONL chunks,
flushing each affected segment once. Rotation closes and flushes the old file
before renaming it. The batch's durable watermark advances only after all file
flushes and any required directory flush succeed. A failed or uncertain batch
still produces a gap and cannot be acknowledged as complete. Queue, record and
retention bounds are unchanged.

## Evidence limits

The fixture excludes Recorder, KNX, Adaptive Lighting and full-house load. Batch
latency is end-to-end fixture completion, not a pure event-loop lag measurement.
The workload timing stops before final trace drain; drain CPU/wall time and trace
health are reported separately. Memory is sampled after drain and garbage
collection. Retained Python allocations and peak allocations are not process RSS
and do not establish a leak or the cause of a host OOM.

Tracing has a bounded queue and disk retention. Rapid artificial command/input
bursts can exceed those bounds. Every run records health before the workload,
afterward and after drain. Dropped records or retention eviction prevent a
complete-trace claim, even when actuation is correct and every worker settles.
Performance measurements do not replace the packaged safety scenarios.

Raw profiles can contain local filesystem paths. Keep them private or sanitize
them before publication; do not add production exports to this repository.

## Measured candidate: 0.1.4

Measured on Python 3.14.7, HA 2026.9.3 and fixture 0.13.366. Baseline source
was commit `bfc729d`; its ZIP hash is
`6bd4c64dc1c502be50db6b85c6c9fa045ec0feaebf636c4568564adb3e774d6b`.
The measured candidate ZIP hash is
`7252f8553b5ae1d5dfefc1c5aa72bdc502b1d2eb502a31a2be0d70920e544b41`.
These are fixture results, not measurements of a production HA installation.

Median unprofiled CPU across three runs, for the complete 100-batch workload:

| Input | Tracing | Baseline CPU | Candidate CPU | Reduction |
| --- | --- | ---: | ---: | ---: |
| unchanged | off | 0.1217 s | 0.0010 s | 99.2% |
| unchanged | on | 0.2228 s | 0.0054 s | 97.6% |
| changed burst | off | 0.7097 s | 0.0274 s | 96.1% |
| changed burst | on | 1.6412 s | 0.1254 s | 92.4% |
| trace only | off | 0.0008 s | 0.0007 s | 12.8% |
| trace only | on | 0.0074 s | 0.0076 s | -2.8% |

The untraced unchanged workload fell from 500 reconciliations and 20,000
Operator state reports to zero of each. The changing bursts fell from 2,900
reconciliations to 100 and from 111,000 Operator state reports to zero; 200
meaningful state changes remained. No actuator commands occurred in those
observe-mode workloads. The trace-only path was already inexpensive.

With 15 live simulated followers and 107 enabled native entities:

| Workload | Tracing | CPU, 100 batches | Median of run p95 batch latency |
| --- | --- | ---: | ---: |
| followers commands | off | 1.2540 s | 23.695 ms |
| followers commands | on | 7.2887 s | 87.098 ms |
| followers reports | off | 0.0078 s | 0.144 ms |
| followers reports | on | 0.0413 s | 0.658 ms |

The 1,500 unchanged follower reports produced zero reconciliation, publication
or actuator commands. Task counts stayed constant at 16 without tracing and 17
with tracing, including 15 workers. After warm-up, the four dependent followers
remain ON when central turns OFF; the command workload therefore exercises 11
switching followers per command, issuing 1,100 commands in total.

In the separate tracemalloc command runs, retained allocations after drain and
collection were 0.32 MB without tracing and 3.29 MB with tracing; peak traced
allocations were 2.83 MB and 7.95 MB respectively. These use decimal MB and
exclude allocations that existed before measurement. They are not process RSS.

The traced command runs dropped no queued rows, but exceeded disk retention;
the rapid unchanged-follower runs dropped 269, 446 and 337 rows in the three
unprofiled repetitions. Neither workload provides complete observation evidence.
Raw receipts retain those limits separately from timing. A stable task count
is evidence for this bounded run, not proof of long-term memory stability or
an explanation of a host OOM.

## Trace writer comparison

The packaged regression first exposed queue overflow while appending each row
with its own file flush. A separate benchmark compared the preceding candidate
ZIP `cbc7f601e120c64308983d4d2dabad48b3571c909001b52ce8847cd27734fbc5`
with the final candidate above. Both ran 100 batches of 64 rows, after ten warm-up
batches, with three alternating unprofiled repetitions. Each included real file
and directory flushes on the same local filesystem.

| Writer | Median CPU | Median wall time | `fsync` calls per run |
| --- | ---: | ---: | ---: |
| Per row | 0.2882 s | 0.3573 s | 6,404 |
| Per batch/segment | 0.0291 s | 0.0311 s | 108 |

All 7,040 rows, including warm-up, were read back in order without gaps in each
run. CPU fell 89.9% and elapsed time fell 91.3% in this writer-only comparison.
This does not increase the queue or retention limit, guarantee gap-free capture
at arbitrary rates, or measure flash storage on a production HA host.

## Native policy comparison

Use `--suite native-policies` to compare the five writers from
`tests/lab/window_recipe.py` with two native policy inputs and three return
monitors. The baseline runs the unchanged timer, temperature, and three return
automation factories with eight real HA helpers. Both archives use the same
three live cover resources, baseline policies, synthetic temperature sensor,
public timer, targets, priorities, and raw feedback. Cover service calls do
not change raw feedback or establish physical confirmation.

The runner copies the probe and recipe into each disposable subprocess and
records both hashes with the ZIP hashes. Use the frozen release archive for
acceptance measurements. A development archive only verifies probe mechanics.
Keep the host free of other tests, builds, and lab workloads during timing.

For example, run the three report workloads with 100 batches each:

```sh
.venv/bin/python scripts/profile_event_load.py \
  --suite native-policies \
  --baseline baseline/ha_operator.zip \
  --candidate dist/ha_operator.zip \
  --output artifacts/native-report-profiles \
  --workloads idle unchanged changing --batches 100 --repeats 3
```

Run `timer` and `reload` separately with `--batches 20`, each with a distinct
output directory. All workloads include ten warm-up batches and run with
integration tracing disabled and enabled. The default measurements use three
alternating unprofiled timing repetitions, one cProfile run, and one tracemalloc
run per archive and trace setting. `--measurements` and `--warmup-batches` allow
bounded mechanics checks; those checks do not establish performance results.

The workloads exercise these boundaries:

| Workload | One batch |
| --- | --- |
| `idle` | Advance five seconds with a warm sensor, inactive timer, and confirmed 7% positions. The baseline's five writers wake; no qualification or warning deadline is pending. |
| `unchanged` | Report the unchanged temperature, three raw positions, and timer state through native HA state plumbing. |
| `changing` | Report 15, 14, 17, then 15 °C in one loop turn, alternate all three raw positions between 7% and 100%, and advance five seconds. |
| `timer` | Start, restart, pause, resume, cancel, qualify two fresh episodes, complete the timer naturally, and cancel a final fresh start. |
| `reload` | Reload the actual config entry with an inactive timer and no active timer request. Check that old workers stop before the three replacement workers run. |

The fixed simulation starts at `2026-09-30T08:00:00.500000+00:00`. Five-second
ticks include HA's time-pattern scheduling spread within the first half-second.
Only simulation time advances. Wall timing uses freezegun's preserved real
`perf_counter`, and CPU timing uses `process_time`. Each receipt includes its
virtual start and end. This avoids counting virtual time as elapsed host time.
Both variants use WARNING-level HA logging. Batch p95 uses the nearest-rank
percentile.

The source timer lasts 65 seconds so a 60-second qualification can precede a
genuine natural completion. Both variants retain a 1,800-second request,
1,800-second temperature qualification, and 300-second return warning. This is
a synthetic event workload, not a production-duration soak. Idle has no pending
deadline; changing reports reset or clear return clocks before a warning and
reset native cold qualification. Timer batches exercise qualification deadlines,
but cancel requests before their 1,800-second expiry. The package lab validates
expiry, overdue warnings, restart recovery, and independent physical effects.

Receipts report process CPU, real wall time, median and p95 batch latency,
traced allocations, state changes and reports by entity group, automation runs,
helper service calls, actuator commands, recomputations, actual Operator snapshot
writes, task counts, and worker counts. Final trace drain CPU and wall time remain
separate from workload timing. Reload timing includes native unload cleanup and
its trace drain. Inspect trace health before, after, and after final drain before
making a complete-trace claim.

The native snapshot writer commits transitions durably. The baseline stores
helper state through HA's debounced helper persistence, whose storage writes
are outside the Operator write counter. Higher Operator write counts can reflect
this stronger contract; they do not by themselves establish a regression.
Legacy queued temperature actions read the final current source state and can
miss an intermediate warm transition. The input sequence is identical, but
the old and native qualification semantics are not equivalent for those bursts.
Report these differences with the measurements.
