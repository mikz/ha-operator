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

Managed entities report observed state. An accepted request or successful Home
Assistant action does not mean that a device moved. See the
[architecture and acknowledgement boundaries](docs/architecture.md).

## Install a release archive

Keep the existing actuator configuration and automations available for rollback.
Follow the [migration runbook](docs/migration.md) before enabling live control.

1. Download `ha_operator.zip` and `ha_operator.manifest.json` from the same
   [release](https://github.com/mikz/ha-operator/releases/tag/v0.1.0). Verify the
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

The integration registers six actions. Resource actions accept either one
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
control enablement. Resource sensors expose desired target, control status, and
manual expiry; optional diagnostic sensors expose command attempts and next
attempt. Requirements expose status, selected provider, and an unmet indicator.

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
existing archive requires explicit replacement. After a replacement, rerun every
artifact-dependent gate. Publish the exact archive that passed acceptance.

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
