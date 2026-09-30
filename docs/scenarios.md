# Acceptance scenarios

This catalog maps expected behavior to implemented test assertions. The test-node
column records source coverage, not a passing result. `Partial` notes identify
claims that the named tests do not establish. Execution status comes from a run
record with the source or archive hash, Home Assistant version, command, and
outcome. Collection and test presence alone are not passes.

Lab claims require the completed packaged execution evidence recorded below.
Unlisted or pending runs do not establish a pass. Fixture reload is not process restart, mocked service
capture is not physical feedback, and successful pairing is not HAP control.

## Validation layers

| Layer | Boundary and evidence |
| --- | --- |
| Pure | Deterministic resolver, normalization, occurrence, lease, and requirement transitions with an explicit clock. |
| HA | Native config entries and subentries, entity capabilities, services, context, listeners, persistence, and lifecycle in HA fixtures. |
| Lab | The exact release ZIP in isolated Home Assistant, real API/UI/HomeKit routes, and an independent simulator effects journal. |
| Migration | Read-only inventory and an explicitly authorized, bounded ownership transfer on the actual installation. |

Pure and HA tests use the baseline Home Assistant 2026.9.3 fixture pairing and the
2026.9.4 compatibility pairing from the lock files. Lab runs use the same archive
hash on both versions. New contracts require new cases even if an older private
test collection is unavailable; an inaccessible test-file count is not coverage.

## Test-node prefixes

Expand each prefix to the linked test file to form a pytest node ID. For example,
`reconcile::test_hidden_refusal_retries_without_changing_admitted_intent` means
`tests/integration/test_runtime_reconciliation.py::test_hidden_refusal_retries_without_changing_admitted_intent`.
Parameterized names refer to all cases of that test.

| Prefix | Test file |
| --- | --- |
| `core` | [tests/unit/test_core.py](../tests/unit/test_core.py) |
| `properties` | [tests/unit/test_properties.py](../tests/unit/test_properties.py) |
| `configuration` | [tests/unit/test_configuration.py](../tests/unit/test_configuration.py) |
| `storage` | [tests/unit/test_storage.py](../tests/unit/test_storage.py) |
| `release` | [tests/unit/test_release.py](../tests/unit/test_release.py) |
| `coverage` | [tests/unit/test_coverage_gate.py](../tests/unit/test_coverage_gate.py) |
| `adapters` | [tests/integration/test_adapters.py](../tests/integration/test_adapters.py) |
| `requests` | [tests/integration/test_runtime_requests.py](../tests/integration/test_runtime_requests.py) |
| `reconcile` | [tests/integration/test_runtime_reconciliation.py](../tests/integration/test_runtime_reconciliation.py) |
| `boundaries` | [tests/integration/test_runtime_boundaries.py](../tests/integration/test_runtime_boundaries.py) |
| `entities` | [tests/integration/test_entities.py](../tests/integration/test_entities.py) |
| `flows` | [tests/integration/test_config_flow.py](../tests/integration/test_config_flow.py) |
| `lifecycle` | [tests/integration/test_lifecycle.py](../tests/integration/test_lifecycle.py) |
| `removal` | [tests/integration/test_subentry_removal.py](../tests/integration/test_subentry_removal.py) |
| `services` | [tests/integration/test_services.py](../tests/integration/test_services.py) |
| `isolation` | [tests/lab/test_isolation.py](../tests/lab/test_isolation.py) |

## Intent, observation, and retries

