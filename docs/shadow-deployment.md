# Passive shadow deployment

This is a reusable procedure. It contains no installation inventory, device
bindings, household schedules, or operational history. Keep those details in a
private deployment record. The integration's [migration runbook](migration.md)
and [scenario catalog](scenarios.md#shadow-observation-and-replay) define the
ownership boundary and isolated validation requirements.

## Prepare a private deployment record

Record the installed HA version, exact integration archive hash, resource
bindings, active legacy writers, manual routes, and physical feedback contracts.
Resolve groups, areas, floors, and labels as well as literal entity targets.
Read action bodies; a name or description is not an ownership contract.
Record unread configuration and unverified behavior explicitly.

Use synthetic role aliases in reusable examples. Never publish real entity IDs,
installation or conversation identifiers, local home paths, endpoints, access
credentials, schedules, or observations. Keep replay input derived from a real
installation private even when identifiers have been pseudonymized: timing and
state transitions can still disclose household behavior.

## Install an additive observer

1. Install the exact archive that passed isolated acceptance tests.
2. Enable `shadow_lock` and `trace_enabled` before adding resource subentries.
3. Keep every resource in observe mode. Existing automations retain actuator
   ownership; shadow deployment does not authorize disabling them.
4. Bind underlying position or relay feedback separately from virtual intent.
   A successful service response and a desired target are not physical feedback.
5. Select additional `trace_entities` only for the inputs needed for comparison.
   Recording an input does not assign actuator ownership.
6. Keep new managed entities outside HomeKit and other voice/mobile bridges,
   automatic domain exposure, aggregate control groups, areas, floors, and labels
   that could route commands to them. Any bridge filter migration is a separate
   reviewed change; preserve existing accessory identity and pairing.
7. Verify the loaded lock, observe modes, current artifact fingerprint, fresh
   heartbeat, and zero operator dispatch before anchoring the observation start.

Do not infer capabilities from virtual wrappers. A cover that lacks STOP must
remain honestly represented. Add a relay fan only after its complete profiles,
feedback contract, and reversal dead time have been independently established.
Keep experimental airflow requirements in the lab initially; observe mode can
still report unmet requirements. Unverified fireplace, passive inlet, airflow,
or restriction inputs remain unconfigured.

## Archive original evidence

Use the admin-only `ha_operator.export_trace` response action. Preserve each
original page, health flag, sequence, and timestamp. Commit pages and their hash
index durably before advancing the checkpoint. Serialize capture; reject stale
cursor updates and conflicting overlap. Never fabricate missing records or
change completeness flags to produce a passing report.

Preserve the original MCP text before parsing. JSON serializers in different
languages can normalize numbers differently and invalidate canonical hashes.
Use Python to extract and archive the response while preserving its numeric
types. Keep raw envelopes and rejected responses privately for diagnosis.

Select the collection cadence from actual record volume and bounded retention.
A daily review may retrieve continuously recorded evidence, but the schedule
itself does not prove that every record survived. Continue pages until the
export tip or a declared catch-up budget; report backlog and any lost range.
A segment rotation count is activity, not proof of loss. Eviction, sequence
holes, dropped records, write errors, unclean tails, and stale heartbeats must
remain visible as limits on the observation interval.

## Compare intent and feedback

Keep requested target, effective desired state, last dispatched command, and
observed state separate. Account for target settling and the configured movement
timeout. Repeated unchanged telemetry must not restart those deadlines.
Investigate sustained divergence without guessing a physical cause. Hidden
refusal cannot be named as rain without suitable evidence.

For a relay fan, record virtual state, power, each direction relay, availability,
manual flag, and timers separately. Check for both directions on, an unconfirmed
direction profile, persistent intent/feedback mismatch, missing timer starts,
and unexpectedly prolonged runs. Unknown or restored feedback blocks physical
inference. Relay state does not establish motor rotation or measured airflow.
A manual flag alone does not identify who started a run or define a lease expiry.
Use simulated failures and synthetic timestamps to reproduce these patterns in
the isolated lab; do not inject faults into an occupied installation.

Manual removal of the last confirmed inlet remains permitted. The coordinator
reports unmet airflow and tries eligible alternatives without stopping extraction.
A hands-off provider may satisfy a requirement through its observed condition,
but it must not be actuated automatically. Confirm a replacement before an
otherwise autonomous handover releases the prior provider.

## Review the observation window

Choose a fixed start and review deadline before collecting evidence. For a
seven-day review require at least 168 elapsed hours and seven separately
accounted local morning opportunities. Use the installation's timezone and
handle daylight-saving changes explicitly. Keep one private ledger row per
opportunity with eligibility, suppression, observed edge or absence, sequence
range, and outcome. Elapsed time alone is not a passing result.

Compute each resource's known comparison seconds over the same start-to-capture
wall-time interval. Include uncovered head, tail, and internal gaps; do not use
only the first-to-last-record ratio. Clip any setup intervals outside the anchor.
Require at least 99 percent accounted coverage, continuous lock/observe evidence,
zero operator dispatch, and no unexplained sustained comparison divergence for
a passing shadow review. A missing natural target change remains not exercised.

Classify every scenario as observed, lab-only, not exercised, or unobservable.
Shadow agreement does not prove the effect of a command the operator did not
send. Keep snapshot replay, native current-clock input checks, and simulator
physical-effect assertions distinct. A missing rain, reversal, manual takeover,
sleep-in, or restart event cannot pass solely because its lab counterpart did.

A completed review supports a separate canary decision. Inspect durable intent
before unlocking, activate one resource explicitly, and preserve a verified
rollback owner. Never turn a monitoring schedule into automatic live cutover.
