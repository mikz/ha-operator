# Native policy conditions and occurrences

Operator references existing HA entities; it does not create sensors, helpers,
groups, Template entities, or automations for a policy. Use the policy subentry's
native condition and trigger selectors to edit rules.

State policies evaluate `conditions` at startup and on native dependency changes.
An absent or empty list is unconditional. Disabled conditions keep native skipped
semantics. False results and exposed evaluation errors make the candidate
ineligible; missing/unavailable inputs follow the selected native condition.
Native compound truth remains authoritative. Command conditions are separate:
they can block delivery while the selected desired target remains unchanged.

Occurrence conditions apply once at admission, including `submit_occurrence`.
Accepted requests retain their frozen target and absolute expiry; later condition
changes do not revoke them. External submissions have no trigger variables and
keep their existing IDs and deduplication semantics. A trigger condition requires
real admission callback data and cannot be used by continuous policies or command
gates.

Native trigger admission is optional. Configure `triggers`, a positive static
`duration` or an existing numeric `duration_entity`, and optional `cancel_triggers`
on an occurrence policy. A duration source uses its native duration unit and is
sampled once at admission. Its changes never renew accepted requests; an invalid
source declines a new admission and preserves any previous request. When both
fields are configured, the source takes precedence. Cancellation
requires trigger-owned admission configuration. The following is policy subentry
data, not a YAML integration loader:

```yaml
name: Timed ventilation
kind: occurrence
resource_id: RESOURCE_SUBENTRY_ID
priority: 10
target: {position: 50}
triggers:
  - trigger: state
    entity_id: timer.example_ventilation
    to: active
    for: "00:01:00"
conditions: []
duration:
  minutes: 30
cancel_triggers:
  - trigger: event
    event_type: timer.cancelled
    event_data:
      entity_id: timer.example_ventilation
```

For an externally configured duration helper, replace `duration` above with
`duration_entity: input_number.example_ventilation_minutes` and configure that
helper's unit as `min`. Operator references the helper; it does not create it.

One stored trigger-owned request per policy is replaced on a new qualifying
callback and retired on cancellation/expiry. Rejected callbacks leave the previous
request intact. Accepted or cancelled work that remains masked by a manual winner
does not interrupt that manual operation. Changes to the selected occurrence identity
or expiry fence its execution, even when the target value is unchanged.
Cancellation affects no manual or externally submitted request.
It does not cancel a pending native `for` wait unless that wait's predicate changes.

Admission requires an enabled policy and an effectively live resource. Observe,
shadow lock, disable, and live reload detach native waits and withdraw trigger-owned
requests. Returning to live attaches fresh without replay. Shutdown restores
accepted absolute expiry through the existing Store behavior; pending native waits
reset on restart/reload. Real HA restoration events follow native trigger behavior.
Operator does not synthesize startup events. Native HA-start triggers remain usable.

Native registration failures inhibit the policy and create a Repair. If a native
platform raises before returning a remover, hidden listeners may remain even though
their callbacks are fenced. The incomplete-cleanup Repair can require a HA restart;
reload alone is not proof of cleanup. Ordinary attachment Repairs clear after
successful explicit reattachment.

## Upgrade from 0.3.0

Before upgrading, inventory custom `qualified_numeric`/`timer_episode` inputs and
occurrence eligibility rules. Approve native replacement behavior: numeric triggers
can start already-below unarmed, native waits reset on reload, timer resume can
qualify again, and an active-to-active restart need not reset qualification.
Occurrence conditions now apply only at admission, including external submissions.

Wait for active work to expire or approve withdrawal **before** disabling affected
policies. In 0.3.0, use supported reconfiguration with `input_type: legacy` to remove
custom inputs, still disabled and with the same subentry ID/target/priority. Clear
old occurrence eligibility fields. Independently migrate Health consumers to native
helpers and clear Health checks through options. Keep unrelated configuration,
manual leases, and notification checkpoints/deadlines intact.

The incompatible migration preflights every subentry before mutation. Remaining
nonempty legacy input/Health checks or old occurrence eligibility fields block it
with a Repair. State eligibility pairs become native state conditions; native
availability truth can differ from the former filtered comparison. No old episode
is imported or replayed. After upgrade, configure native rules on the same disabled
policies, validate, and enable them deliberately. If unresolved, stay on 0.3.0.
A rejected migration permits a package rollback to 0.3.0 only after verifying
configuration and Store are unchanged. After successful version-4 migration,
restoring old subentry shapes does not lower the entry version. Restore the matching
pre-upgrade native HA backup, including entry version, subentries, Store, and
component. Native restore initiates restart and returns to that snapshot; admissions
made afterward are not preserved. Do not edit `.storage` directly.