| ID | Required layers | Expected outcome | Implemented test nodes and limits |
| --- | --- | --- | --- |
| TARGET-SEPARATION | Pure, HA, Lab | Request, effective target, last dispatch, and raw observation differ without overwriting one another. Managed state stays observed. | `services::test_request_receipt_follows_real_atomic_file_and_release`; `entities::test_resource_sensors_keep_intent_separate` |
| TARGET-IDLE | Pure, HA | With no valid request, become idle without inventing a target or commanding off. A configured baseline policy supplies an explicit return target when required. | `core::test_no_request_is_idle_and_does_not_invent_target`; `requests::test_release_manual_resumes_surviving_policy_command` |
| OBSERVE-ZERO | HA, Lab | Active requests and requirement decisions in observe mode cause zero actuator effects, including delayed work and restart reconciliation. | `reconcile::test_observe_rejects_manual_and_never_dispatches_policy`; `lifecycle::test_real_setup_observe_registry_and_reload` |
| OBSERVE-ADMISSION | HA, Lab | New manual requests and occurrence submissions in observe mode fail clearly without a success receipt or actuator effect. Explicit occurrence skips remain available. | `requests::test_observe_rejects_request_without_saving_or_actuating`; `requests::test_observe_rejects_occurrence_without_saving_or_actuating`; `requests::test_occurrence_skip_before_submit_and_disabled_no_replay` |
| COVER-RAIN-RETRY | Pure, HA, Lab | A valid open target refused by the simulated device remains pending and retries at the configured cadence, indefinitely, without requiring a rain sensor. | `reconcile::test_hidden_refusal_retries_without_changing_admitted_intent`; `core::test_hidden_rain_keeps_intent_until_device_reaches_target` |
| COVER-EXPIRY | Pure, HA, Lab | An expired target lease no longer drives retries. The next effective target comes from surviving intent. | `reconcile::test_expiry_wakes_without_telemetry_and_removes_old_retry`; `reconcile::test_expiry_during_suspended_actuation_cannot_send` |
| COVER-SUPERSESSION | Pure, HA, Lab | A replacement target invalidates queued and delayed effects for the previous generation. | `reconcile::test_superseded_generation_cannot_send_after_async_boundary`; `properties::test_replacing_intent_never_resurrects_superseded_target` |
| TARGET-ATTRIBUTE | HA | A dynamic target attribute change triggers reevaluation even if the source state string does not change. | `reconcile::test_policy_attribute_target_changes_without_state_transition` |
| TARGET-RESTRICTION | Pure, HA, Lab | A configured restriction or fault inhibits the affected commands and explains why; clearing it reevaluates still-valid intent. | `reconcile::test_unknown_safety_input_inhibits_until_explicit_clear`; `adapters::test_generation_and_restrictions_checked_at_each_call` |
| TARGET-UNAVAILABLE | Pure, HA, Lab | Unknown or unavailable actuator feedback does not report achievement or provider readiness. Recovery reconciles valid intent. | `reconcile::test_nonphysical_observation_inhibits_dispatch_and_recovers`; `reconcile::test_capability_loss_does_not_kill_worker` |
| TARGET-RATE | Pure, HA, Lab | Retry and command intervals prevent an unbounded command loop while allowing eventual retries. | `reconcile::test_unchanged_telemetry_does_not_renew_lease_or_starve_retry`; `reconcile::test_inflight_motion_uses_movement_timeout_before_retry` |
| TARGET-TOLERANCE | Pure, HA | Position tolerance and movement timeout use configured boundaries without fabricating a physical arrival. | `core::test_observed_matching_respects_all_fields_and_availability`; `reconcile::test_initial_motion_has_bounded_timeout` |
| TARGET-FEEDBACK-CONTRACT | HA, Lab, Migration | Reject explicit nonphysical feedback after restart. Treat raw-producer cache freshness as a binding contract; HA publication time alone does not establish a hardware measurement. | `requests::test_valid_saved_intent_waits_for_non_restored_observation`; `reconcile::test_nonphysical_observation_inhibits_dispatch_and_recovers`; partial: unmarked cached-source freshness cannot be established generically. |

## Manual ownership and scheduled occurrences

