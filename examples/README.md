# Configuration examples

These JSON files are data templates for native HA Operator subentries. They are
not a Home Assistant backup, a `configuration.yaml` fragment, or an automatic
import format. Every entity ID is fictional. Create the resource subentries
first, then replace `WINDOW_RESOURCE_ID`, `BLIND_RESOURCE_ID`, and
`RELAY_FAN_RESOURCE_ID` with their generated HA subentry IDs.

In the native subentry form, enter scalar fields through their selectors and
structured `target`, `default_target`, `profiles`, and `providers` values through
the object selectors. Reconfiguration replaces the submitted configuration;
omitting an optional field removes it. Display names do not determine subentry
IDs or entity unique IDs.

| File | Subentry type | Purpose |
| --- | --- | --- |
| [window-resource.json](window-resource.json) | `resource` | A position-controlled window with a five-minute retry interval. |
| [blind-resource.json](blind-resource.json) | `resource` | A blind used by the morning occurrence policy. |
| [fan-resource.json](fan-resource.json) | `resource` | A native speed-controlled fan with an explicit turn-on default. |
| [switch-resource.json](switch-resource.json) | `resource` | An on/off resource. |
| [relay-fan-resource.json](relay-fan-resource.json) | `resource` | A reversible fan with complete off, inward, and outward profiles. |
| [ventilation-policy.json](ventilation-policy.json) | `policy` | A state-driven window target. |
| [window-baseline-policy.json](window-baseline-policy.json) | `policy` | A baseline closed target when no higher-priority demand applies. |
| [relay-fan-baseline-policy.json](relay-fan-baseline-policy.json) | `policy` | A baseline off profile after inlet demand or a manual lease ends. |
| [morning-policy.json](morning-policy.json) | `policy` | A morning opening occurrence with explicit submission and skip semantics. |
| [airflow-requirement.json](airflow-requirement.json) | `requirement` | Alternative passive, window, and inward-fan evidence. |

The [daily occurrence blueprint](daily-occurrence.yaml) is a native automation
blueprint. Install it through HA's blueprint workflow and create an automation
with the generated occurrence-policy subentry ID. It submits once per local
calendar date at the selected time, or skips that occurrence when the chosen
sleep-in entity is on. It does not add a startup catch-up trigger.

## Adapt the bindings

Use a baseline policy when ending a request must return the actuator to a known
target. Removing the last request otherwise makes the resource idle and leaves
the device as observed. A resource's `default_target` is a command-normalization
default, not an automatic idle target. The baseline examples have ordinary
priority 0, below the example ventilation policy's 10 and below requirements and
manual leases. Their positions are fixture choices, not production defaults.

Use underlying entities for observations. A helper that reports a virtual window
target is not evidence that a window is open. The airflow example's relay
predicates establish a configured relay-state contract, not measured airflow.
Replace the predicates with actual airflow evidence if that is the required
contract.

The relay example's two-second dead time is a fixture value. Set the production
value from the device's verified electrical and mechanical requirements. Every
profile names every output, including outputs that must be off. An unavailable,
restored, or assumed output cannot confirm a safe reversal state.

The cover examples omit `restriction_entity`; retrying a refused target does not
require a rain sensor. Add a restriction or fault entity only when its meaning
is established for that resource. Position and timing values are demonstration
inputs, not a validated opening size or airflow rate for a building.

## Use occurrence policies

An occurrence policy does not invent a schedule. A native HA automation submits
an explicit occurrence ID and expiry when its scheduled event arrives, or marks
that same occurrence skipped when sleep-in applies. Use a stable ID per intended
event, such as `morning-2026-10-01`, rather than a new random ID on every retry.

If sleep-in ends after that morning's occurrence was skipped, leave the occurrence
consumed. The next morning uses a new ID. Policy disablement, HA restart, and
manual-lease expiry do not turn old occurrences into catch-up work.

The blueprint uses the installation's local date for occurrence identity and UTC
Unix seconds for expiry. Its finite validity window is explicit. Multiple daily
events need distinct occurrence prefixes or policies. Observe mode rejects
submissions, so exercise the complete example only against isolated lab devices
in live mode before deployment.

All resources initially observe. Complete the isolated scenario gates and the
[migration runbook](../docs/migration.md) before enabling live dispatch.
