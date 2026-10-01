# Production migration

Start each resource in observe mode and transfer actuator ownership one resource
at a time. This runbook does not authorize production changes. Implementation and
acceptance testing use an isolated lab; the production inventory is read-only
until a deployment is explicitly authorized.

The [shadow deployment guide](shadow-deployment.md) describes a private inventory,
passive observation, isolated simulation, and later live canary. Installing an observer does not transfer actuator ownership.

For boolean mode synchronization, use the [desired-control migration](desired-controls.md#migrate-existing-controls).
It preserves independent mode values and public controls while retiring the old
mirrors and coupling automation. Existing scheduling automations continue to run.

## Replace window policy helpers

Use this procedure for windows already controlled by Operator. It changes policy
inputs and return confirmation, while keeping resource IDs, policy IDs, manual
leases, and public controls. It imports no helper values or history.

1. Wait until the public ventilation timer and its accepted opening request are
   inactive. Save the policy, resource, helper, automation, dashboard, and bridge
   definitions. Search their consumers before removing anything.
2. Disable the retiring timer, temperature, and return-confirmation writers.
   Wait for their running actions to finish.
3. Install the exact package accepted in the isolated lab. Reconfigure the
   existing cold policy with **Qualified numeric sensor** and the existing timed
   policy with **Timer episode**. Copy their targets, priorities, source entities,
   units, qualification delays, and request duration. Clear the old helper
   eligibility fields. Qualification starts from fresh source observations.
4. Configure **Return confirmation** on each cover. Copy the monitored target
   limit, warning delay, and existing position tolerance. Confirm raw feedback;
   a virtual target does not establish a physical return.
5. Replace helper dashboard cards with native reason, effective-expiry, and
   return-overdue entities. Resolve their stable unique IDs through the entity
   registry. Clear the retired writers' notifications.
6. Verify the saved configuration, effective targets, raw feedback, and the next
   natural timer cycle. Preserve the fan, cover, and desired-switch identities
   in HomeKit. Keep diagnostic entities disabled unless needed.
7. Search consumers again. Take a full HA backup before deleting helper entries,
   then remove the retired helpers and writers. Remove their obsolete HomeKit
   filter references through the native options flow. Keep the public timer.

For configuration rollback, disable the new inputs, restore the saved definitions,
and initialize the old helpers with fresh state before enabling their writers.
The new package still supports those definitions. Keep one controller for each
function; no binary downgrade or stored-state conversion is required.

Reloading the integration interrupts timer-originated requests. Perform the
transfer while they are inactive. A full HA restart preserves already accepted
requests with their original expiry. See [native timed window control](window-control.md)
for these lifecycle contracts and the simulated acceptance cases.

## Inventory the existing control paths

1. Record the deployed Home Assistant version and verify its integration
   capabilities against that release's source and release notes.
2. Record the raw actuator entities, virtual proxies, groups, manual control
   routes, scenes, scripts, automations, timers, and bridge exposures that can
   reach each resource. Include disabled definitions so rollback is deliberate.
3. Read configuration bodies as well as descriptions. Record incomplete searches,
   unread definitions, missing devices, and unverified physical behavior.
4. Identify the actual evidence source for position, contact, relay state, or
   airflow. Record whether the source is restored, assumed, or optimistic, and
   how it obtains physical feedback after restart. A changed HA `last_reported`
   timestamp alone does not prove fresh physical feedback; some producers can
   republish cached state without a restore marker.
5. Preserve the configuration, enabled states, entity IDs, and release archive
   needed for rollback. Keep credentials and private configuration out of shared
   fixtures and documentation.

The following are generic migration patterns, not observations from an installation.
Keep the actual device inventory, thresholds, schedules, histories, and unresolved
control paths in a private deployment record.

| Legacy pattern to investigate | Migration consequence |
| --- | --- |
| A virtual cover target is copied to a raw cover periodically or after a debounce. | Import intent and feedback separately. Enumerate every active sync owner before live takeover. |
| An open-window helper reads desired state. | Exclude it from physical confirmation; use verified raw feedback or a real contact. |
| A scene description differs from its action body. | Base ownership on the resolved actions, including area, floor, and label targets. |
| A virtual fan controls multiple relays. | Document complete profiles, confirmed-off transitions, and device-specific reversal dead time. |
| A manual flag and timer disagree, or start and stop automations have different manual guards. | Choose explicit lease deadlines and align ownership. A long run or idle timer does not establish human intent or physical airflow. |
| Eligibility and sleep state independently affect a morning routine. | Give each occurrence an identity and expiry. Suppression consumes the occurrence without catch-up. |
| Configuration bodies or physical inputs are unavailable. | Record the uncertainty and resolve its effect before claiming complete live ownership. Do not invent missing inputs. |

## Bind and inspect resources

1. Install the exact release archive that passed the required isolated gates.
   Record its SHA-256 hash and the evidence run IDs.
2. Add the integration and resource subentries in observe mode. Bind physical
   entities rather than a legacy virtual proxy when physical feedback is needed.
   For the 0.1.1 shadow rollout, also enable the integration-wide `shadow_lock`
   option. Keep new managed entities out of HomeKit, mobile
   controls, areas, floors, and labels used by aggregate actions.
3. Configure retry cadence, movement timeout, tolerance, and any restriction or
   fault inputs from the device's contract. A rain input is optional.
4. Configure relay profiles with every output specified, including all-off.
   Confirm the required reversal dead time from the actual device design.
5. Add policies using the generated resource subentry IDs. Add requirements only
   after selecting and documenting each provider's evidence contract. Observe
   mode can still report unmet requirements; keep experimental airflow
   requirements in the isolated lab during the initial production shadow period.
6. Inspect explanations for active requests, winning source, lease deadline,
   effective target, last command, observation, and unmet requirements.

Before deleting a resource subentry, reconfigure or remove the policies and
requirements that reference it. Native HA deletion does not enforce this graph.
If a dangling reference prevents setup, preserve the remaining configuration,
correct the affected dependents, and use the integration's native Reload to
resume. Do not remove runtime storage or cascade-delete unrelated subentries.

Observe mode must produce no actuator effects. Keep the existing owner running
while comparing decisions, but account for its effects when interpreting
observations. A matching target at one instant is not evidence of retry,
supersession, manual, or restart behavior.

Observe mode rejects new manual requests and occurrence submissions. Test
schedule inputs in isolated simulations; do not redirect the existing
morning automation into an observe-only resource and expect it to operate the
house. A recorded legacy command or movement never proves HA Operator actuation.

## Transfer one resource

1. Identify every legacy writer to the resource, including direct bridge control
   and scenes that bypass the managed entity.
2. Record and disable the relevant legacy writers. Keep their definitions for
   rollback and preserve unrelated resources' behavior.
3. Route intended manual control through the managed entity or an explicit
   integration request. Preserve access to the raw entity for diagnosis without
   treating its telemetry as a manual lease.
4. Set the resource mode select to `live`. Inspect the effective target before
   this step because eligible automatic requests resume.
5. Verify the approved production behavior through observed feedback and logs.
   Lab failure injection, rain-refusal simulation, relay faults, and crash tests
   remain in the isolated lab.
6. Record the ownership transfer and complete its required acceptance evidence
   before moving to the next resource.

Manual closure must remain permitted. If closure leaves an active fireplace and
extractor without confirmed inlet evidence, report that condition and acquire
another eligible provider if available. Do not stop extraction, bypass the
manual lease, or relabel an unconfirmed provider as ready.

## Roll back or recover

1. Put the affected resource in observe mode to stop further automatic dispatch.
   Use a hands-off lease when the integration remains healthy and temporary
   manual ownership is the intended action.
2. Inspect physical state independently. Use an actual device STOP only when its
   binding supports it; a raw cover with features `7` has no native STOP.
3. Restore the recorded legacy owner and bridge routes without leaving two
   command writers enabled.
4. Keep a HA backup, fault details, release hash, and relevant logs. Follow
   the Repair for unusable restored data, then reload after recovery.
5. Reenter observe mode and repeat the affected gates before another takeover.

Lease expiry and release resume eligible automatic requests. Before resuming live
operation after an extended rollback, inspect that intent and deliberately
release or replace obsolete requests.