| ID | Required layers | Expected outcome | Implemented test nodes and limits |
| --- | --- | --- | --- |
| MANUAL-TARGET | Pure, HA, Lab | A manual target wins over requirements and ordinary policies for its lease; those lower-priority requests remain recorded. | `core::test_manual_beats_provider_beats_arbitrarily_high_policy`; `reconcile::test_requirement_fallback_cooldown_and_manual_last_inlet_close` |
| MANUAL-HANDS-OFF | Pure, HA, Lab | A hands-off lease causes zero automatic effects until expiry or release, including effects already waiting in an adapter. | `requests::test_hands_off_release_and_resource_validation`; `core::test_hands_off_never_acquires_but_observed_route_still_satisfies`; partial: adapter interruption uses generation tests. |
| MANUAL-RESUME | Pure, HA, Lab | Lease expiry or release automatically reevaluates durable intent rather than replaying a cached prior command. | `requests::test_release_manual_resumes_surviving_policy_command`; `requests::test_manual_expiry_exact_deadline_dispatches_current_policy_target`; `requests::test_hands_off_release_and_resource_validation` |
| MANUAL-PROVENANCE | HA, Lab | Explicit managed control establishes a lease. Raw telemetry, unknown context, and unrelated service calls do not invent manual ownership. | `requests::test_raw_context_never_creates_or_renews_manual_ownership` covers user, parent, absent, and operator-correlated context; `properties::test_telemetry_cannot_renew_lease`; `reconcile::test_unchanged_telemetry_does_not_renew_lease_or_starve_retry`. The context matrix is HA fixture coverage. |
| MANUAL-INDEFINITE | Pure, HA | An explicitly indefinite lease has no synthetic timeout and survives restart until release. | `requests::test_indefinite_manual_target_survives_runtime_restart`; `requests::test_default_explicit_and_indefinite_leases`; fixture runtime restart does not replace full process evidence. |
| COVER-STOP | HA, Lab | Supported physical STOP suppresses stale automatic work; unsupported raw STOP is not advertised or faked by setting the current position. | `requests::test_stop_precedes_durable_hands_off_and_bypasses_pacing`; `entities::test_cover_commands_do_not_claim_motion`; `requests::test_unsupported_stop_still_persists_hands_off` |
| COVER-STOP-BARRIER | HA, Lab | Send immediate STOP, drain a prior in-flight service, and send final STOP if the same lease still owns the resource. Acknowledgement waits for the barrier and durable save. | `boundaries::test_stop_fences_an_inflight_delayed_device_command`; partial: device-internal queues after service return are outside this boundary. |
| COVER-STOP-UNSAVED | HA, Lab | Live STOP attempts the physical stop even when persistence fails. The current process inhibits further actuation and reports the unsaved hands-off lease; crash recovery does not claim that lease survived. Observe mode sends no STOP. | `requests::test_stop_save_failure_can_resume_prior_intent_after_runtime_restart`; `requests::test_stop_save_failure_retains_emergency_hands_off`; abrupt process crash still needs Lab. |
| OCCURRENCE-SLEEP-IN | Pure, HA, Lab | The selected morning occurrence is consumed as skipped. Ending sleep-in after its trigger does not cause catch-up. | `services::test_occurrence_submit_response_suppression_and_no_catchup`; `properties::test_tombstone_prevents_replay_regardless_admission_order` |
| OCCURRENCE-IDENTITY | Pure, HA, Lab | Repeating the same occurrence ID is idempotent. A distinct next-day occurrence can run if eligible. | `services::test_occurrence_submit_response_suppression_and_no_catchup`; `requests::test_occurrence_dynamic_target_frozen_and_duplicate_cannot_extend` |
| OCCURRENCE-EXPIRY | Pure, HA, Lab | An occurrence that expires while HA is stopped is not replayed on restart. | `requests::test_expired_occurrence_never_replays_after_runtime_restart`; `requests::test_occurrence_dynamic_target_frozen_and_duplicate_cannot_extend`; full process evidence remains Lab. |
| OCCURRENCE-DISABLE | Pure, HA | Disabling a policy tombstones pending occurrences. Reenablement permits future occurrences without resurrecting discarded ones. | `requests::test_occurrence_skip_before_submit_and_disabled_no_replay` |
| OCCURRENCE-TARGET-SNAPSHOT | Pure, HA | An admitted occurrence retains its captured target when the policy's dynamic target changes. Duplicate submission cannot extend its expiry or replace that snapshot. | `core::test_occurrence_target_is_frozen_at_admission_despite_current_policy_change`; `requests::test_occurrence_dynamic_target_frozen_and_duplicate_cannot_extend` |

## Requirements and fan adapters

