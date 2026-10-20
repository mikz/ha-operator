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

## Native policies and confirmation

Use continuous native `conditions` on a state policy, referencing existing native
sensors or helpers. Configure those entities independently. For timed requests,
use optional native `triggers`, admission-only `conditions`, a positive `duration`,
and optional `cancel_triggers` on an occurrence policy. The
[native policy example](native-policies.md) shows the saved subentry data.
There is no custom qualification or timer episode engine.

A native state trigger matching timer `active` for a minute can qualify again
after resume. An active-to-active restart does not necessarily restart the wait.
Natural finish before qualification prevents admission; after admission it leaves
the frozen request expiry intact unless configured cancellation matches. Native
pending waits reset on reload/restart. Real restoration events follow HA behavior;
Operator does not synthesize startup admissions. Approve these differences before
migrating legacy timer inputs.

Each policy has at most one stored trigger-owned request. A qualifying callback
replaces it with a frozen target and expiry measured from callback time. Rejected
conditions or an invalid target leave the earlier accepted request untouched.
Cancellation withdraws only that policy's trigger-owned request. External request
IDs and deduplication keep their existing behavior.

Observe mode, shadow lock, disable, and live reload detach pending waits and
withdraw trigger-owned requests. Returning live attaches fresh without replay.
Shutdown preserves accepted absolute expiry through the existing Store behavior.
A service response does not wait for a disk save; crash recovery uses the last
saved snapshot. Existing manual leases and unrelated desired controls retain
their persistence rules.

Cover resources can configure **Return confirmation**: the highest target to
monitor and the warning delay. Confirmation uses raw position and the resource's
tolerance. An overdue return creates one notification and turns on **Return
overdue**. Raw confirmation clears both. Retrying or unchanged telemetry does not
restart the deadline. Virtual targets or group averages cannot prove that every
physical cover returned.

### Existing external helper recipes

The recipes in `window_recipe.py` remain independent deployment configuration for
external occurrence submissions and native helper predicates. Operator does not
provision or own their scripts, automations, or helpers. Their legacy episode
semantics differ from native trigger admission; do not silently exchange them.
Keep one writer for each function. Preserve consumer definitions and histories
until their replacement and rollback paths are verified.

## Validate the configuration

After preparing the pinned images and release archive, run the isolated native
configuration tests:

```sh
uv run --offline --locked python scripts/lab.py prepare --ha-version 2026.10.2
uv run --offline --locked python scripts/lab.py test --ha-version 2026.10.2 --scenario windows
```

The runner exercises the packaged
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

The `windows` scenario exercises native conditions and timer state-for admission,
cancellation, pending-wait loss on reload, observe-mode fencing, and accepted
expiry. Independently configured fixture helpers supply continuous cold eligibility;
the simulator supplies physical feedback. Accelerated waits validate ordering,
not a production-duration observation period.

Native dashboard cards show desired and observed position, Reason, effective
expiry, return overdue, and the public timer. Each value opens its underlying
entity details and history. Screenshots and journals establish physical effects,
refusal, expiry, and recovery. Run the exact package on the supported 2026.10.2
baseline; image preparation or helper unit tests alone do not establish acceptance.
