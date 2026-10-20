# Number and select resources

Operator consumes existing HA actors. Configure any source, helper, group, or
Template behavior independently. Operator creates only its own managed controls
and status entities; deleting/reloading a resource leaves referenced actors and
other consumers intact.

A number resource references `number.example_setpoint`, stores a bound
`unit_of_measurement` (null for unitless), and requires explicit `tolerance` in that
unit. Initial resource creation captures the actor's HA state/service unit, not its
backend native unit. Reload or editing another subentry never recaptures it.

Use `target: {value: 20}` for fixed numeric intent, or existing policy
`target_entity`/`target_field: value` to read an existing desired source. The source
state must expose the same unit; both unitless is valid. For `target_attribute`,
the attribute value is explicitly declared to use the resource's bound unit; do
not borrow the parent entity's unit. Configure any conversion outside Operator.

Values must be finite and within current native bounds. Native step is UI
increment metadata, not a universal allowed-value grid. A step of 1 does not make
2.5 invalid. Never snap, clamp, substitute zero, or reinterpret units. Malformed
bounds/step inhibit control without pretending that independent feedback vanished.

Unit drift invalidates comparison and dispatch for that resource. Desired retains
its bound unit; Observed and the unavailable managed control expose the actor's
actual unit. Reason records both. Equal numbers in different units do not establish
satisfaction. Native HA services skip the unavailable managed control; Operator's
request boundary also rejects incompatible input independently.

For an explicit rebind, load the integration, release active numeric manual and
occurrence requests, and remove fixed numeric policy/requirement/default targets
whose meaning would change. Reconfigure the binding to the actor's current unit,
then configure fresh intent deliberately. Rebinding is rejected while outstanding
values remain or runtime is unavailable; no conversion or saved-state import occurs.

A select resource references `select.example_mode`. Use `target: {option: boost}`
or a source with `target_field: option`. Current native options are authoritative,
including exact empty/whitespace strings when the actor supports them. Invalid
options are never replaced with another option.

An invalid state-policy target makes that candidate ineligible, so a lower eligible
policy can win. Existing explanation shows the rejected candidate and winner.
Previously accepted manual/occurrence targets retain ownership and expiry if later
invalid; their dispatch is blocked until valid again, released, or expired.

Managed values come from observation. A successful `number.set_value` or
`select.select_option` call is not confirmation. Existing workers, pacing,
restrictions, command conditions, generation checks, and drained service execution
remain in force. Mode selects remain separate. `manual_control: false` omits the
managed control and rejects manual requests while ordinary policies remain usable.