| ID | Required layers | Expected outcome | Implemented test nodes and limits |
| --- | --- | --- | --- |
| AIRFLOW-ACTIVATION | Pure, HA, Lab | Fireplace and extraction activation inputs must both be on. An off input makes the requirement inactive; otherwise an unknown input keeps activation unknown. | `reconcile::test_conjunctive_airflow_activation_releases_only_its_policy_override`; `reconcile::test_unknown_evidence_never_confirms_and_activation_unknown_is_inert` |
| AIRFLOW-ALTERNATIVES | Pure, HA, Lab | A confirmed passive contact, motorized window, or inward-fan provider can satisfy the requirement. Acquisition additionally requires command eligibility. Every predicate for a selected provider must hold. | `core::test_passive_provider_can_satisfy_but_never_acquires`; `core::test_evidence`; `reconcile::test_requirement_fallback_cooldown_and_manual_last_inlet_close`; partial: mixed contact/window/fan path needs Lab. |
| AIRFLOW-INTENT-REJECTED | HA, Lab | A virtual cover target or optimistic fan state cannot stand in for raw physical evidence. | `configuration::test_admission_rejects_optimistic_evidence_and_unsupported_policy`; `reconcile::test_requirement_uses_raw_position_and_attribute_only_updates`; `boundaries::test_requirement_evidence_rejects_declared_optimistic_state` |
| AIRFLOW-MAKE-BEFORE-BREAK | Pure, HA, Lab | Confirm a replacement inlet before releasing the previous provider. A refused opening does not interrupt the existing provider. | `core::test_make_before_break_keeps_old_request_until_replacement_confirmed`; partial: physical effect ordering needs Lab. |
| AIRFLOW-MANUAL-CLOSE | Pure, HA, Lab | Manual closure is honored even if airflow becomes unmet. Another eligible provider can be acquired; extraction is never stopped as fallback. | `reconcile::test_requirement_fallback_cooldown_and_manual_last_inlet_close`; `core::test_manual_takeover_aborts_acquisition_and_tries_alternative` |
| AIRFLOW-TIMEOUT | Pure, HA, Lab | Acquisition timeout yields an explained unmet condition and notification path without false readiness or extraction shutdown. | `reconcile::test_requirement_fallback_cooldown_and_manual_last_inlet_close`; `reconcile::test_airflow_notification_occurs_once_per_unmet_transition` |
| AIRFLOW-EVIDENCE-LOSS | Pure, HA, Lab | Losing required feedback makes confirmation unavailable; a previous successful command cannot keep it confirmed. | `reconcile::test_requirement_uses_raw_position_and_attribute_only_updates`; `reconcile::test_unknown_evidence_never_confirms_and_activation_unknown_is_inert` |
| AIRFLOW-MULTIPLE-OWNERS | Pure, HA, Lab | Ending requirement demand preserves an ordinary policy's valid request on the same resource. | `reconcile::test_conjunctive_airflow_activation_releases_only_its_policy_override`; `core::test_manual_beats_provider_beats_arbitrarily_high_policy` |
| FAN-CAPABILITIES | HA, Lab | Validate speed and direction on all request paths, including turn-on arguments. A percentage attribute does not grant speed support. | `adapters::test_fan_unsupported_speed_direction_unknown_state_and_default`; `entities::test_fan_commands_and_features` |
| FAN-SPEED-STEPS | Pure, HA | Normalize percentage against the bound native fan's supported steps and preserve its on/off semantics. | `adapters::test_native_fan_quantization_uses_supported_speed_bins`; `adapters::test_native_fan_speed_count_and_direction_preserve_running_speed` |
| RELAY-PROFILE-COMPLETE | Pure, HA | Reject incomplete profiles or missing all-off/dead-time configuration before live dispatch. | `configuration::test_invalid_relay_profiles`; `configuration::test_relay_profile_validation_and_output_ownership`; `adapters::test_relay_rejects_unsafe_profile_config` |
| RELAY-REVERSAL | HA, Lab | Turn off, confirm outputs off, wait dead time, then apply the new profile. No opposite outputs overlap. | `adapters::test_relay_confirmed_break_before_make`; `adapters::test_relay_waits_for_delayed_off_reports`; `adapters::test_relay_rechecks_conflicting_feedback_before_each_energize` |
| RELAY-UNCONFIRMED-OFF | HA, Lab | Unknown, unavailable, restored, or assumed output state cannot confirm off or permit the next on effect. | `adapters::test_relay_service_success_without_off_confirmation_never_energizes`; `adapters::test_relay_all_off_unconfirmed_feedback_never_energizes_replacement` |
| RELAY-CANCEL | HA, Lab | Supersession, hands-off, observe mode, unload, or a fault during reversal prevents every stale subsequent effect. | `adapters::test_relay_stale_before_dispatch_and_between_off_calls`; `adapters::test_relay_rechecks_after_dead_time`; `adapters::test_relay_stale_between_energized_outputs_skips_later_output`; `adapters::test_relay_wait_cancellation_unsubscribes` |
| RELAY-OFF-DIRECTION | Pure, HA, Lab | Direction-only control while physically off leaves outputs off. Later bare turn-on uses the configured default profile. | `adapters::test_relay_off_feedback_and_profile_normalization`; `adapters::test_relay_bare_on_uses_configured_profile_after_reverse` |

## Shadow observation and replay

Version 0.1.1 requires the following additional packaged scenarios on both Home
Assistant versions. Their presence in this table does not establish a pass. The
release record must contain a completed run and the supporting trace, replay,
physical journal, and cleanup evidence for the same archive.

| ID | Required layers | Expected outcome | Evidence requirement |
| --- | --- | --- | --- |
| SHADOW-LOCK-ZERO-COMMANDS | HA, Lab | With the integration lock enabled, every resource stays in observe mode. Attempts to select live mode fail, and policy and input changes cause zero HA Operator actuator commands. | Independent simulator command counts for the bounded lock window in `shadow-lock-journal.json`; trace records alone do not prove zero physical effects. |
| SHADOW-LOCK-RELOAD-RESTART | HA, Lab | The lock survives native reload and process restart, including resources with stored live modes and valid intent. | Restart records, exported session transitions, and the independent zero-command journal. |
| SHADOW-TRACE-EXPORT | HA, Lab | An admin retrieves the versioned, paginated trace with sanitized configuration, intent, input, decision, context, and health fields. Disabled tracing yields no records. | `shadow-trace.json`, native action responses, privacy assertions, and source tests for non-admin refusal. |
| SHADOW-TRACE-REPLAY | Pure, Lab | The packaged pure engine evaluates each recorded engine-input snapshot at its recorded time and matches the recorded result. Missing ancestry, gaps, and partial snapshots prevent a complete replay claim. | `shadow-replay.json` binds the exported trace hash to a comparison ledger. Inputs come from the recorder and are not independently reconstructed from raw feedback. Physical consequences remain `not_observed`. |
| CELLAR-CONFIGURATION | HA, Lab | Native resource, policy, and airflow configuration binds the simulated cellar devices to explicit raw feedback and alternative providers. | Successful setup and configured resource identities in `cellar-evidence.json`. |
| CELLAR-REFUSED-POWER-DIRECTION | HA, Lab | Refused power or direction changes remain pending or unconfirmed. A successful service response cannot establish fan movement or airflow. | `cellar-evidence.json` records simulator-generated faults and correlated commands, effects, and feedback. |
| CELLAR-OFF-FEEDBACK-REVERSAL | HA, Lab | A direction reversal waits for confirmed conflicting outputs off and the configured dead time. Unknown or missing off feedback blocks progression. | Independent relay ordering and feedback sequence numbers. |
| CELLAR-STALE-VIRTUAL-AVAILABILITY | HA, Lab | A virtual optimistic target or unavailable raw feedback cannot confirm an airflow provider. | Separate desired, raw, availability, and physical feedback evidence. |
| CELLAR-FALLBACK-KEEP-EXTRACTING | HA, Lab | A failing cellar provider causes alternative selection and an unmet report when appropriate. Extraction continues. | Acquisition and alternative confirmation evidence with extractor state and command journal. |
| CELLAR-MANUAL-SCOPE-EXPIRY | HA, Lab | An explicit hands-off lease blocks automatic commands, then expiry resumes the currently eligible target. An unrelated legacy manual boolean does not create an operator lease. | Durable lease, absolute expiry, current target, and independently observed command ordering. |

