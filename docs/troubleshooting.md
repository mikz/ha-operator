# Troubleshooting

Start with the resource's **Desired target**, **Observed**, **Reason**, and
**Control status**. Desired target shows the effective target. Observed shows
usable raw feedback from the underlying integration; relay or native device
state alone does not prove airflow. Logical Desired switches store requested
intent and do not claim follower convergence. A successful action response confirms durable acceptance,
not movement or convergence.

| Symptom | Check and action |
| --- | --- |
| Setup fails with a configuration Repair | Correct the named field or dependency in the native subentry flow, then select **Reload**. Setup starts no actuator workers until the configuration and saved intent are valid. |
| Setup fails with a storage Repair | Preserve the snapshot and follow the Repair instructions. Restore a valid snapshot or correct storage access, then select **Reload**. Do not replace saved intent with telemetry. |
| A loaded entry reports inhibited storage | Requests cannot persist safely. Preserve the snapshot, correct the cause, and reload. Diagnostic data remains available; it cannot authorize movement. |
| Managed entity and Observed are unavailable | Check every raw source entity, including every relay output. Operator logs one source-loss message and one recovery message per outage. It does not delete the configured device. |
| Managed entity or Observed is unknown | The source exists but does not provide usable feedback. Check its state and position, speed, or relay attributes. Restored, optimistic, or assumed feedback cannot confirm a physical target. |
| Control status stays pending or waiting | Check the physical integration, restrictions, fault feedback, retry interval, and motion guard. Reconcile does not bypass safety checks or pacing. |
| Requests fail in observe mode | Select **Live** only after verifying the raw actuator and policies. A shadow lock must be removed through integration options before live control is permitted. |
| Airflow unmet is on | Check each provider's independent evidence and restrictions. Manual closure remains allowed. Operator never stops extraction as an airflow fallback. |
| A timer policy no longer qualifies after editing configuration | Add, edit, delete, and entity enablement can reload the entry. Reload intentionally suppresses an active timer episode. Start a new episode; do not replay its old occurrence identity. |
| A desired control does not reattach a child | Repeated ON does not replay an ON edge. Check the child and its attachment configuration; use a deliberate OFF-to-ON transition when needed. |

## Inspect diagnostics

In **Settings > Devices & services**, open **HA Operator**, then download
integration diagnostics. Diagnostics contain sanitized configuration, bounded
history, and runtime fault metadata. They are non-authoritative. A failed entry
can return structural/fault information without a running runtime.

Use the `explain` action for the current selection and command state. Enable the
bounded disk trace through integration options when a longer sequence is needed,
then export it with the administrator-only `export_trace` action. Preserve useful
trace evidence before removing the integration. See [Observability](observability.md)
and the [action reference](reference.md#call-integration-actions).

## Preserve names and identity

Secondary labels, control mode options, and fixed status presentation use native
Home Assistant translations. User resource, policy, provider, and Reason values
remain unchanged. Home Assistant owns the reserved `unknown` and `unavailable`
labels; custom ENUM translations do not override their meaning.

Reconfiguration preserves subentry IDs, entity unique IDs, entity IDs, custom
names and icons, disabled settings, and valid saved intent. Deleting a subentry
removes its logical device and entities. Deleting a referenced resource can make
setup fail until you correct or remove its dependents and explicitly reload.
