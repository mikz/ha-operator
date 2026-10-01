# Configuration and action reference

HA Operator uses one configuration entry with four native subentry types:
resources, policies, airflow requirements, and desired controls. Existing Home
Assistant integrations own the physical devices. Operator owns logical devices
and saved intent. See the [adapter examples](../examples/README.md) for complete
configuration objects.

## Configure the integration

Add **HA Operator** in **Settings > Devices & services**. The
shadow lock defaults to off. Enabling it keeps resources in observe mode and
blocks Operator actuator commands. Disabling it leaves resources in observe
mode until explicitly activated.

Adding, editing, or removing a subentry reloads the entry automatically after a
healthy setup. Reload and entity enablement can interrupt active native timer
qualification or accepted episodes. Ordinary manual leases, desired controls,
and valid absolute deadlines survive reload. Home Assistant shutdown and restart
have separate timer recovery behavior. See [window control](window-control.md).

If authoritative saved intent cannot load, setup fails and a Repair explains
recovery. Preserve the snapshot, correct the cause, and select **Reload** from the
integration menu. Editing a failed entry alone does not establish successful
recovery. Offline downstream devices do not make the whole integration fail setup.

## Configure a resource

Each resource needs `name` and `kind`. Choose one of the following adapters.
Names are your labels; references use stable subentry IDs.

| Adapter | Required configuration | Feedback and command constraints |
| --- | --- | --- |
| `cover` | `entity_id` | Raw cover with non-optimistic `current_position` and position control. STOP exists only if the source supports it. |
| `switch` | `entity_id` | Raw switch with on/off feedback. |
| `fan` | `entity_id`, `default_target` | Native fan with on/off feedback. Speed and direction require the corresponding source features. |
| `relay_fan` | `outputs`, `profiles`, `default_target`, `reversal_dead_time` | Exclusive raw switch outputs, complete profiles, and an all-off profile. Relay feedback confirms switches, not physical airflow. |

Minimal cover and switch objects are
`{"name": "Vent", "kind": "cover", "entity_id": "cover.vent"}` and
`{"name": "Light", "kind": "switch", "entity_id": "switch.light"}`.
A native fan also needs a default target, for example
`{"name": "Supply", "kind": "fan", "entity_id": "fan.supply", "default_target": {"on": true}}`.
Only add `percentage` or `direction` when the source supports those commands.

A relay fan requires the complete [relay fan example](../examples/relay-fan-resource.json).
`outputs` is a nonempty list of switch entities. Each named `profiles` object
contains an `outputs` mapping with a Boolean for every owned switch; it can also
include `percentage` from 0 to 100 and `direction` of `forward` or `reverse`.
Include an all-off profile and at least one on profile. `default_target` names an
on profile, such as `{"profile": "supply_low"}`. `reversal_dead_time` is 0.001 to
3600 seconds with all outputs off before reversal. Select a hardware-safe delay.
No other resource can own any of these outputs.

The following optional fields apply to resources.

| Field | Default | Contract |
| --- | --- | --- |
| `retry_interval` | 300 seconds | Positive interval between retries of unmet intent. |
| `command_interval` | 30 seconds | Positive minimum spacing for normal commands. Supported STOP bypasses pacing and invalidates pending commands. |
| `movement_timeout` | 120 seconds | Positive time to defer another command while physical feedback reports movement. A decision can already report target not reached; this guard bounds the wait before retry. |
| `tolerance` | 2 percentage points | Cover position tolerance from 0 to 100. |
| `manual_duration` | 1800 seconds | Positive lease duration when a request supplies no expiry. |
| `manual_control` | `true` | `false` removes managed command entities and manual controls; manual request, release, and STOP are rejected. Source-following policy intent still runs. |
| `restriction_entity` | None | On blocks dispatch; unknown or missing usable feedback also blocks. It does not erase intent. |
| `fault_entity` | None | On or unknown/missing usable feedback inhibits dispatch. |
| `return_monitor` | None | Cover-only object with `target_at_most` from 0 to 100 and positive `warning_after_seconds`. Warns about an unconfirmed effective return using raw feedback and the resource tolerance. |

The default fan target supplies a native ON command, not an automatic return
policy. Lease expiry withdraws the override; configure a baseline policy when a
resource must return to a target. Command acknowledgement never confirms position
or airflow. Manual closure remains allowed even when airflow becomes unmet.

## Configure a policy

A policy needs `name`, existing `resource_id`, `kind` (`state` or `occurrence`),
and a target source. `priority` defaults to 0 and accepts integers from -1000000
to 1000000; larger values win among eligible policies. Manual leases and airflow
requirements have separate precedence.

| Target source | Fields | Contract |
| --- | --- | --- |
| Fixed target | `target` | Capability-valid object with `position`, `on`, `percentage`, `direction`, or configured `profile`, according to the adapter. |
| Native source | `target_entity`, optional `target_attribute`, `target_field` | Read a source state or attribute into `position` (default), `on`, `percentage`, or `direction`. Missing/unknown values do not provide a usable target. |
| Desired control | `intent_id` | Existing desired control subentry ID. Only for a state policy and a switch resource; excludes all other target fields. |

A state policy stays eligible while its source qualifies. Optional
`eligibility_entity` must match `eligibility_state`, which defaults to `on`.
An occurrence policy needs a bounded, distinct event submitted by an action or
native timer input. Disabling a policy suppresses pending occurrences; it does
not suppress future distinct events after re-enabling.

Optional `input` replaces the eligibility fields. It has one of these shapes:

