# Native timed window control

A timed opening needs both an expiring request and a return policy. Configure a
fixed, low-priority baseline for each window. Admit explicit openings through
`ha_operator.request`. The request's absolute deadline remains unchanged during
movement, refusal, telemetry updates, and restart.

The configuration builders in
[`window_recipe.py`](../tests/lab/window_recipe.py) generate native HA scripts,
automations, and helper definitions. They accept deployment bindings from the
caller and contain no HA connection. Keep real entity IDs and generated house
configuration in a private deployment directory.

## Command routing

The dispatcher accepts an explicit list of logical covers or its configured
group. It expands the group to individual resources, checks that all are
available and live, and submits requests with one absolute deadline. Use a new
request ID for a new command. An idempotent retry must reuse its original ID and
absolute deadline.

Each request is accepted independently. A later failure does not undo earlier
admissions. The dispatcher reports partial failure, and accepted requests retain
their deadlines. Its restart mode cancels remaining script work; it cannot
revoke requests already accepted by Operator.

Callers must check the dispatcher's `accepted` response before continuing to
other device or helper actions. An HA script stopped with `error: true` can return
an empty service response. An HTTP success response alone does not establish
request acceptance.

Use explicit request scripts for position presets. Native cover scenes can skip
their service call when the observed state already matches. This can leave an
older opening request active after a device closes autonomously. A group's
average position also does not establish that every member reached its target.

## Automatic requests and confirmation

Native policies can own the qualification clocks. In the policy form, choose
**Qualified numeric sensor** for a state policy or **Timer episode** for an
occurrence policy. The next form shows only that input's settings. Remove the
old helper eligibility fields when converting a policy; no helper values or
history are imported. Reconfiguration keeps the policy ID and common settings.

A numeric input requires a sensor, a threshold, an exact source unit, and a
positive qualification delay. It compares strictly below the threshold. Warm
reports reset qualification; unknown reports preserve the episode. Recovery
requires a fresh numeric report before the saved qualification becomes eligible.

A timer input requires a timer, a positive qualification delay, and a positive
request duration. A fresh start begins a new episode. Pause or cancel withdraws
it; resume does not open another window. Natural completion keeps an accepted
request until its expiry. The request expires one qualification delay plus one
request duration after the fresh start.

A full Home Assistant restart retains accepted timer requests with their original
absolute expiry. Operator discards pending timer qualification and waits for a
fresh start. Restoring an active timer does not create an opening. Numeric
qualification retains its deadline but requires a fresh finite report before it
becomes eligible. Restored or optimistic device feedback does not confirm movement.

Reloading or unloading the integration while Home Assistant runs interrupts
pending and accepted timer episodes. Operator closes admissions, drains active
services, and saves current suppression through Store before replacement workers
start. Activity records each interruption. Enabling a disabled diagnostic entity
can reload the entry and interrupt a timer episode. Manual leases, fan settings,
and sleep intent survive ordinary reload.

Store logs write failures through Home Assistant. Crash recovery uses the last
saved state, so a command or withdrawal made before a delayed save can be lost.
The SIGKILL lab cases wait for a normal save before crashing HA.

Cover resources can also configure **Return confirmation**: the highest target
to monitor and the warning delay in seconds. Confirmation uses raw position and
the resource's position tolerance. An overdue return creates one notification
and turns on the **Return overdue** problem sensor. Physical confirmation clears
both. Retrying a command or reporting an unchanged position keeps the deadline.

These settings are optional. Existing helper recipes remain supported as
described below. Keep one controller for each function during a transfer.

### Existing helper recipes

The timer recipe gives each fresh start, including a restart while active, an
identity and absolute expiry. Pause, cancel, and replacement suppress the prior
occurrence. A paused marker distinguishes resume from active restart. It is saved
before withdrawal, so a failed withdrawal cannot turn resume into a new opening.
Startup reconciles that marker with the restored timer state and invalidates
unadmitted qualifications. A timestamp rejects events captured before readiness.
Operator restores already accepted occurrences through Home Assistant Store.

