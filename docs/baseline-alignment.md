# Baseline alignment

This is the approved implementation plan from 2026-10-01. It supersedes the
custom durability and operator trace contracts in the v0.1.6 implementation.
The [development contract](development-contract.md) and
[architecture](architecture.md) define the resulting requirements. This plan
records pending work, not completed validation.

## Scope

Use Home Assistant's `helpers.storage.Store` directly. The runtime owns its
in-memory state and makes validated state changes on the HA event loop. Use
ordinary `async_load` and `async_delay_save`, with native lifecycle saving where
needed. Keep the default serializer unless measurements justify a change.
Remove `IntentStore`, disk-commit barriers, revisions, durable acknowledgements,
and the storage-fault latch. Do not add a replacement transaction framework or
write-confirmation wrapper.

Remove `ShadowTrace`, its rolling journal and hooks, `export_trace`,
`trace_enabled`, `trace_entities`, the migration-only `seed_intents` action,
watermarks, and custom operator archive,
replay, and report commands. Use native entities, Recorder history,
Activity/Logbook, logging, downloadable diagnostics, and `explain`. Preserve
Playwright browser traces, including `trace.zip`.

Preserve configuration and subentries, entity IDs, retained actions, request
signatures, and duplicate-request behavior. Preserve reconciliation workers,
adapters, generation checks, shutdown barriers, timing, manual override and
expiry, STOP and hands-off, observe/live modes, the global shadow lock, relay
interlocks, and sleep behavior. Source-only sleep followers remain without
manual controls. Policy and trigger redesign is outside this change.

## Upgrade boundary

Do not import old runtime state or read legacy snapshot schemas. Retain config
entries, subentries, and entity identities. A fresh native Store starts with
configured initial desired values, policy controls enabled by default, and resources
in observe mode. Discard old modes, desired values, leases, deadlines,
occurrences, duplicate-request records, and input and monitor state. Live
activation is a separate deployment step after checking configured defaults.

After a deployment backup exists, use one entry version migration to remove
obsolete trace options and only this entry's known snapshot and operator trace
paths. Do not add fallback loading, a second writer, cleanup counters, or a
cleanup framework. Deployment backup supplies rollback.

Subsequent native Store reloads restore saved intent and absolute deadlines.
Expire stale state without renewing durations. Do not replay attachment edges:
central ON with an attached bedroom control OFF remains that way after reload.
Runtime write failures use ordinary Store behavior and do not halt actuation.
Invalid configuration or unusable restored data still produces a setup error
and actionable Repair.

## Implementation sequence

1. Update this plan, the development contract, and architecture commitments.
   Enumerate affected tests before editing runtime code.
2. Replace custom persistence with direct Store usage and runtime-owned state.
   Preserve validated same-run control ordering and duplicate suppression.
   Remove disk-commit barriers while retaining worker and generation barriers.
   Add the entry upgrade and exact-scoped obsolete-file cleanup without imports.
3. Remove operator trace infrastructure, options, actions, scripts, lab replay,
   and archive tests. Replace safety assertions with independent simulator
   effects, HA states, and Recorder history. Keep observation controls and
   browser traces.
4. Complete configuration cleanup and upgrade tests. Revise active documentation,
   service descriptions, translations, package checks, and CI to remove obsolete
   promises and references. Record any retained historical evidence as historical.
5. Package and validate the exact candidate ZIP in the isolated lab on HA
   2026.9.3 and 2026.9.4. Exercise browser and HAP paths and existing safety
   scenarios. Compare v0.1.6 and candidate CPU and allocations using the same
   workload and the existing cProfile and tracemalloc harness. Record actual
   version, archive hash, commands, results, and measurement limits.

The planned release version is `0.2.0`: removing an exported action and resetting
runtime state changes the upgrade contract. The final manifest, archive, and
lab evidence must agree on the approved version and hash. Commit, publication,
and deployment require coordinating-owner authorization.

## Test impacts

The existing files below contain affected coverage. Preserve safety assertions
when removing assertions about disk commits or operator trace records.

| Area | Affected files | Required change |
| --- | --- | --- |
| Persistence | `tests/unit/test_storage.py` | Replace custom writer, revision, fsync, cancellation, and fault-latch tests with native Store initialization, reload, invalid-data setup, and runtime behavior tests. |
| Request ordering and STOP | `tests/integration/test_runtime_requests.py`, `test_platform_concurrency.py`, `test_services.py` | Keep duplicate/conflict handling, deadlines, cancellation, STOP barriers, and supersession. Remove persistence-before-actuation and durable response assertions. Write failures must not latch actuation off. |
| Upgrade and lifecycle | `tests/integration/test_lifecycle.py`, `test_config_flow.py`, `test_subentry_removal.py` | Verify retained config and IDs, fresh configured defaults, observe modes, discarded legacy runtime state, exact cleanup, and no fallback loading. Keep invalid-config Repair and worker shutdown coverage. |
| Desired controls and sleep | `tests/integration/test_desired_controls.py`, `tests/lab/scenarios_sleep.py` | Keep ON attachment, repeated ON, detached OFF, source-only restrictions, and raw-feedback separation. Verify native reload preserves detached OFF without replaying an edge. Remove strict trace and durable ordering assertions. |
| Native inputs and return monitors | `tests/integration/test_policy_inputs_runtime.py`, `test_policy_inputs_lifecycle.py`, `test_return_monitor_runtime.py`, `test_native_policy_entities.py` | Keep qualification, timer episodes, warning deadlines, stale expiry, and same-run input ordering. Use Store restoration without custom commit barriers. |
| Fan and presentation | `tests/integration/test_fan_intent.py`, `test_entities.py`, `test_gold_presentation.py`, `test_source_availability.py` | Keep fan settings, relay safety, availability, and entity links. Remove durability and journal attributes. |
| Operator trace | `tests/unit/test_shadow*.py`, `tests/integration/test_shadow_runtime.py`, `test_shadow_lab_export.py`, trace tests in `test_services.py` and `test_desired_controls.py` | Remove tests for deleted infrastructure. Retain `test_shadow_lock.py` with assertions through state and effects. |
| Packaged lab | `tests/lab/scenarios_shadow.py`, `shadow_trace.py`, `replay.py`, `readiness.py`, `runner.py`, `scenarios_observability.py`, `scenarios_windows.py`, `scenarios_cellar.py` | Remove operator journal readiness and replay dependencies. Keep meaningful safety, observability, restart, browser, and HAP checks through simulator effects and HA state or Recorder. |
| Profiling and release evidence | `tests/performance/test_*profile.py`, `tests/unit/test_profile_event_load.py`, `test_evidence.py`, `test_lab_readiness.py`, `test_lab_controller.py`, release and CI checks | Remove trace-disk benchmarking and trace-health requirements. Keep comparable event/native-policy workloads, cProfile, tracemalloc, package hashes, browser artifacts, and privacy checks. |

Safety acceptance still covers rain refusal and later reconciliation, airflow
handover, manual closure and expiry, occurrence suppression, relay reversal,
HomeKit commands, sleep followers, reload, and restart. A client acknowledgement
or requested target cannot establish physical completion.

## Execution boundary

Development and tests use the isolated lab without production credentials,
devices, host networking, or external routes. Keep private deployment evidence
outside the public repository. Reuse cached pinned dependencies and images.
Retire only this task's containers and temporary work after validation; preserve
other users' containers and workers. Report failed cleanup and remaining paths.