Shadow recording is passive. Recorded observations describe the inputs received
while the existing controller owned the actuators. A simulated alternative run
must label its inputs, faults, and physical outcomes as simulator-generated.
Neither replay agreement nor context correlation establishes what the physical
house would have done under different commands.

Every full run must preserve `shadow-trace.json`, `shadow-replay.json`,
`shadow-lock-journal.json`, and `cellar-evidence.json`. All release runs, including
the production-timing soak, must include `cleanup-verification.json` with the run
identity, completion time, and empty scoped Docker container, network, and volume
inventories. The [0.1.1 release notes](releases/0.1.1.md) link to the release
validation evidence.

## Durability, native integration, and release

| ID | Required layers | Expected outcome | Implemented test nodes and limits |
| --- | --- | --- | --- |
| STORE-ACK | HA, Lab | The integration publishes acceptance only after its durable write succeeds. Correlate bridge acknowledgements separately. | `requests::test_request_commits_before_any_actuation`; `services::test_request_receipt_follows_real_atomic_file_and_release` |
| STORE-CONCURRENT | Pure, HA | Concurrent mutations serialize from the latest committed revision without lost updates. | `storage::test_cancellation_serializes_writes_and_publishes_committed_revision` |
| STORE-REJECTED-MUTATION | Pure, HA | A validation exception preserves committed state and does not latch a storage fault. | `storage::test_invalid_mutation_is_not_storage_fault`; `storage::test_mutator_exception_does_not_publish_or_fault` |
| STORE-FAILURE | HA, Lab | Write failure inhibits dispatch and preserves the previous in-process committed intent. A post-rename failure can leave newer complete intent on disk; it cannot produce a success receipt. | `requests::test_failed_request_inhibits_and_creates_repair`; `storage::test_real_atomic_writer_failure_boundaries` |
| STORE-CORRUPT | HA, Lab | Invalid, truncated, or missing expected storage faults instead of silently replacing acknowledged intent with an empty store. | `lifecycle::test_corrupt_storage_loads_visible_fault_without_actuation`; `requests::test_initialized_missing_file_inhibits`; `requests::test_corrupt_nested_manual_inhibits_without_overwriting` |
| STORE-RECOVERY | HA, Lab | Explicit reload after correction recovers without duplicate listeners, lost leases, or replayed consumed occurrences. | `removal::test_restored_valid_snapshot_clears_storage_repair_only_after_good_reload`; `lifecycle::test_real_setup_observe_registry_and_reload`; partial: combine with occurrence/lease restart cases, not full process proof. |
| COVER-RESTART | HA, Lab | A normal restart restores durable intent and remaining lease deadlines, refreshes observation, and resumes only valid work. | `lifecycle::test_live_native_request_roundtrip_survives_reload`; `requests::test_valid_saved_intent_waits_for_non_restored_observation`; Lab required for full process restart. |
| COVER-KILL | Lab | Killing HA around persistence/dispatch boundaries yields either the previous or committed durable intent, with journal evidence for any uncertain physical attempt. | No unit/HA equivalent; actual process-kill Lab evidence required. |
| NATIVE-SUBENTRIES | HA, Lab | Create, reconfigure, and remove resource, policy, and requirement subentries through native flows; references and entities remain consistent. | `flows::test_resource_errors_then_creation_and_reconfigure`; `flows::test_policy_needs_resource_then_creates`; `flows::test_requirement_passive_and_overlap`; see removal scenario. |
| WINDOW-NATIVE-CONFIGURATION | HA, Lab | Disable the synthetic helper writers and reconfigure their policy IDs through native numeric and timer forms. | `WINDOW-NATIVE-CONFIGURATION`; native form driver tests; `window-native-evidence.json` cutover record. |
| WINDOW-NATIVE-NUMERIC | Pure, HA, Lab | Cold reports retain a deadline; a reported warm transition resets it. Unknown retains continuity, and restart requires fresh finite evidence. | `WINDOW-NATIVE-NUMERIC-QUALIFICATION`, `WINDOW-NATIVE-NUMERIC-UNKNOWN-RECOVERY`; exact same-event-loop transitions remain unit and HA integration checks. |
| WINDOW-NATIVE-TIMER | Pure, HA, Lab | Fresh starts replace episodes; pause/cancel withdraw; resume suppresses admission; completion retains accepted expiry. Stronger policies and manual requests preserve that expiry. | `WINDOW-NATIVE-TIMER-LIFECYCLE`, `WINDOW-NATIVE-TIMER-PRECEDENCE`; independent simulator command/effect journal. |
| WINDOW-NATIVE-RECOVERY | HA, Lab | Graceful HA restart and host-controlled SIGKILL preserve accepted expiry. Pending integration reload suppresses admission without catch-up; observe rejects without later replay. | `WINDOW-NATIVE-TIMER-RESTART`, `WINDOW-NATIVE-TIMER-KILL`, `WINDOW-NATIVE-PENDING-RELOAD`, `WINDOW-NATIVE-OBSERVE-ZERO`; crash receipts and typed diagnostic snapshots. |
| WINDOW-NATIVE-RETURN | Pure, HA, Lab | The effective 7% target retains its warning deadline during retries. Refused or unknown raw feedback stays unconfirmed; physical return clears the warning. | `WINDOW-NATIVE-RETURN-DEMO`, `WINDOW-NATIVE-RETURN-UNKNOWN-VIRTUAL`; five native dashboard screenshots and `window-native-evidence.json`. |
| NATIVE-SUBENTRY-REMOVAL | HA, Lab | Delete an unreferenced resource and one referenced by dependents through native WS. Quiesce old workers; preserve remaining config; expose dangling references through a setup error and Repair; recover after correction and Reload. | `removal::test_native_delete_referenced_resource_inhibits_until_repaired_and_reloaded`; `removal::test_unreferenced_resource_delete_reloads_to_empty_configuration` |
| NATIVE-CAPABILITIES | HA, Lab | Managed entity features match the bound adapter, and unsupported operations fail clearly. | `entities::test_cover_commands_do_not_claim_motion`; `entities::test_fan_commands_and_features`; `configuration::test_native_capability_and_managed_source_rejection` |
| NATIVE-LIFECYCLE | HA | Setup, unload, reload, and entity removal leave no duplicate subscriptions or pending resource effects. | `lifecycle::test_real_setup_observe_registry_and_reload`; `lifecycle::test_entry_update_listener_reloads_exactly_once`; `entities::test_native_lifecycle_updates_and_unsubscribes` |
| NATIVE-UNLOAD-BARRIER | HA, Lab | Unload drains in-flight adapter service work, including executor calls, before replacement workers can own the actuator. | `boundaries::test_unload_waits_for_physical_executor_call_to_finish`; service completion is not proof of an empty firmware command queue. |
| NATIVE-REQUEST-ID | HA, Lab | Repeating an accepted request ID is idempotent and returns the same ownership result. | `requests::test_request_retry_is_idempotent_and_conflict_is_validation_error`; `services::test_request_receipt_follows_real_atomic_file_and_release` |
| DIAGNOSTICS-REDACTION | HA, Lab | Diagnostics redact private identifiers and arbitrary stored strings, bound history length, and cause no request or actuation. | `entities::test_diagnostics_allowlist_and_bounded_history`; `lifecycle::test_corrupt_storage_loads_visible_fault_without_actuation`; `LAB-DIAGNOSTICS` exercises the native download endpoint. |
| LAB-ISOLATION | Lab | Inspect routes and container configuration; prove no production credentials, external route, host network, or published ports before control tests. | `isolation::ComposeIsolationTests::test_forbids_network_escapes`; `isolation::InspectIsolationTests::test_detects_extra_network_and_gateway`; `isolation::RouteIsolationTests::test_forbids_gateway_external_route_and_policy`; runtime inspection still required. |
| LAB-ONBOARDING | Lab | A fresh config installs the exact archive, completes HA onboarding, and adds the integration without development-tree mounts. | Lab-only execution; fixture setup does not prove packaged fresh-install onboarding. |
| LAB-HAP-PAIR | Lab | A real HAP client pairs with the isolated HA HomeKit bridge. Preserve the transcript independently of later control assertions. | Lab-only execution; HAP transcript required. |
| LAB-HAP-CONTROL | Lab | HAP control reaches the managed entity and produces correlated durable-intent and simulator effects evidence; pairing alone is insufficient. | Lab-only execution; HAP transcript, durable receipt, and physical journal required. |
| LAB-HAP-RESTART | Lab | The same paired client reconnects after HA restarts; the accepted manual request and observed physical state survive independently. | `LAB-HAP-RESTART-DURABLE` in the packaged runner; process restart and paired-client transcript required. |
| LAB-UI | Lab | Browser-visible managed controls and status reflect observations, decisions, mode, and faults; preserve screenshots and trace. | Lab-only execution; browser trace and screenshots required. |
| LAB-SOAK | Lab | Repeated request changes, feedback delays, and lifecycle events within the declared run duration leave no stale effects, lost durable updates, or duplicate active command writers. Preserve duration and journal size with the outcome. | Lab-only execution; declared duration and physical journal required. |
| RELEASE-ARCHIVE | HA, Lab | The validated archive contains only production integration files. Its hash matches installation, all lab evidence, and publication. | `release::test_reproducible_flat_archive_and_source_verification`; `release::test_corrupted_archive_or_evidence_is_rejected`; `release::test_excludes_caches_and_simulator_code`; final archive/hash evidence still required. |
| RELEASE-COVERAGE | HA | Every production module exceeds 95% line coverage and config flow reaches 100% line and branch coverage; report per-module results. | `coverage::test_requires_strictly_over_95_percent_per_module`; `coverage::test_config_flow_requires_every_line_and_branch`; these test the gate, not current coverage. |
| RELEASE-MUTATION | Pure, HA | Focused mutations of lease expiry, generation, evidence, and persistence guards are detected by meaningful tests. | `scripts/mutation_gate.py` executes four explicit guard mutations; inspect its source-hashed report and JUnit evidence. |
| MIGRATION-OWNERSHIP | Migration | Inventory and explicitly transfer one resource's writers; no legacy automation, bridge route, or scene remains an unaccounted competing owner. | Runbook verification only; no production test execution authorized. |
| MIGRATION-EVIDENCE | Migration | Resolve unread definitions and document evidence strength before live takeover. Do not use a virtual window-open helper as a passive contact. | Read-only inventory plus runbook; unresolved source gaps block a complete inventory claim. |

