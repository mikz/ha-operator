# Implementation boundary contract

This contract defines the approved baseline alignment described in
[Baseline alignment](baseline-alignment.md). It is an implementation requirement,
not a claim that the changes or release gates have passed. The implementation
owner owns production code, tests, documentation, and packaging. The coordinating
owner reviews the changes and runs final acceptance.

## Configuration

One singleton config entry, native subentries of type `resource`, `policy`, `requirement`, `intent`.
IDs are the HA subentry_id. Resource data: `name`, `kind` (cover/switch/fan/relay_fan),
`entity_id` (native adapters), `retry_interval` (300), `command_interval` (30),
`movement_timeout` (120), `tolerance` (2), `manual_duration` (1800), optional
`restriction_entity` (on blocks), `fault_entity` (on inhibits), and `default_target`.
Relay fan data: `outputs` list of switch entity IDs, `profiles` mapping of profile
name to {outputs: {entity_id: bool}, percentage?: number, direction?: str},
`reversal_dead_time` required, `default_target` containing an on profile target. Every profile
specifies every output, including off; an all-off profile is required.

Resource `manual_control` defaults to true. False removes manual command and lease
controls and rejects manual requests, release, and STOP. Existing controls are
integration-disabled without erasing registry identity or user-disabled settings;
old leases are pruned before the source follower becomes active.

Cover resource data can include `return_monitor`: `target_at_most` (0–100) and
positive `warning_after_seconds`. It uses the existing resource tolerance.

Resource `command_conditions` optionally maps supported command names to native HA
condition lists. Keys are `turn_on`/`turn_off` for switch and relay fan,
`set_cover_position` for cover, and `turn_on`/`turn_off`/`set_direction`/
`set_percentage` for native fan. Fan ON/OFF guards also apply when HA requires the
percentage-service fallback. An ON guard applies before a fan or relay transaction
starts and before each energizing output. A deliberate relay OFF obeys `turn_off`;
the mandatory all-off interlock leg of an ON/reversal transaction keeps its existing
safety behavior. Supported STOP bypasses these guards. Guards apply equally to
manual requests and policies, and never replace the selected target with OFF.
Enabled command conditions use Home Assistant's native semantics. A false result
or an evaluation error exposed to Operator blocks that operation. The selected
native condition handles missing or unavailable entities; they are not a universal
availability veto. Use an explicit availability/state condition when absence must block.
Native target conditions using `behavior: all` can pass when no available targets
remain. Preserve that native result; use a single-entity state condition or
`behavior: any` for each required target joined by native AND when presence matters.
Native empty or disabled top-level condition lists permit commands.
Recheck command permission, restrictions,
and generation immediately before physical dispatch and generation after awaits.

Resource `health_checks` optionally contains objects with a unique `alias`, native
`conditions`, and optional native `qualification` conditions. Native repeatable
object selectors expose the condition editors in resource add/reconfigure forms.
Persist the original serializable HA condition configuration. Native compiled
checkers own evaluation, dependency tracking, and cleanup; bounded polling covers
checkers whose native `needs_polling` is true. Reject `condition: trigger`, including
inside compounds: reconciliation has no automation trigger context. Core 2026.10.2
is the minimum; there is no earlier condition implementation fallback.

Qualification that is false means waiting, errors mean unknown, and disabled or
empty qualification means skipped. A qualified check reports healthy, unhealthy,
unknown, or skipped; disabled/empty checks never establish health. Native compound
truth remains authoritative, including OR conditions that handle optional inputs.
A known unhealthy check remains visible alongside unknown or waiting checks.
Health never changes selected targets or command permission and never dispatches
STOP/restart. Only resources with configured health checks gain a diagnostic,
read-only Health entity; existing entity identities and histories remain intact.

Check evidence reports the native config, results/errors, and bounded factual source
snapshots, never a device diagnosis. Native static entity extraction is incomplete
for templates, expanded targets, and some entity-valued bounds; label its provenance
without claiming complete dependencies or that every referenced branch ran. An
unchanged sensor's `last_changed` is not proof of stale data. Publish frozen evidence
only on meaningful health/permission transitions, while `explain` may refresh source
facts. Do not rerun native conditions for explanation traces or mutate a caller's
native automation trace. Downloadable diagnostics retain their privacy allowlist.

Intent data: `name`, required boolean `initial_value`, optional `on_targets` list
of other intent IDs. Values are held until explicit command. An OFF-to-ON command
updates attached controls as one in-memory state change. Repeated ON and source OFF
do not change dependents. Graph references must exist and remain acyclic.

Policy data: `name`, `resource_id`, `kind` (state/occurrence), `priority` (0),
`target` (Target dictionary), optional `eligibility_entity` and `eligibility_state`
(on), optional `target_entity`/`target_attribute`/`target_field` (position default).
Switch state policies can instead use `intent_id`, an internal stable reference
to the desired boolean; it is exclusive with other target fields.
Native attributes are subscribed as well as state changes. Runtime enabled state is
persisted; disabling tombstones all pending occurrences, not future occurrences.

Optional policy `input` replaces helper eligibility fields. `qualified_numeric`
requires a state policy, sensor `entity_id`, `comparison: below`, finite
`threshold`, exact native `unit`, and positive `qualification_seconds`.
`timer_episode` requires an occurrence policy, timer `entity_id`, positive
`qualification_seconds`, and positive `request_seconds`. Legacy policy data
remains supported. Config entry version 2 preserves existing data and subentry IDs.

