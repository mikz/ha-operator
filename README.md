# HA Operator

HA Operator is a native Home Assistant integration for durable control requests,
manual overrides, scheduled occurrences, and requirements shared by several
devices. It separates a request from the effective target, the last command, and
the device's observed state.

Version 0.1.0 passed local source tests and packaged acceptance runs on Home
Assistant 2026.9.3 and 2026.9.4. Each packaged run passed all 23 runner scenarios;
a separate baseline run verified five-minute retries. Download the
[v0.1.0 release](https://github.com/mikz/ha-operator/releases/tag/v0.1.0) and its
[validation evidence](https://github.com/mikz/ha-operator/releases/download/v0.1.0/ha_operator-evidence.zip).
The [validation record](docs/scenarios.md#release-validation-record) identifies the
tested archive and the limits of this evidence. Automated results are available
in [GitHub Actions](https://github.com/mikz/ha-operator/actions/workflows/validate.yml).
Development requires Python 3.14.2 or later within Python 3.14.
The [0.1.1 release notes](docs/releases/0.1.1.md) describe shadow observation.
[0.1.2](docs/releases/0.1.2.md) fixes partial fan commands from native controls
and HomeKit.

## Control behavior

- Resources start in `observe` mode. They calculate decisions without dispatching
  actuator commands. New manual requests and occurrence submissions are rejected
  in this mode. Set the resource's mode select to `live` after validation.
- A manual target lease overrides requirement and ordinary policy targets. A
  hands-off lease suspends automatic commands. When the lease ends, valid durable
  intent resumes automatically.
- If no request remains, the resource becomes idle without commanding off. Add a
  baseline policy when a resource must return to a target after an override.
- A cover that refuses a position remains pending. The integration retries the
  still-valid target at the configured interval, including when there is no rain
  sensor. Retries have no attempt-count expiry. A replacement request, an expired
  lease, a fault, or hands-off state changes what work remains valid.
- An occurrence can be explicitly skipped. Ending sleep-in, enabling a policy,
  or restarting Home Assistant does not replay a skipped occurrence.
- Airflow requirements can use a passive contact, a reported motorized-window
  position, or configured fan evidence. An alternative must be confirmed before
  releasing an existing provider.
- Manual closure remains permitted when it makes airflow unmet. The integration
  reports the unmet requirement and can acquire another eligible provider. It
  never stops extraction as an airflow fallback.

Fan controls compose separate power, speed, and direction commands against the
latest accepted manual intent. A direction selected while off remains off and
applies to the next on command within that lease. Expiry or release clears the
selection; bare on then uses the configured default.

Managed entities report observed state. An accepted request or successful Home
Assistant action does not mean that a device moved. See the
[architecture and acknowledgement boundaries](docs/architecture.md).

The [native timed window recipe](docs/window-control.md) combines expiring
openings with a return policy, explicit position presets, and raw confirmation.
Its isolated lab scenarios exercise native HA scripts and encrypted HomeKit commands.

[Desired controls and source followers](docs/desired-controls.md) replace mode
synchronization loops with durable native switches. A control can attach another
control on an ON transition while leaving its OFF behavior independent. Followers
can omit manual controls and derive their target directly from committed intent.
The guide includes state import, public-group migration, HomeKit checks, and rollback.
The [event-load measurements](docs/performance.md) document CPU, publication and
allocation behavior with tracing on and off.

## Record a passive shadow trace

Version 0.1.1 adds three integration options:

- `shadow_lock` forces every resource into observe mode and rejects selecting
  live mode. The lock survives reload and restart because it is saved in the
  integration options. It defaults to disabled.
- `trace_enabled` records the configured inputs and decisions in a bounded,
  integration-owned journal. Enabling tracing does not activate a resource.
  It defaults to disabled.
- `trace_entities` selects additional entities to record. It defaults to an empty
  list and does not give HA Operator control of those entities.

For passive observation, enable `trace_enabled` and `shadow_lock` when adding
the integration or editing its options in **Settings > Devices & services**. Keep the existing
automations as the actuator owners. The lock suppresses HA Operator commands;
it does not block commands from those automations or other integrations.

Disabling the lock is an explicit change to the integration options. Before
doing so, inspect every resource's stored mode and accepted intent, and complete
the [ownership-transfer procedure](docs/migration.md). Loading a locked
integration saves its resources in observe mode. Unlocking alone does not restore
their previous live modes; activate each resource explicitly when it is ready.

Use the admin-only `ha_operator.export_trace` action with response data to retrieve the
sanitized journal. `after` is the last sequence number you processed; omit it
for the first page. `limit` defaults to 100 and accepts 1–1000 records; each page
also has a 1 MiB size cap. Follow `next_after` while `more` is true. The
`through_sequence` field identifies the sequence tip captured for that export.
Each response includes the schema version, integration version, component and
configuration hashes, records, pagination, and journal health.
Tracing disabled returns an empty record list and `health.enabled: false`.

Records retain the configured input fields, accepted intent, decisions, and
dispatch context with pseudonymous identifiers. They omit arbitrary entity
attributes, names, and credentials. A new session has a configuration and intent
snapshot. Sequence numbers continue across sessions. The journal reports dropped
records, write errors, rotation, heartbeat time, and cursor gaps. Segment rotation
increments `rotations` without making a continuous retained trace incomplete.
Evicting an old segment sets `history_gap`; dropped records, write errors, and
other completeness guards still prevent a complete replay claim. Preserve the
original health fields, including `complete: false` in older exports.

The journal uses a queue of at most 512 records, a 64 KiB limit per record,
32 rotating 2 MiB files, and a 60-second heartbeat. Recording and export do not
create manual ownership. A raw report or a correlated context is evidence of a
report or command path, not physical confirmation or proof of human intent.

Observe-mode decisions use the inputs that actually arrived. They do not predict
how the house would respond to commands that HA Operator did not issue. Evaluate
that behavior separately with the isolated replay and simulator scenarios in the
[scenario catalog](docs/scenarios.md#shadow-observation-and-replay).

To inspect an exported trace, save a response as `sanitized-trace.json`, or wrap
all pages as `{"schema": 1, "pages": [...]}`. Validate it before preparing a replay:

```sh
uv run python scripts/shadow_replay.py validate sanitized-trace.json
```

After [preparing the isolated lab](#develop-and-validate) with the same release
component, replay the trace against that exact archive:

```sh
uv run python scripts/shadow_replay.py run sanitized-trace.json --ha-version 2026.9.3
```

The replay report compares recorded engine-input snapshots and results at their
recorded times. It flags missing session ancestry, gaps, and incomplete snapshots.
The native HA probe separately loads the last recorded state for each entity
while the shadow lock is active; it uses the lab's current clock. It does not
reproduce historical native timers or HomeKit delivery. Both steps preserve
their scope in `artifacts/lab/<run_id>/shadow-replay.json`; physical consequences
from the original recording remain `not_observed`.

For the [seven-day observation](docs/shadow-deployment.md), preserve hourly export
pages unchanged and list them in `hourly-index.json` as
`{"schema": 1, "pages": [...]}`. Each entry contains `file` (relative to the
index), `sha256` (the original page hash), `request_after` (the requested cursor,
or `null` initially), and `capture_at` (UTC Unix seconds). Use `roles.json` to map
cellar roles to sanitized entity aliases, and choose a new output file:

```sh
uv run python scripts/shadow_archive.py hourly-index.json --bindings roles.json --output seven-day-report.json
```

This offline report streams continuity checks and observed cellar-state episodes
across the archive. It retains the recorder's original health flags and reports
sequence/heartbeat continuity separately. A rotation counter alone does not prove
loss, and an original `complete: false` is never rewritten as true. A complete
observed interval does not establish coverage of all seven days. This analysis
does not run the bounded engine or native replay and makes no physical motor,
airflow, or manual-actor claims.

## Install a release archive

Keep the existing actuator configuration and automations available for rollback.
Follow the [migration runbook](docs/migration.md) before enabling live control.

1. Download `ha_operator.zip` and `ha_operator.manifest.json` from the same
   [release](https://github.com/mikz/ha-operator/releases/latest). Verify the
   archive against the manifest and preserve its hash with the release evidence.
   Local builds place these files under `dist/`.
2. Extract the archive into Home Assistant's
   `/config/custom_components/ha_operator/` directory. The archive contains the
   integration's files directly, without a containing directory.
3. Restart Home Assistant, and add the **HA Operator** integration through
   **Settings > Devices & services**.
4. Add resource, policy, and requirement subentries. Use the
   [sanitized configuration examples](examples/README.md) as data templates.
5. Inspect the generated resource mode selects and decision entities in observe
   mode. Complete the [scenario catalog](docs/scenarios.md) in an isolated lab
   before taking over actuator ownership.

Use native Home Assistant actions for managed covers, fans, and switches. Change
resource mode through `select.select_option`. The underlying adapter's features
govern supported commands; a virtual entity cannot add STOP or speed control to
an actuator that lacks it.

## Integration actions

The integration registers seven actions. Resource actions accept either one
`resource_id` or one managed `entity_id`, never both. `config_entry_id` is
optional for the singleton integration.

| Action | Inputs and result |
| --- | --- |
| `ha_operator.request` | Create a manual `target` or `hands_off` lease. Accepts optional response data with `accepted`, `request_id`, `resource_id`, and `expires_at`. |
| `ha_operator.release` | Release the resource's manual lease and resume valid automatic intent. |
| `ha_operator.submit_occurrence` | Accept `policy_id`, `occurrence_id`, and UTC Unix `expires_at`. Optional response returns the durable occurrence record. |
| `ha_operator.skip_occurrence` | Mark the specified `policy_id` and `occurrence_id` skipped through its supplied UTC Unix `expires_at`. |
| `ha_operator.reconcile` | Reevaluate one resource, or all resources when no target is supplied. Existing mode, lease, fault, and command guards still apply. |
| `ha_operator.explain` | Return response data for one resource, or all resources: revision, fault, decision, observation, manual lease, last command, attempts, and requirements. |
| `ha_operator.export_trace` | Admin-only action that returns a sanitized, versioned journal page. Accepts optional `config_entry_id`, `after`, and `limit`; does not accept a resource target or change control state. |

For `request`, `mode` defaults to `target`, which needs a capability-valid `target`
object. `hands_off` forbids a target. Choose at most one of `duration` in seconds,
`expires_at` as UTC Unix seconds, or `indefinite: true`. Omitting all three uses the
resource's `manual_duration`, which defaults to 1800 seconds. An optional
`request_id` has 1–128 characters; reuse it only for identical intent. Reuse with
different intent is rejected.

Occurrence IDs also have 1–128 characters. Expiry must be a future UTC Unix
timestamp. Use a stable ID for one intended event; retrying the submission must
not invent another occurrence. The [daily occurrence blueprint](examples/daily-occurrence.yaml)
demonstrates submission and sleep-in skipping through native HA automation.

Resource buttons provide **Resume automatic** and **Reconcile**. Policy switches
control enablement. Each resource exposes **Desired target**, **Observed**, **Reason**,
**Control status**, and **Manual expiry** on its native device page. Cover positions
use percentages, so Desired and Observed can share a History graph. Desired carries
the selected source and request identity as attributes; Reason provides a separate
timeline when the cause changes at the same target. Optional diagnostic sensors
expose command attempts and next attempt. Requirements expose status, selected
provider, and an unmet indicator. See [status and history](docs/observability.md)
for attribute contracts and the isolated browser demo.

Use `select.select_option` for resource mode and `cover.stop_cover` for supported
managed covers. There are no custom `ha_operator.set_mode` or `ha_operator.stop`
actions. See the [STOP durability limitation](docs/architecture.md#manual-control-and-stop).

## Develop and validate

Install the locked development dependencies with `uv sync --locked`. Development
and acceptance tests must never connect to a production Home Assistant instance.
The isolated lab contains Home Assistant, a simulator, and a test runner, with no
production credentials, host networking, published ports, or external routes.
Build dependencies before starting that runtime.

Run the Python checks and generate coverage evidence:

```sh
uv run pytest --cov --cov-report=json:artifacts/coverage.json --junitxml=artifacts/pytest.xml
uv run python scripts/coverage_gate.py artifacts/coverage.json
uv run python scripts/mutation_gate.py --output artifacts/mutations.json
```

The coverage gate requires more than 95% line coverage for every production Python
module and 100% line and branch coverage for the config flow. The mutation gate checks selected decision
and dispatch guards; inspect its report rather than treating execution as proof
that every mutation was detected.

The configured test paths include unit tests, HA integration fixtures, and the
lab isolation validator tests. They do not start the packaged lab. CI repeats
the fixture suite using the independently locked
[2026.9.4 compatibility environment](compat/ha2026.9.4/pyproject.toml), runs HACS
and hassfest validation, and requires the separate packaged lab jobs.

Build and verify the release artifact:

```sh
uv run python scripts/release.py build
uv run python scripts/release.py verify --against-source
```

The build produces `dist/ha_operator.zip` and a manifest with archive and file
hashes, the version, the source revision, and dependency-lock digests. A differing
existing archive or manifest requires explicit replacement. After a replacement, rerun every
artifact-dependent gate. Publish the exact archive that passed acceptance.
The manifest also records an installed-component fingerprint over the Python,
JSON, and YAML files. Trace export uses this fingerprint to identify the installed
code independently of ZIP metadata. It does not replace the archive hash.

Prepare images before testing the isolated runtime:

```sh
uv run python scripts/lab.py prepare --ha-version 2026.9.3
uv run python scripts/lab.py test --ha-version 2026.9.3 --scenario all --timeout 1800
uv run python scripts/lab.py test --ha-version 2026.9.3 --scenario soak --timeout 1500
uv run python scripts/lab.py prepare --ha-version 2026.9.4
uv run python scripts/lab.py test --ha-version 2026.9.4 --scenario all --timeout 1800
```

Lab results belong under `artifacts/lab/<run_id>/`. Preserve the summary,
isolation evidence, simulator effects journal, HomeKit transcript, browser
evidence, logs, and crash records. Each scenario needs an explicit outcome;
unavailable or unexecuted checks are not passes. See the
[scenario-to-layer mapping](docs/scenarios.md) for the required behavior.
Full runs also preserve exported shadow traces, replay results, and the scoped
Docker cleanup receipt. A release bundle rejects missing scenarios, incomplete
traces used for replay, and evidence for another archive.

## Documentation

- [Architecture](docs/architecture.md): resolution, persistence, acknowledgement,
  adapter, and lifecycle contracts.
- [Scenario catalog](docs/scenarios.md): stable IDs, validation layers, and expected
  outcomes.
- [Migration](docs/migration.md): inventory, observe mode, ownership transfer, and
  rollback.
- [Examples](examples/README.md): resource, policy, and requirement data.
- [Development boundary contract](docs/development-contract.md): shared internal
  interfaces and ownership.
- [0.1.1 release notes](docs/releases/0.1.1.md): shadow observation changes and
  release-specific validation.
