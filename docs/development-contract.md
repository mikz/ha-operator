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
follow the same rule. No private HA trace APIs or global service interception.

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
persistent platform advisor exists. Keep the HA 2026.9.3 and 2026.9.4 gates.