The temperature recipe keeps a qualification deadline in a native helper. A
fresh numeric HA report is required after startup. Unknown values preserve the
episode rather than becoming zero. Counting an offline interval toward the
deadline assumes continuity; it does not prove uninterrupted temperature or
fresh physical measurement. Manual leases retain precedence over the resulting
ordinary policy.

Create each clock's readiness helper and the cold eligibility boolean with
`initial: false`. Startup clears unadmitted timer state or establishes temperature
freshness, then enables that clock's readiness helper last. Qualification actions
reject a closed or unavailable readiness gate. After installing or reloading
these automations, initialize their records before enabling readiness.
The timer record starts as `{"v":1,"phase":"idle","initialized_at":UTC_EPOCH}`;
use `"paused"` when the existing timer is paused. Set `UTC_EPOCH` to the actual
initialization time. Unknown or malformed timer records inhibit admission.

Native helpers track qualification and alert clocks. The Operator response
establishes runtime acceptance of a request or occurrence. Ordinary Store saves
preserve state for reload; a service response does not wait for a disk save.

The confirmation helper reads raw positions individually. It reports true when
any known raw position exceeds the configured threshold, false only when every
raw position is known and within it, and unavailable otherwise. Desired state,
virtual targets, and group averages cannot confirm physical position.

The return alert keeps a separate deadline for an unconfirmed return target,
notifies once per episode, and dismisses the notification on recovery. Its
message includes the current Operator explanation. Repeated telemetry does not
restart the alert clock.

## Validate the configuration

After preparing the pinned images and release archive, run the isolated native
configuration tests:

```sh
uv run --offline --locked python scripts/lab.py prepare --ha-version 2026.9.3
uv run --offline --locked python scripts/lab.py test --ha-version 2026.9.3 --scenario windows
```

Repeat with the second supported HA version. The runner exercises the packaged
integration through native scripts, helpers, scenes, registry changes, and
encrypted HomeKit commands. Simulator effects establish movement and ordering.
Accelerated timing verifies behavior without claiming a production-duration
observation period.
Prepared-image receipts include the lab source hashes. A changed recipe, runner,
or simulator requires preparing the images again before another accepted run.

Use the [migration runbook](migration.md) for ownership transfer and rollback.
Inspect every saved consumer after a registry rename. Keep the group and shared
presets guarded during a staged transfer, then verify the final HomeKit filter
and unrelated accessory identities.

### Synthetic native policy demonstration

The `windows` lab keeps the helper compatibility checks, then disables their
writers and reconfigures the same policy IDs through the native two-step forms.
Its temperature comes from the simulator's independent `window_temperature`
sensor, with a `°C` unit. Read-only diagnostics provide episode and return
deadlines; diagnostic entities stay disabled.

The native checks use a two-second qualification, a 12-second opening request,
and a six-second return warning. The dashboard demonstration uses an 18-second
request. Accepted restart checks use a 180-second request so the original expiry
can survive HA startup. These are accelerated configurations, not observations
of production timing. The existing ten-minute soak remains a separate release
gate with production retry intervals.

Open `/window-native/return` in the retained isolated lab. Its native Lovelace
cards show desired position, raw observed position, reason, effective expiry,
return overdue, and the public ventilation timer. The runner saves five states:
opening, refused opening, expired request with the 7% baseline, overdue return,
and confirmed raw return. It also saves `window-native-evidence.json` with the
clock settings, ordered explanations, typed deadlines, and simulator journal
markers. `window-native-dashboard.json` records the native dashboard definition.

The refusal demonstration injects raw positions through simulator controls and
records that choice in its journal. Commands and physical movement remain
independently journaled. Unknown raw feedback and a virtual cover at 7% cannot
clear the return warning. Clearing refusal permits a paced retry, physical
movement to 7%, and dismissal of the stable notification.

Both supported HA versions must complete the native window cases and save all
five screenshots. Preparing a lab image or passing helper tests alone does not
establish native policy acceptance.