## Evidence record

### Packaged runner mapping

These are implemented runner IDs, not execution results. Inspect their completed
records in `scenarios.json` and bind them to `summary.json` and the archive hash.
An earlier scenario passing before a later setup failure does not pass the whole
run or prove scenarios that were never reached.

| Catalog scope | Runner IDs and source | Remaining scope distinction |
| --- | --- | --- |
| Fresh installation and native config | `LAB-ONBOARDING`, `LAB-NATIVE-CONFIG-FLOW`, `LAB-RESOURCE-CONFIGURATION` in [runner.py](../tests/lab/runner.py) | Native resource create path does not establish deletion or every reconfigure path. |
| Observe, retry, replacement, STOP, expiry, restart, kill | `OBSERVE-ZERO`, `COVER-RAIN-RETRY`, `COVER-SUPERSESSION`, `COVER-STOP`, `COVER-EXPIRY`, `COVER-RESTART`, `COVER-KILL` in [runner.py](../tests/lab/runner.py) | A kill after accepted intent does not cover every persistence crash boundary. |
| HAP pairing, control, and restart | `LAB-HAP-PAIR`, `LAB-HAP-RESTART-DURABLE` in [runner.py](../tests/lab/runner.py) | Asserts write, physical movement, subscription event, readback, durable-intent correlation, and reconnection with the same pairing after restart. It does not establish Bonjour discovery, iPhone, or home-hub behavior. |
| Native cover UI | `LAB-NATIVE-COVER` in [runner.py](../tests/lab/runner.py) | Cover control does not establish all status, fault, fan, and configuration UI paths. |
| Native diagnostics download | `LAB-DIAGNOSTICS` in [runner.py](../tests/lab/runner.py) | Checks the downloaded allowlist, pseudonymous IDs, bounded history, and absence of known private identifiers. |
| Production timing | `LAB-REALISTIC-SOAK` in [runner.py](../tests/lab/runner.py) | Exercises two five-minute retry periods. It is not a general request-storm or lifecycle soak. |
| Schedule and occurrence policies | `SCHEDULE-SLEEP-IN-DURABLE`, `SCHEDULE-ADJACENT-NATIVE-BLOCKS`, `OCCURRENCE-DST-IDENTITY-EXPIRY` in [scenarios_schedule.py](../tests/lab/scenarios_schedule.py) | Read assertions and event evidence before assigning catalog outcomes. |
| Alternative airflow and relay sequencing | `AIRFLOW-CONFIGURATION`, `FAN-OFF-DIRECTION-NO-AIRFLOW`, `AIRFLOW-UNMET-ALTERNATIVES-KEEP-EXTRACTING`, `AIRFLOW-MANUAL-CLOSE-LAST-INLET`, `AIRFLOW-MAKE-BEFORE-BREAK-PHYSICAL-CONFIRMATION`, `RELAY-REVERSAL-CONFIRMED-DEAD-TIME` in [scenarios_airflow.py](../tests/lab/scenarios_airflow.py) | These paths require independent physical-state and relay-journal assertions, not just managed state. |

