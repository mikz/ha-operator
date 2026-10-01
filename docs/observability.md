# Status and history

Each resource has one native HA device containing its managed entity and status
sensors. Existing Desired target and Control status identities remain unchanged.
Observed and Reason are new entities; their history starts when HA creates them.

| Entity | Meaning |
| --- | --- |
| Desired target | Current effective target after arbitration. Its `target` attribute contains the full structured target. |
| Observed | Adapter-validated feedback. A missing source or a source reporting `unavailable` makes this entity `unavailable`. A reachable source with unknown, invalid, restored, assumed, optimistic, or missing value feedback produces `unknown`. |
| Reason | Readable selection cause: a configured policy name, manual ownership, or an airflow requirement and its active inputs. |
| Control status | Execution outcome, such as satisfied, waiting, hands-off, or observe. |
| Manual expiry | Absolute deadline for the current manual lease. |
| Effective expiry | Absolute deadline of the selected manual lease or policy occurrence. Ordinary state policies have no expiry. |
| Return overdue | Problem indicator when the effective cover return target has not been confirmed within the configured warning period. |

Cover Desired and Observed use `%`. Observed has the `measurement` state class;
Desired has no state class. Both support ordinary Recorder history. Relay fans
report profile names. Native fans and switches retain their scalar state and
structured target attributes. Relay feedback does not prove fan rotation or
measured airflow; requirements still need their configured confirmation evidence.

Native input policies also provide **Input phase** (enum) and **Qualification
due** (timestamp). Numeric inputs provide **Qualified** (binary sensor), which
is unknown before recovery evidence. These read-only diagnostic entities start
disabled. Runtime qualification and deadlines continue while they are disabled.
Enabling a disabled entity can make Home Assistant reload the entire integration
entry. This invokes input recovery; enable diagnostics while the ventilation
timer and its opening request are inactive. Disabling a diagnostic removes its
view without stopping the input runtime.

Desired, Observed, Reason, Effective expiry, and Return overdue are operational
entities. Policy-enabled switches and operating-mode selectors use `CONFIG`.
Diagnostic availability does not establish physical feedback or command acceptance.

## Selection attributes

Desired and Reason carry the same bounded selection snapshot:

- `source_kind`: `manual`, `policy`, `occurrence`, `requirement`, `idle`, or `unknown`.
- `source_id` and `source_name`: configured identity and readable name. For manual
  commands, the source identifies the ingress (`service`, `entity`, or `stop`),
  not a person.
- `selection_reason`: the readable cause shown by Reason.
- `request_id`, `occurrence_id`, and `expires_at`: applicable intent identity and
  absolute UTC deadline; otherwise null.
- `related_entities`: configured eligibility, native policy input, target, or activation inputs.
- `active_inputs`: active members of requirement inputs, captured during evaluation.
- `managed_entity`, `desired_entity`, `observed_entity`, `reason_entity`,
  `status_entity`, and `effective_expiry_entity`: registry-resolved references
  that follow entity renames.

Entity references are data for templates, APIs, and dashboard cards. HA does not
automatically turn arbitrary entity-ID attributes into clickable links. Put the
native entities in cards or open their shared device page.

Control status preserves its existing `reason` and `source` attributes.
`execution_reason` aliases `reason`; it explains the execution result. For example,
Desired may select an airflow provider while execution reports “target not reached.”
The operator does not label a refusal as rain without a restriction signal.

Selection comes from the same synchronous evaluation as the target. Group
expansion is bounded to 32 entities. Changing a group's active members updates
the explanation even if the group's aggregate state stays on. Unchanged
telemetry does not add timestamps or revisions to these attributes.

## History and Activity

Add Desired and Observed to a native History graph. Add Reason to a separate
timeline. Recorder captures changed attributes as well as changed states, so a
new winning policy remains visible when the desired position stays the same.
History depends on the installation's Recorder inclusion and retention settings;
these sensors do not reconstruct events from before installation.

Activity records target, source, and meaningful outcome transitions with the
managed entity reference. Each physical service call receives a new context.
When an accepted request or occurrence has a known ingress context, the dispatch
context references it as its parent. Context is not durable: after restart,
request IDs and deadlines survive but the original context is unavailable.
Telemetry never establishes manual ownership or identifies a user.

`ha_operator.explain` includes the selection snapshot, candidates, rejection
reasons, observation, lease, and requirement predicates. Downloadable diagnostics
remain allowlisted and omit private labels and entity IDs.
Native input and return-monitor state is bounded in diagnostics and the initial
trace snapshot. Trace input sources include sensor unit metadata. Unit strings,
episode identities, and configuration fingerprints use opaque aliases in exports.

Keep histories from earlier template helpers separate. Reusing their entity IDs
does not reliably merge Recorder history with the new native entities.

## Repeat the isolated browser demo

Build the release ZIP, then prepare dependencies before starting the offline lab:

```sh
.venv/bin/python scripts/release.py build
.venv/bin/python scripts/lab.py prepare --ha-version 2026.9.3
.venv/bin/python scripts/lab.py test --scenario observability --keep
.venv/bin/python scripts/lab_preview.py <run-id>
```

Open the printed loopback URL in a host browser. Use the disposable credentials
in `.lab/runs/<run-id>/control/preview-login.json`; keep that file private.
The preview validates the retained run's image, network membership, routes, and
artifact before opening a host-only TCP relay to HA. It adds an inbound path
through `docker exec`; it does not publish a Docker port or add an outbound route.
No production credentials or devices enter the lab.

The native dashboard shows a refused window target and a relay that refuses to
energize. The test also verifies a reason change without another actuator command,
manual expiry, Recorder attributes, and linked Activity. Evidence includes the
independent simulator journal, history, Activity, screenshot, and browser trace.

Stop the preview process after the demonstration. Remove that run's containers,
network, and volumes with its saved Compose configuration:

```sh
docker compose -p <run-id> -f artifacts/lab/<run-id>/compose.json down --volumes
```

Delete the private preview login file after cleanup. Use the same scenario without
`--keep` for automatic cleanup in compatibility tests.

## Interpret source outages

Managed observation entities and **Observed** distinguish source availability
from usable physical feedback. Missing sources and sources reporting
`unavailable` make these entities unavailable. A present source with an unknown
or unusable value keeps them available with an unknown state. **Desired target**,
**Reason**, and durable intent remain available during an outage. The execution
status can still report unavailable when feedback is unusable; source presence
alone does not permit a command or confirm airflow.

Operator logs one INFO message when a resource's source becomes unavailable and
one when the source returns. For a relay resource, every owned output must be
present and not report unavailable. Returning as unknown restores source
availability only. Repeated source reports and reconciliations do not log another
transition or extend accepted deadlines. The distinction can affect dashboards,
automations, and History that previously treated all unusable values as unknown.

Native entity calls have no platform-wide concurrency semaphore. The runtime
serializes durable admission and owns one worker per resource, including every
output of a relay fan. Independent resources can make progress while another
physical command waits. Same-resource supersession and physical STOP retain the
runtime's generation fences, transport settlement, and pacing rules.
