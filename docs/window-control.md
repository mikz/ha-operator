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

Each admission is durable independently. A later failure does not undo earlier
admissions. The dispatcher reports partial failure, and accepted requests retain
their deadlines. Its restart mode cancels remaining script work; it cannot
revoke requests already accepted by Operator.

Callers must check the dispatcher's `accepted` response before continuing to
other device or helper actions. An HA script stopped with `error: true` can return
an empty service response. An HTTP success response alone does not establish
durable acceptance.

Use explicit request scripts for position presets. Native cover scenes can skip
their service call when the observed state already matches. This can leave an
older opening request active after a device closes autonomously. A group's
average position also does not establish that every member reached its target.

## Automatic requests and confirmation

The timer recipe gives each fresh start an identity and absolute expiry. Pause,
cancel, and replacement suppress the prior occurrence. A resumed timer does not
create a new opening. Startup discards unadmitted timer qualifications; Operator
restores already accepted occurrences through its own durable store.

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

Native helper restoration is weaker than Operator's atomic persistence. Helpers
track qualification and alert clocks, not durable command acceptance. Only the
Operator response establishes acceptance of the request or occurrence.

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

Use the [migration runbook](migration.md) for ownership transfer and rollback.
Inspect every saved consumer after a registry rename. Keep the group and shared
presets guarded during a staged transfer, then verify the final HomeKit filter
and unrelated accessory identities.