### Execution records

For each run, preserve the command, archive SHA-256, source revision, dependency
locks, HA/image versions, start and finish times, and each scenario's explicit
outcome. Distinguish pass, fail, skipped, blocked, and not run. A missing scenario
entry remains unverified.

The lab summary is `artifacts/lab/<run_id>/summary.json`; scenario outcomes are in
`scenarios.json`. Supporting evidence includes route and container inspection,
`simulator-journal.json`, `hap-transcript.jsonl`, screenshots, `trace.zip`, HA logs, and
`crash-events.jsonl`. The simulator journal records physical effects independently
of HA's requested state. Preserve it when an assertion fails.

### Release validation record

Version 0.1.0 completed the local validation matrix with archive SHA-256
`36c568d064b60b2fcb08342ad7e5ba379d520e3bebc77fb150a066f036ccdc7e`.
Download the [release](https://github.com/mikz/ha-operator/releases/tag/v0.1.0) and
[evidence ZIP](https://github.com/mikz/ha-operator/releases/download/v0.1.0/ha_operator-evidence.zip).
The paths below are inside that ZIP, not files served from the Git repository.
Its `evidence.json` index binds the included files to the tested archive.
[GitHub Actions](https://github.com/mikz/ha-operator/actions/workflows/validate.yml)
records the separate automated validation runs.

| Check | Recorded result | Evidence path in the bundle |
| --- | --- | --- |
| HA 2026.9.3 source tests | 570 tests; zero failures, errors, or skips. Release/source identity verified before and after execution. | `evidence/tests/2026.9.3/result.json` and `junit.xml` |
| HA 2026.9.4 source tests | 570 tests; zero failures, errors, or skips. Release/source identity verified before and after execution. | `evidence/tests/2026.9.4/result.json` and `junit.xml` |
| Coverage, both versions | All 19 production Python modules exceed 95% line coverage; the lowest is 98.22%. Config flow has 100% line and branch coverage. | `evidence/tests/<version>/coverage.json` |
| Focused mutations | All four guard mutations killed; the unmutated baseline passed. | `evidence/mutations.json` and `evidence/mutations/` |
| HACS and hassfest | Both official validator images returned success against the recorded production source hashes. | `evidence/validators/hacs.json` and `hassfest.json`, with logs |
| HA 2026.9.3 packaged lab | 23/23 scenarios passed in 143.68 seconds; isolation and cleanup passed. | `evidence/lab/lab-2026-9-3-383ac879/` |
| HA 2026.9.4 packaged lab | 23/23 scenarios passed in 143.67 seconds; isolation and cleanup passed. | `evidence/lab/lab-2026-9-4-05adff92/` |
| HA 2026.9.3 production-timing soak | `LAB-REALISTIC-SOAK` passed in 627.54 seconds; isolation and cleanup passed. | `evidence/lab/lab-2026-9-3-ba03a1c7/` |

Each lab directory includes `summary.json`, `scenarios.json`, and the independent
`simulator-journal.json`. Cleanup results are recorded in each summary; the soak
also includes a separate `cleanup-verification.json`. The full runs also
include HAP transcripts, restart records, browser evidence, and downloaded
diagnostics. All three runs used the archive hash above and left no lab
containers, networks, or volumes after cleanup. Passing the 23 runner scenarios
does not expand their scope beyond the assertions mapped in this catalog.

The soak recorded exactly three inlet position commands, separated by 300.020
and 300.015 seconds. The 70% target reached the configured two-percentage-point
tolerance. The final captured position was 68.683% while still moving; this
result does not assert settled position 70%.

The [migration inventory](migration.md#inventory-the-existing-control-paths)
provides realistic fixture patterns. Recreate those patterns with fictional IDs
and simulator devices. Never import production credentials or use the live
household as an acceptance-test environment.

## Native fan command composition (0.1.2)

`CELLAR-HAP-FAN-COMPOSITION` sends real encrypted HAP writes to native and relay
fans. With independent power refusal, it exercises On→Direction, Direction→On,
combined Active/Direction/Speed, Off→Direction, and restart with pending intent.
It requires durable accepted targets, observed-off state during refusal, the
expected simulator profile after refusal clears, wire events, and no conflicting
relay effects. Fixture tests cover concurrent writes, lease expiry/release,
invalid saved settings, and idempotent command retries.