Requirement data: `name`, `activation_entities` (all on; unknown stays unknown),
`providers` list of {id, resource_id?: str, target?: Target, evidence: list of
{entity_id, attribute?: str, operator: eq/gte/lte, value, kind:
position/contact/relay/airflow}}, `acquisition_timeout` (120).
All evidence predicates must hold; unknown means unconfirmed. Provider order is
list order. Passive providers have no resource_id. No extractor output is configured.

## Core models

Frozen Target fields: position: float|None; on: bool|None; percentage: float|None;
direction: str|None; profile: str|None. from_dict/to_dict helpers, omit None values.
Observation: target: Target|None, available: bool, moving: bool, restriction:
str|None, reported_at: float. All time arguments are UTC Unix seconds.
ManualLease: resource_id, mode (target/hands_off), target, expires_at (None indefinite),
request_id, source. Core resolves valid manual > requirement > ordinary policy.
The engine evaluates snapshots without performing persistence or actuator effects.

## HA runtime interface

`entry.runtime_data` is OperatorRuntime, with `resources` mapping ID -> resource data,
`policies` and `requirements` mappings, `observations` mapping ID -> Observation,
`decisions` mapping ID -> Decision, `requirement_results` mapping ID -> result,
the runtime state dictionary, `fault` optional str, `last_commands` mapping
ID -> dictionary, `next_attempts` mapping ID -> timestamp, `attempts` mapping ID -> int.

Methods: subscribe(callback)->unsubscribe; async_request(resource_id, *, mode='target',
target: Target|dict|None, duration: float|None=None, expires_at: float|None=None,
indefinite=False, request_id: str|None=None, source='service'); async_release(id);
async_stop(id); async_set_mode(id, mode); async_set_policy_enabled(id, bool);
async_submit_occurrence(policy_id, occurrence_id, expires_at);
async_skip_occurrence(policy_id, occurrence_id, expires_at);
async_reconcile(resource_id: str|None=None); explain(resource_id: str|None=None)->dict.
`mode(id)` -> observe/live; `manual(id)` -> ManualLease|None; `policy_enabled(id)` bool.
`policy_input(id)` returns `NumericState`/`TimerState` or None;
`return_monitor(id)` returns `ReturnMonitorState`. Read-only entity
views never schedule or own these transitions.
`adapter(id)` returns adapter object, exposes supported_features, read_observation(now),
normalize(Target)->Target, async_apply(Target, still_current: Callable[[],bool]),
async_stop(), supports_stop bool. notify callbacks are HA event-loop callbacks.

## Native persistence

Use `homeassistant.helpers.storage.Store` directly for runtime state under
`.storage`. The runtime owns its in-memory state. Use `async_load`,
`async_delay_save`, and native lifecycle saving where needed. Keep the default
serializer unless profiling justifies a change. Do not add a
commit revision, durable acknowledgement, write-confirmation wrapper, second
writer, or storage-fault latch. Runtime write failures follow ordinary Store
behavior and do not halt actuation. Invalid configuration or unusable restored
data fails setup with an actionable Repair.

The runtime maintains modes, policy enablement, held desired values, manual
leases, absolute deadlines, occurrence suppression, duplicate-request records,
policy inputs, and return monitors. Preserve request method signatures and
duplicate-request behavior, but remove commit revision and durable receipt
semantics. Read-only entity views do not mutate this state.

The upgrade retains config entries, subentries, and entity identities. It does
not import schema 1–3 `ha_operator.<entry>.json` runtime state. A fresh Store uses
configured initial desired values, policy controls enabled by default, and observe
resource modes. Old leases, deadlines, occurrences, request records, and input
or monitor state do not carry over. Reload after native persistence restores
absolute deadlines and expires stale state without renewing durations or
synthesizing OFF-to-ON attachment edges.

After a deployment backup exists, remove only this entry's obsolete snapshot
and operator trace files through a small, exact-scoped cleanup. Do not read the
obsolete snapshot as a fallback or retain a second active persistence path.

## Native observability

Use native entities, Recorder history, Activity/Logbook, the integration logger,
downloadable diagnostics, and `explain`. Remove `ShadowTrace`, its listeners and
adapter hooks, `export_trace`, `trace_enabled`, `trace_entities`, trace watermarks,
and operator journal archive, replay, and report tools. Keep observe/live modes
and the global shadow lock. Keep Playwright browser traces; they are browser
test artifacts.

## Entities

Platforms add entities with config_subentry_id. Stable unique IDs `${subentry_id}_${key}`.
Managed entity commands call runtime request methods and report observations only.
Base entity callback subscribes/unsubscribes runtime and writes state. Resource device
identifier (DOMAIN, subentry_id). Each subentry owns its independent logical device;
there is no parent device across subentry boundaries. Policy and requirement devices
follow the same rule. Meaningful Reason transitions record bounded decision
snapshots through a real internal native Script, before publishing the matching
state. Existing caller trigger evidence may be read only from its exact active
native automation context. No global service interception, caller-trace mutation,
condition reconstruction, separate trace database, or frontend override.


## Lab

Install the production ZIP into fresh `/config`. The simulator test integration
lives only under `tests/lab/custom_components/ha_operator_sim`. Use independent
simulator effects, HA states, and Recorder for assertions. Do not replace the
operator journal with another audit framework. Implementation workers must not
use production MCP tools. Keep the lab network isolated with no published ports.

## Gates

Package evidence reflects real execution; no unrun check can be labelled passed.
The coordinating owner owns final acceptance. Workers run bounded meaningful
tests and report exact commands and results. Preserve other workers' changes.
Route platform and lifecycle questions to the coordinating owner when no
persistent platform advisor exists. Require the native HA 2026.10.2 baseline; earlier versions are unsupported.