| Input | Required fields | Behavior |
| --- | --- | --- |
| `qualified_numeric` | `type`, sensor `entity_id`, `comparison: "below"`, finite `threshold`, exact native `unit`, positive `qualification_seconds` | State policy only. Cold reports start qualification; warm reports reset it. Unknown feedback preserves the episode and deadline but cannot qualify without fresh valid recovery evidence. |
| `timer_episode` | `type`, timer `entity_id`, positive `qualification_seconds`, positive `request_seconds` | Occurrence policy only. One native timer episode produces at most one bounded accepted occurrence. Reload interrupts the episode; diagnostics do not own it. |

## Configure an airflow requirement

An airflow requirement needs `name`, a nonempty `activation_entities` list, and a
nonempty ordered `providers` list. All activation sources must be on. Off means
inactive; unknown feedback remains unknown. `acquisition_timeout` defaults to
120 positive seconds before considering an alternative provider.

Each provider needs a unique `id` and nonempty `evidence`. An active provider also
needs existing `resource_id` and a capability-valid `target`. A passive provider
has no actuator target. Each evidence predicate needs `entity_id`, `kind`
(`position`, `contact`, `relay`, or `airflow`), `operator` (`eq`, `gte`, or `lte`),
and scalar `value`; `attribute` is optional. Numeric comparisons need finite
numbers. Position evidence must use a raw, non-optimistic cover's
`current_position`. All predicates must match; commands are not confirmation.
Operator never turns extraction off to satisfy an airflow requirement.

## Configure a desired control

A desired control needs `name` and explicit Boolean `initial_value`. The initial
value only seeds an absent saved value. Optional `on_targets` lists existing
desired control IDs in an acyclic graph. An off-to-on command sets those controls
on in one state update. Repeated on and switching off do not change them.
See [desired controls](desired-controls.md) for configuration and migration.

## Call integration actions

Operator registers six actions. All require a loaded entry. Optional
`config_entry_id` selects that entry; omission uses the singleton. Resource
selectors are one `resource_id` or one managed `entity_id`, never both. IDs are
native subentry IDs, not user labels or raw physical source entities.

| Action | Inputs | Response and acceptance |
| --- | --- | --- |
| `ha_operator.request` | Resource selector; `mode` (default `target`); `target` for target mode; optional expiry and `request_id` | Optional response: `accepted`, `request_id`, `resource_id`, `expires_at`. Accepts a manual lease for a live resource with manual control. `hands_off` forbids a target. |
| `ha_operator.release` | Resource selector | No response. Releases the manual lease; eligible automatic requests can resume. |
| `ha_operator.submit_occurrence` | `policy_id`, `occurrence_id`, `expires_at` | Optional occurrence response. Requires an enabled occurrence policy on a live resource. |
| `ha_operator.skip_occurrence` | `policy_id`, `occurrence_id`, `expires_at` | No response. Suppresses that identity, including before submission. |
| `ha_operator.reconcile` | Optional resource selector | No response. Reevaluate one resource or all resources; respects mode, deadlines, restrictions, faults, and pacing. |
| `ha_operator.explain` | Optional resource selector | Required response: fault, current decisions/observations, manual leases, last commands, attempts, and requirements. Read-only explanation is not authoritative device feedback. |

For manual expiry, choose at most one of positive `duration` in seconds, future
`expires_at` in UTC Unix seconds, or `indefinite: true`. Omit all three to use the
resource's `manual_duration`. Optional `request_id` and required `occurrence_id`
have 1–128 characters. Reuse a request ID only for identical intent. A repeated
occurrence ID cannot replay a skipped, canceled, or expired occurrence. Source
updates do not extend absolute deadlines.

Success means the runtime accepted the command. Home Assistant Store saves
state through its normal delayed lifecycle. Physical movement and independent
confirmation can follow later or remain blocked.
Observe mode accepts no new manual requests or occurrences and emits no outputs.
No custom trigger
or condition platform is exposed: use native entity state triggers/conditions
and existing timer events. Native cover, fan, switch, and select actions remain
available according to each entity's capabilities. Supported cover STOP uses
`cover.stop_cover`; see the [STOP contract](architecture.md#manual-control-and-stop).

## Supported native interface

| Platform | Purpose and capability |
| --- | --- |
| `cover` | Position control and independent position feedback. STOP is exposed only when the raw cover supports it. |
| `fan` | Native speed, power, and supported direction, or a finite relay profile bundle with confirmed break-before-make. Preset modes and oscillation are not exposed. |
| `switch` | Observed switch control, persistent policy enablement, and authoritative desired controls. Desired state does not claim follower convergence. |
| `sensor` | Desired, Observed, Reason, fixed control/requirement status, provider, expiry, attempts, and native policy input phase. |
| `binary_sensor` | Manual ownership, airflow unmet, qualified input, and return overdue. Airflow unmet and return overdue use the native problem class. |
| `select` | Persistent observe/live mode. Shadow lock prohibits live control. |
| `button` | Resume automatic ownership and request reconciliation through the runtime. |

All four subentry types support native add, edit, and delete. Loaded entries
reload automatically after supported changes. An active timer qualification or
accepted episode is intentionally interrupted by reload. Normal shutdown and
restart preserve valid saved intent and absolute deadlines; telemetry cannot
renew them. See [troubleshooting](troubleshooting.md).

Operator supports manual overrides, native qualified numeric and timer policies,
held desired controls with ON-only attachments, source followers, and ordered
airflow handover with independent confirmation. It does not add authentication,
network discovery, external protocol clients, or a custom automation rule system.
Use Home Assistant's native state triggers and conditions. Physical feedback,
restrictions, and actuator capabilities remain requirements of the source
integration.
