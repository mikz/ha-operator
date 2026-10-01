# Implementation boundary contract

This is the shared worker contract for the approved v1 plan. It is implementation
documentation, not proof that any release gate passed. Root owns runtime.py,
__init__.py, services.py and the common project/test configuration.

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
updates attached controls in one durable transaction. Repeated ON and source OFF
do not change dependents. Graph references must exist and remain acyclic.

Policy data: `name`, `resource_id`, `kind` (state/occurrence), `priority` (0),
`target` (Target dictionary), optional `eligibility_entity` and `eligibility_state`
(on), optional `target_entity`/`target_attribute`/`target_field` (position default).
Switch state policies can instead use `intent_id`, an internal stable reference
to the committed desired boolean; it is exclusive with other target fields.
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

## Core models (engine worker owns exact implementation)

Frozen Target fields: position: float|None; on: bool|None; percentage: float|None;
direction: str|None; profile: str|None. from_dict/to_dict helpers, omit None values.
Observation: target: Target|None, available: bool, moving: bool, restriction:
str|None, reported_at: float. All time arguments are UTC Unix seconds.
ManualLease: resource_id, mode (target/hands_off), target, expires_at (None indefinite),
request_id, source. Core resolves valid manual > requirement > ordinary policy.
Pure API and remaining dataclasses finalized by engine worker and broadcast to root.

## HA runtime interface (root owns)

`entry.runtime_data` is OperatorRuntime, with `resources` mapping ID -> resource data,
`policies` and `requirements` mappings, `observations` mapping ID -> Observation,
`decisions` mapping ID -> Decision, `requirement_results` mapping ID -> result,
`store.state` durable dictionary, `fault` optional str, `last_commands` mapping
ID -> dictionary, `next_attempts` mapping ID -> timestamp, `attempts` mapping ID -> int.

Methods: subscribe(callback)->unsubscribe; async_request(resource_id, *, mode='target',
target: Target|dict|None, duration: float|None=None, expires_at: float|None=None,
indefinite=False, request_id: str|None=None, source='service'); async_release(id);
async_stop(id); async_set_mode(id, mode); async_set_policy_enabled(id, bool);
async_submit_occurrence(policy_id, occurrence_id, expires_at);
async_skip_occurrence(policy_id, occurrence_id, expires_at);
async_reconcile(resource_id: str|None=None); explain(resource_id: str|None=None)->dict.
`mode(id)` -> observe/live; `manual(id)` -> ManualLease|None; `policy_enabled(id)` bool.
`policy_input(id)` returns committed `NumericState`/`TimerState` or None;
`return_monitor(id)` returns committed `ReturnMonitorState`. Read-only entity
views never schedule or own these transitions.
`adapter(id)` returns adapter object, exposes supported_features, read_observation(now),
normalize(Target)->Target, async_apply(Target, still_current: Callable[[],bool]),
async_stop(), supports_stop bool. notify callbacks are HA event-loop callbacks.

## Strict storage (storage worker owns)

`IntentStore(hass, path: Path)`; `.state` dictionary; `.fault` optional string;
async_load(expected_existing: bool=False); async_update(mutator: Callable[[dict],None])
->dict (serialize updates from current committed state, deep copy before mutator);
async_close() waits outstanding writer; explicit recovery only through reload.
Empty payload keys: revision=0, manuals={}, occurrences={}, modes={}, policy_enabled={},
requests={} (idempotency receipts), intents={} (held booleans), policy_inputs={}
(committed numeric/timer input records), and return_monitors={} (committed cover
return deadlines). Writes use the version 3 envelope. Reading version 1 adds an
empty intents map; reading versions 1 and 2 adds empty policy_inputs and
return_monitors maps before validating the complete state. Existing intent and
absolute deadlines remain intact. Mutator exceptions must not fault storage.
Writer failure inhibits; success persists before publication. Unusable
authoritative state during startup fails config-entry setup with a Repair; a
write failure after healthy setup leaves the loaded runtime inhibited.

## Entities

Platforms add entities with config_subentry_id. Stable unique IDs `${subentry_id}_${key}`.
Managed entity commands call runtime request methods and report observations only.
Base entity callback subscribes/unsubscribes runtime and writes state. Resource device
identifier (DOMAIN, subentry_id). Each subentry owns its independent logical device;
there is no parent device across subentry boundaries. Policy and requirement devices
follow the same rule. No private HA trace APIs or global service interception.

## Lab

Production ZIP installed into fresh `/config`; simulator test integration lives only
under tests/lab/custom_components/ha_operator_sim. Simulator worker owns tests/lab/sim
and test-only integration; lab worker owns scripts/lab.py, tests/lab/compose.yaml,
Dockerfiles, runner and E2E scenarios. Agree transport contract directly. No production
MCP/tool use by implementation workers. Strict isolated network, no published ports.

## Gates

Package evidence reflects real execution; no unrun check can be labelled passed.
Root owns final acceptance. Workers run bounded meaningful tests for their modules and
report exact commands/results. All workers preserve each other's changes. Consult the
persistent read-only /root/ha_platform advisor for platform/lifecycle questions.
