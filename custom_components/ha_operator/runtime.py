"""HA event boundary, request handling, and one actuator worker per resource."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any, Literal, TypedDict, TypeGuard, cast
from uuid import uuid4

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED
from homeassistant.core import (
    Context,
    Event,
    EventStateChangedData,
    EventStateReportedData,
    HassJob,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import OperatorConfigEntry
from .adapters import DISPATCH_PARENT, Adapter, create_adapter
from .async_utils import async_settle
from .configuration import validate_configuration
from .const import DOMAIN
from .core import (
    Decision,
    EngineInputs,
    Evidence,
    ManualLease,
    Observation,
    Occurrence,
    Policy,
    Provider,
    Requirement,
    RequirementMemory,
    RequirementResult,
    Resource,
    Target,
    TargetValidationError,
    evaluate,
    evaluate_evidence,
)
from .data import (
    AcceptedRequestReceipt,
    JSONObject,
    NumericInputConfig,
    OccurrenceRecord,
    PolicyConfig,
    PolicyInputRecord,
    RequestReceipt,
    RequirementConfig,
    ResourceConfig,
    RuntimeSnapshot,
    StoredSnapshot,
    TargetData,
    TimerInputConfig,
)
from .intents import IntentValidationError, apply_command
from .policy_inputs import (
    NumericInput,
    NumericReport,
    NumericState,
    TimerEvent,
    TimerInput,
    TimerState,
    numeric_transition,
    recover_numeric,
    recover_timer,
    timer_transition,
)
from .provenance import Selection, selection_for, target_label
from .return_monitor import (
    ReturnMonitorInput,
    ReturnMonitorState,
    ReturnMonitorTransition,
    return_transition,
)
from .storage import InvalidSnapshot, empty_state, validate_state

_LOGGER = logging.getLogger(__name__)
_UNKNOWN = {"unknown", "unavailable"}

type ContextKey = tuple[str, str | None] | tuple[str, str | None, str | None]
type StateEvent = Event[EventStateChangedData] | Event[EventStateReportedData]
type PolicyEvent = NumericReport | TimerEvent | None


class PolicyInputMaps(TypedDict):
    """The existing two-map policy-input preview and mutation surface."""

    policy_inputs: dict[str, PolicyInputRecord]
    occurrences: dict[str, OccurrenceRecord]


class LastCommand(TypedDict):
    target: TargetData
    at: float


def _is_state_event(value: StateEvent | datetime) -> TypeGuard[StateEvent]:
    """Preserve the existing distinction between native state and deadline callbacks."""
    return hasattr(value, "data")


def _now() -> float:
    return dt_util.utcnow().timestamp()


def _number(value: object, label: str) -> float:
    try:
        finite = isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="finite_number",
            translation_placeholders={"field": label},
        )
    return float(value)


def _validate_saved_state(state: StoredSnapshot) -> RuntimeSnapshot:
    """Validate intent records before publishing or pruning a loaded snapshot."""

    def record(item: object) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Saved intent record must be an object")
        return item

    def text(value: object, label: str) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ValueError(f"Saved {label} must contain 1 to 128 characters")
        return value

    for key, value in state["manuals"].items():
        item = record(value)
        for field in ("request_id", "source"):
            metadata = item.get(field)
            if metadata is not None and not isinstance(metadata, str):
                raise ValueError(f"Saved manual {field} must be text or null")
        ManualLease(
            key,
            item["mode"],
            target=Target.from_dict(item["target"]) if item.get("target") else None,
            expires_at=item.get("expires_at"),
        )
        if "fan_settings" in item:
            settings = record(item["fan_settings"])
            if set(settings) - {"percentage", "direction"}:
                raise ValueError("Saved fan settings contain unsupported fields")
            selected = Target.from_dict(settings)
            if selected.direction not in {None, "forward", "reverse"}:
                raise ValueError("Saved fan direction is invalid")
    for key, value in state["occurrences"].items():
        item = record(value)
        policy_id = text(item["policy_id"], "policy_id")
        occurrence_id = text(item["occurrence_id"], "occurrence_id")
        if key != json.dumps([policy_id, occurrence_id]):
            raise ValueError("Saved occurrence key does not match its identity")
        if type(item.get("skipped", False)) is not bool:
            raise ValueError("Saved occurrence suppression must be boolean")
        Occurrence(
            policy_id,
            occurrence_id,
            item["expires_at"],
            item.get("skipped", False),
            target=Target.from_dict(item["target"]) if item.get("target") else None,
        )
        if not item.get("skipped") and not item.get("target"):
            raise ValueError("Admitted occurrence must retain its target")
    for request_id, value in state["requests"].items():
        text(request_id, "request_id")
        item = record(value)
        fingerprint = item["fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("Saved request fingerprint is invalid")
        receipt = record(item["receipt"])
        if receipt["request_id"] != request_id:
            raise ValueError("Saved receipt does not match request identity")
        text(receipt["resource_id"], "resource_id")
        # Reuse the finite-or-explicit-indefinite deadline contract.
        ManualLease(receipt["resource_id"], "hands_off", expires_at=receipt["expires_at"])

    # Runtime semantics have been validated before control admission.
    return cast(RuntimeSnapshot, state)


class OperatorRuntime:
    """Recompute current intent; never enqueue historical target commands."""

    def __init__(self, hass: HomeAssistant, entry: OperatorConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        configured = validate_configuration(entry.subentries)
        self.resources = configured["resources"]
        self.policies = configured["policies"]
        self.requirements = configured["requirements"]
        self.intents = configured["intents"]
        self._shadow_lock_latched = bool(entry.options.get("shadow_lock", False))
        self._store = Store[RuntimeSnapshot](hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True)
        self.observations: dict[str, Observation] = {}
        self._source_availability: dict[str, bool] = {}
        self.decisions: dict[str, Decision] = {}
        self.selections: dict[str, Selection] = {}
        self._contexts: dict[ContextKey, Context] = {}
        self.requirement_results: dict[str, RequirementResult] = {}
        self._requirement_details: dict[str, dict[str, object]] = {}
        self.last_commands: dict[str, LastCommand] = {}
        self.next_attempts: dict[str, float] = {}
        self.attempts: dict[str, int] = {}
        self.fault: str | None = None
        self._adapters = {
            key: create_adapter(hass, key, config) for key, config in self.resources.items()
        }
        self._listeners: set[Callable[[], None]] = set()
        self._unsubscribers: list[Callable[[], None]] = []
        self._timers: dict[str, Callable[[], None]] = {}
        self._deadline_timer: Callable[[], None] | None = None
        self._next_evaluation: float | None = None
        self._wake = {key: asyncio.Event() for key in self.resources}
        self._actuator_locks = {key: asyncio.Lock() for key in self.resources}
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_tasks: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[bool] | None = None
        self._close_interrupts_timer_inputs = False
        self._generation = dict.fromkeys(self.resources, 0)
        self._signatures: dict[str, tuple[Target | None, str | None, str | None, str, bool]] = {}
        self._last_send: dict[str, float] = {}
        self._motion_started: dict[str, float] = {}
        self._last_identity: dict[str, tuple[Target, str | None]] = {}
        self._applying: set[str] = set()
        self._errors: dict[str, str] = {}
        self._emergency_hands_off: dict[str, ManualLease] = {}
        self._memory: dict[str, RequirementMemory] = {}
        self._closed = False
        self._initialized = False
        # Runtime owns the state; Store only schedules and loads persistence.
        self._state = cast(RuntimeSnapshot, empty_state())
        self._input_flush: asyncio.Task[None] | None = None
        self._pending_resources: set[str] = set()
        self._policy_events: deque[tuple[str, PolicyEvent, float, Context | None, bool]] = deque()
        self._input_ingress = dict.fromkeys(self.resources, 0)
        self._input_processed = dict.fromkeys(self.resources, 0)
        self._policy_sources: dict[str, list[str]] = {}
        self._input_fingerprints: dict[str, str] = {}
        self._timer_states: dict[str, tuple[str, str | None, float | None]] = {}
        self._timer_episodes: dict[str, str] = {}
        self._numeric_reports: dict[str, NumericReport] = {}
        self._timer_admissions_open = False
        self._return_dirty: set[str] = set()
        self._return_fingerprints = {
            key: hashlib.sha256(
                json.dumps(
                    {
                        "monitor": config["return_monitor"],
                        "tolerance": config["tolerance"],
                        "entity_id": config["entity_id"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            for key, config in self.resources.items()
            if config.get("return_monitor")
        }
        self._return_notifications: dict[str, tuple[float | None, float | None] | None] = {}
        for policy_id, config in self.policies.items():
            if source := config.get("input"):
                self._policy_sources.setdefault(source["entity_id"], []).append(policy_id)
                self._input_fingerprints[policy_id] = hashlib.sha256(
                    json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
        self._feedback_resources: dict[str, set[str]] = {}
        self._input_resources = self._input_dependencies()
        self._decision_input_entities = set(self._input_resources)
        self._observed_entities = self._decision_input_entities

    def _input_dependencies(self) -> dict[str, set[str]]:
        """Map input changes to owners, including complete provider handovers."""
        dependencies: dict[str, set[str]] = {}
        for identifier, config in self.resources.items():
            feedback = set(config.get("outputs", []))
            if entity_id := config.get("entity_id"):
                feedback.add(entity_id)
            for entity_id in feedback:
                self._feedback_resources.setdefault(entity_id, set()).add(identifier)
            inputs = feedback | {
                source_id
                for source_id in (config.get("restriction_entity"), config.get("fault_entity"))
                if source_id is not None
            }
            for entity_id in inputs:
                dependencies.setdefault(entity_id, set()).add(identifier)
        for policy in self.policies.values():
            if source := policy.get("input"):
                dependencies.setdefault(source["entity_id"], set()).add(policy["resource_id"])
            for entity_id in (policy.get("eligibility_entity"), policy.get("target_entity")):
                if entity_id:
                    dependencies.setdefault(entity_id, set()).add(policy["resource_id"])
        groups = []
        for requirement in self.requirements.values():
            owners = {p["resource_id"] for p in requirement["providers"] if p.get("resource_id")}
            groups.append(owners)
            inputs = set(requirement["activation_entities"]) | {
                evidence["entity_id"]
                for provider in requirement["providers"]
                for evidence in provider["evidence"]
            }
            for entity_id in inputs:
                dependencies.setdefault(entity_id, set()).update(owners)
        # A provider's policy, restriction or feedback can change the handover.
        # Shared inputs can connect groups; close over all affected providers.
        changed = True
        while changed:
            changed = False
            for owners in dependencies.values():
                for group in groups:
                    if owners & group and not group <= owners:
                        owners.update(group)
                        changed = True
        return dependencies

    def _input_record(self, policy_id: str, state: NumericState | TimerState) -> PolicyInputRecord:
        # Each caller pairs this state with this input's own transition or recovery.
        return cast(
            PolicyInputRecord,
            {
                "type": self.policies[policy_id]["input"]["type"],
                "fingerprint": self._input_fingerprints[policy_id],
                "state": state.to_record(),
            },
        )

    @staticmethod
    def _numeric_report(source: NumericInputConfig, native: State | None) -> NumericReport:
        value = None
        if (
            native is not None
            and native.state not in _UNKNOWN
            and not any(
                native.attributes.get(key) for key in ("restored", "assumed_state", "optimistic")
            )
            and native.attributes.get("unit_of_measurement") == source["unit"]
        ):
            try:
                parsed = float(native.state)
                if math.isfinite(parsed):
                    value = parsed
            except ValueError, TypeError:
                pass
        return NumericReport(value)

    @callback
    def _capture_timer_state(
        self, entity_id: str, native: State | None, previous: State | None
    ) -> None:
        if native is None:
            self._timer_states[entity_id] = ("unknown", None, None)
            return
        finish = native.attributes.get("finishes_at")
        parsed = dt_util.parse_datetime(finish) if isinstance(finish, str) else None
        self._timer_states[entity_id] = (
            native.state,
            previous.state if previous is not None else None,
            parsed.timestamp() if parsed is not None else None,
        )

    @callback
    def _timer_event(self, event: Event[Mapping[str, Any]]) -> None:
        if self._closed:
            return
        entity_id = event.data["entity_id"]
        phase, previous, finish = self._timer_states.get(entity_id, ("unknown", None, None))
        kind = str(event.event_type).partition(".")[2]
        for policy_id in self._policy_sources[entity_id]:
            source = self.policies[policy_id]["input"]
            if source["type"] != "timer_episode":
                continue
            episode = self._timer_episodes.get(policy_id)
            action: Literal["start", "pause", "resume", "cancel", "finish", "change"]
            if kind in {"started", "restarted"}:
                if not self._timer_admissions_open:
                    continue
                if phase != "active":
                    continue  # Lifecycle event must agree with its preceding native state.
                if previous not in {"idle", "active", "paused"}:
                    continue
                if previous == "paused":
                    if episode is None:
                        continue
                    action = "resume"
                else:
                    episode = uuid4().hex
                    self._timer_episodes[policy_id] = episode
                    action = "start"
            else:
                if episode is None:
                    continue
                if kind == "paused":
                    action = "pause"
                elif kind == "cancelled":
                    action = "cancel"
                elif kind == "finished":
                    action = "finish"
                else:
                    action = "change"
                expected = {
                    "pause": "paused",
                    "cancel": "idle",
                    "finish": "idle",
                    "change": "active",
                }[action]
                if phase != expected:
                    continue
            record = self._state["policy_inputs"].get(policy_id)
            accepted = (
                record is not None
                and record["state"].get("episode_id") == episode
                and record["state"].get("phase") == "accepted"
            )
            admissible = self.mode(
                self.policies[policy_id]["resource_id"]
            ) == "live" and self.policy_enabled(policy_id)
            self._policy_events.append(
                (
                    policy_id,
                    TimerEvent(action, episode, finish, accepted),
                    event.time_fired.timestamp(),
                    event.context,
                    admissible,
                )
            )
            self._queue_reconciliation(self._input_resources[entity_id], policy_input=True)
        self._timer_states[entity_id] = (phase, phase, finish)

    async def _recover_policy_inputs(self) -> None:
        now = _now()

        def recover(raw_state: RuntimeSnapshot) -> None:
            # async_start validated all saved semantics before recovery changes runtime state.
            state = raw_state
            for policy_id in set(state["policy_inputs"]) & (
                set(self.policies) - set(self._input_fingerprints)
            ):
                for item in state["occurrences"].values():
                    if item["policy_id"] == policy_id:
                        item["skipped"] = True
                del state["policy_inputs"][policy_id]
            for policy_id, fingerprint in self._input_fingerprints.items():
                source = self.policies[policy_id]["input"]
                record = state["policy_inputs"].get(policy_id)
                matching = record is not None and record["fingerprint"] == fingerprint
                if record is not None and record["type"] == "timer_episode":
                    old = TimerState.from_record(record["state"])
                    if old.episode_id is not None and (
                        not matching
                        or old.phase == "qualifying"
                        or (old.expires_at is not None and old.expires_at <= now)
                    ):
                        key = json.dumps([policy_id, old.episode_id])
                        item = state["occurrences"].setdefault(
                            key,
                            {
                                "policy_id": policy_id,
                                "occurrence_id": old.episode_id,
                                # Every saved non-idle episode validates a finite expiry.
                                "expires_at": cast(float, old.expires_at),
                            },
                        )
                        item["skipped"] = True
                recovered: NumericState | TimerState
                if source["type"] == "qualified_numeric":
                    recovered = (
                        recover_numeric(NumericState.from_record(record["state"]))
                        if matching and record is not None
                        else NumericState()
                    )
                else:
                    recovered = (
                        recover_timer(TimerState.from_record(record["state"]), now=now).state
                        if matching and record is not None
                        else TimerState()
                    )
                    if recovered.episode_id is not None:
                        self._timer_episodes[policy_id] = recovered.episode_id
                state["policy_inputs"][policy_id] = self._input_record(policy_id, recovered)

        self._mutate_state(recover, skip_unchanged=True)

    async def _apply_policy_input(
        self,
        policy_id: str,
        event: PolicyEvent,
        captured_at: float,
        context: Context | None,
        admissible: bool,
    ) -> None:
        if self._closed or self.fault:
            return
        config = self.policies[policy_id]
        source = config["input"]

        def mutate(state: PolicyInputMaps) -> None:
            record = state["policy_inputs"][policy_id]
            final: NumericState | TimerState
            if source["type"] == "qualified_numeric":
                # Only numeric-source callbacks enqueue NumericReport; ticks enqueue None.
                numeric_event = cast(NumericReport | None, event)
                numeric = numeric_transition(
                    NumericInput(source["threshold"], source["qualification_seconds"]),
                    NumericState.from_record(record["state"]),
                    numeric_event,
                    now=captured_at,
                )
                # Process a report at its captured time, then catch up without inventing evidence.
                final = numeric_transition(
                    NumericInput(source["threshold"], source["qualification_seconds"]),
                    numeric.state,
                    None,
                    now=_now(),
                ).state
            else:
                timer_config = TimerInput(
                    source["qualification_seconds"], source["request_seconds"]
                )
                previous = TimerState.from_record(record["state"])
                # Only timer lifecycle callbacks enqueue TimerEvent; ticks enqueue None.
                timer_event = cast(TimerEvent | None, event)
                if timer_event is not None and timer_event.kind == "start":
                    if json.dumps([policy_id, timer_event.episode_id]) in state["occurrences"]:
                        return
                transition = timer_transition(timer_config, previous, timer_event, now=captured_at)
                tick = timer_transition(timer_config, transition.state, None, now=_now())
                changes = (*transition.occurrence_changes, *tick.occurrence_changes)
                final = tick.state
                if final.phase in {"qualifying", "accepted"} and (
                    not admissible
                    or self.mode(config["resource_id"]) != "live"
                    or not self.policy_enabled(policy_id)
                ):
                    assert final.episode_id is not None
                    rejected = timer_transition(
                        timer_config, final, TimerEvent("suppress", final.episode_id), now=_now()
                    )
                    final = rejected.state
                    changes = (*changes, *rejected.occurrence_changes)
                for change in changes:
                    key = json.dumps([policy_id, change.episode_id])
                    item = state["occurrences"].get(key)
                    if change.action == "suppress":
                        if item is None:
                            item = state["occurrences"][key] = {
                                "policy_id": policy_id,
                                "occurrence_id": change.episode_id,
                                "expires_at": change.expires_at,
                            }
                        item["skipped"] = True
                    elif item is None:
                        # Reuse admission validation only for the surviving, unexpired request.
                        if final.phase != "accepted" or final.episode_id != change.episode_id:
                            continue
                        self._occurrence(policy_id, change.episode_id, change.expires_at)
                        self._admit(config["resource_id"])
                        target = self._policy_target(config)
                        if target is None:
                            assert final.episode_id is not None
                            rejected = timer_transition(
                                timer_config,
                                final,
                                TimerEvent("suppress", final.episode_id),
                                now=_now(),
                            )
                            final = rejected.state
                        occurrence: OccurrenceRecord = {
                            "policy_id": policy_id,
                            "occurrence_id": change.episode_id,
                            "expires_at": change.expires_at,
                            "skipped": target is None,
                        }
                        if target is not None:
                            occurrence["target"] = target.to_dict()
                        state["occurrences"][key] = occurrence
            state["policy_inputs"][policy_id] = self._input_record(policy_id, final)

        original_occurrences = {
            key: item
            for key, item in self._state["occurrences"].items()
            if item["policy_id"] == policy_id
        }
        original: PolicyInputMaps = {
            "policy_inputs": {policy_id: self._state["policy_inputs"][policy_id]},
            "occurrences": original_occurrences,
        }
        candidate = deepcopy(original)
        mutate(candidate)
        if candidate == original:
            return
        await self._change(
            mutate,
            skip_unchanged=True,
            context=context,
            context_key=("occurrence", policy_id, event.episode_id)
            if isinstance(event, TimerEvent)
            else None,
        )

    async def async_start(self) -> None:
        try:
            try:
                loaded = await self._store.async_load()
                self._state = (
                    _validate_saved_state(validate_state(loaded))
                    if loaded is not None
                    else cast(RuntimeSnapshot, empty_state())
                )
                for policy_id, fingerprint in self._input_fingerprints.items():
                    record = self._state["policy_inputs"].get(policy_id)
                    if (
                        record is not None
                        and record["fingerprint"] == fingerprint
                        and record["type"] != self.policies[policy_id]["input"]["type"]
                    ):
                        raise ValueError("Saved policy input type disagrees with its fingerprint")
            except (ValueError, TypeError, KeyError, OverflowError) as err:
                raise InvalidSnapshot("Saved runtime records are invalid") from err
            if any(key not in self._state["intents"] for key in self.intents):

                def initialize(state: RuntimeSnapshot) -> None:
                    for key, config in self.intents.items():
                        state["intents"].setdefault(key, config["initial_value"])

                self._mutate_state(initialize)
            if self.shadow_locked and any(
                self._state["modes"].get(key) == "live" for key in self.resources
            ):
                self._mutate_state(
                    lambda state: state["modes"].update(dict.fromkeys(self.resources, "observe"))
                )
            await self._recover_policy_inputs()
            removed_monitors = (
                set(self._state["return_monitors"]) - self._return_fingerprints.keys()
            )

            # Preserve deadlines only when the monitor settings and raw source agree.
            def initialize_monitors(state: RuntimeSnapshot) -> None:
                state["return_monitors"] = {
                    key: state["return_monitors"][key]
                    if key in state["return_monitors"]
                    and state["return_monitors"][key]["fingerprint"] == fingerprint
                    else {"fingerprint": fingerprint, "state": ReturnMonitorState().to_record()}
                    for key, fingerprint in self._return_fingerprints.items()
                }

            self._mutate_state(initialize_monitors, skip_unchanged=True)
            for key in removed_monitors:
                persistent_notification.async_dismiss(
                    self.hass, f"ha_operator_{self.entry.entry_id}_{key}_return"
                )
            now = _now()

            def prune(raw_state: RuntimeSnapshot) -> None:
                # Only startup-owned mutations have followed the saved-intent validation.
                state = raw_state
                state["manuals"] = {
                    key: item
                    for key, item in state["manuals"].items()
                    if ((expiry := item.get("expires_at")) is None or expiry > now)
                    and self.manual_control(key)
                }

            if any(
                ((expiry := item.get("expires_at")) is not None and expiry <= now)
                or not self.manual_control(key)
                # Earlier startup-owned writes affect other maps, never manuals.
                for key, item in self._state["manuals"].items()
            ):
                self._mutate_state(prune)
        except (InvalidSnapshot, OSError, HomeAssistantError) as err:
            self._invalid_state(err)
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="storage_error",
            ) from err
        else:
            # Startup semantic validation and owned mutations preserve these records.
            self._initialized = True
        self._unsubscribers.append(self.hass.async_add_shutdown_job(HassJob(self.async_close)))
        if self.requirements:
            self._unsubscribers.append(
                self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._activation_input_changed)
            )
        if self._observed_entities:
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass, self._observed_entities, self._input_changed
                )
            )
            self._unsubscribers.append(
                async_track_state_report_event(
                    self.hass, self._observed_entities, self._input_changed
                )
            )
        if self._policy_sources:

            @callback
            def open_timer_admissions(_: HomeAssistant) -> None:
                for entity_id in self._policy_sources:
                    if entity_id.startswith("timer."):
                        native = self.hass.states.get(entity_id)
                        self._capture_timer_state(entity_id, native, native)
                self._timer_admissions_open = True

            self._unsubscribers.append(async_at_started(self.hass, open_timer_admissions))

            @callback
            def timer_filter(data: Mapping[str, Any]) -> bool:
                return data.get("entity_id") in self._policy_sources

            for event_type in (
                "started",
                "restarted",
                "paused",
                "cancelled",
                "finished",
                "changed",
            ):
                self._unsubscribers.append(
                    self.hass.bus.async_listen(
                        f"timer.{event_type}", self._timer_event, event_filter=timer_filter
                    )
                )
            for entity_id in self._policy_sources:
                native = self.hass.states.get(entity_id)
                if entity_id.startswith("timer."):
                    self._capture_timer_state(entity_id, native, native)
                else:
                    for policy_id in self._policy_sources[entity_id]:
                        # Configuration validates sensor sources as qualified-numeric inputs.
                        self._numeric_reports[policy_id] = self._numeric_report(
                            cast(NumericInputConfig, self.policies[policy_id]["input"]), native
                        )
        self._recompute()
        self._tasks = [
            self.entry.async_create_background_task(
                self.hass, self._worker(key), f"ha_operator:{key}"
            )
            for key in self.resources
        ]
        self._wake_all()

    async def async_close(self, *, interrupt_timer_inputs: bool = False) -> bool:
        """Fence synchronously, then finish the first boundary's owned cleanup."""
        if self._close_task is None:
            self._closed = True
            self._timer_admissions_open = False
            self._close_interrupts_timer_inputs = interrupt_timer_inputs
            for key in self._generation:
                self._generation[key] += 1
            self._close_task = self.hass.async_create_task(
                self._async_close(), "ha_operator:quiesce"
            )
        success, cancelled = await async_settle(self._close_task)
        if cancelled:
            raise asyncio.CancelledError
        return success

    async def _async_close(self) -> bool:
        flush, self._input_flush = self._input_flush, None
        if flush is not None:
            flush.cancel()
        self._pending_resources.clear()
        self._policy_events.clear()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        if self._deadline_timer:
            self._deadline_timer()
            self._deadline_timer = None
        for task in self._tasks:
            task.cancel()
        if flush is not None:
            await asyncio.gather(flush, return_exceptions=True)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*self._stop_tasks, return_exceptions=True)
        interrupted = []
        demote = self.shadow_locked and self._initialized
        interrupt = self._close_interrupts_timer_inputs and any(
            (source := policy.get("input")) is not None and source["type"] == "timer_episode"
            for policy in self.policies.values()
        )

        def fence(raw_state: RuntimeSnapshot) -> None:
            # Unload fencing is entered only for initialized intent; abort cleanup skips it.
            state = raw_state
            if interrupt:
                for policy_id in self._input_fingerprints:
                    record = state["policy_inputs"].get(policy_id)
                    if (
                        record is not None
                        and record["type"] == "timer_episode"
                        and record["state"]["phase"] in {"qualifying", "accepted"}
                    ):
                        self._suppress_timer_input(state, policy_id)
                        interrupted.append(policy_id)
            if demote:
                state["modes"].update(dict.fromkeys(self.resources, "observe"))

        if self._initialized:
            if interrupt or demote:
                self._mutate_state(fence, skip_unchanged=True)
                for policy_id in interrupted:
                    policy = self.policies[policy_id]
                    entity_id = er.async_get(self.hass).async_get_entity_id(
                        "sensor", DOMAIN, f"{policy['resource_id']}_reason"
                    )
                    self.hass.bus.async_fire(
                        "logbook_entry",
                        {
                            "name": policy["name"],
                            "message": "Timer episode interrupted by HA Operator reload or unload",
                            "domain": DOMAIN,
                            "entity_id": entity_id,
                        },
                    )
            await self._store.async_save(self._state)
        self._listeners.clear()
        return True

    @callback
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def adapter(self, resource_id: str) -> Adapter:
        return self._adapters[resource_id]

    def mode(self, resource_id: str) -> str:
        if self.shadow_locked:
            return "observe"
        return self._state["modes"].get(resource_id, "observe")

    @property
    def shadow_locked(self) -> bool:
        self._shadow_lock_latched |= bool(self.entry.options.get("shadow_lock", False))
        return self._shadow_lock_latched

    async def async_prepare_unlock(self) -> None:
        """Demote all resources before the options flow removes its lock."""
        if self._closed:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unloaded")
        if self.shadow_locked:
            await self._change(
                lambda state: state["modes"].update(dict.fromkeys(self.resources, "observe"))
            )

    def policy_enabled(self, policy_id: str) -> bool:
        return self._state["policy_enabled"].get(policy_id, True)

    def manual(self, resource_id: str) -> ManualLease | None:
        if not self.manual_control(resource_id):
            return None
        if resource_id in self._emergency_hands_off:
            return self._emergency_hands_off[resource_id]
        data = self._state["manuals"].get(resource_id)
        if data is None:
            return None
        lease = ManualLease(
            resource_id=resource_id,
            mode=data["mode"],
            target=Target.from_dict(data["target"]) if data.get("target") else None,
            expires_at=data.get("expires_at"),
            request_id=data.get("request_id"),
            source=data.get("source", "service"),
        )
        return lease if lease.active(_now()) else None

    def _resource(self, resource_id: str) -> ResourceConfig:
        if resource_id not in self.resources:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_resource"
            )
        if self._closed:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unloaded")
        return self.resources[resource_id]

    def _admit(self, resource_id: str) -> ResourceConfig:
        config = self._resource(resource_id)
        if self.fault:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="storage_inhibited")
        if self.mode(resource_id) != "live":
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="observe_request"
            )
        return config

    def manual_control(self, resource_id: str) -> bool:
        resource = self.resources.get(resource_id)
        return resource.get("manual_control", True) if resource is not None else True

    def source_available(self, resource_id: str) -> bool:
        """Return published source presence, without treating it as physical evidence."""
        return not self._closed and self._source_availability.get(resource_id, False)

    def _manual_resource(self, resource_id: str) -> ResourceConfig:
        config = self._resource(resource_id)
        if not self.manual_control(resource_id):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="manual_disabled"
            )
        return config

    def desired_value(self, intent_id: str) -> bool | None:
        return self._state["intents"].get(intent_id) if not self.fault else None

    def intent_context(self, intent_id: str) -> Context | None:
        return self._contexts.get(("intent", intent_id))

    async def async_set_desired(
        self, intent_id: str, on: bool, *, context: Context | None = None
    ) -> None:
        if self._closed:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unloaded")
        changed: list[str] = []

        def mutate(state: RuntimeSnapshot) -> None:
            previous = dict(state["intents"])
            try:
                apply_command(state["intents"], self.intents, intent_id, on)
            except IntentValidationError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key=err.translation_key,
                    translation_placeholders=err.translation_placeholders,
                ) from err
            changed[:] = [
                key for key, value in state["intents"].items() if previous.get(key) != value
            ]

        await self._change(
            mutate,
            context_key=lambda: [("intent", key) for key in changed],
            context=context,
            skip_unchanged=True,
        )

    def _invalid_state(self, error: Exception) -> None:
        self.fault = "storage_error"
        _LOGGER.error("HA Operator state cannot be trusted: %s", error)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"storage_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="storage_error",
        )

    @callback
    def _mutate_state(
        self,
        mutator: Callable[[RuntimeSnapshot], object],
        *,
        skip_unchanged: bool = False,
    ) -> bool:
        """Validate a candidate mutation before replacing runtime-owned state."""
        candidate = deepcopy(self._state)
        mutator(candidate)
        if skip_unchanged and candidate == self._state:
            return False
        self._state = candidate
        self._store.async_delay_save(lambda: self._state, 1)
        return True

    async def _change(
        self,
        mutator: Callable[[RuntimeSnapshot], object],
        *,
        context_key: ContextKey | Callable[[], Iterable[ContextKey]] | None = None,
        context: Context | None = None,
        skip_unchanged: bool = False,
    ) -> RuntimeSnapshot:
        """Apply a synchronous state change, then publish and wake current work."""
        if self._closed:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="unloaded")
        if self.fault:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="storage_inhibited")
        changed = self._mutate_state(mutator, skip_unchanged=skip_unchanged)
        if changed and context_key is not None and context is not None:
            for key in context_key() if callable(context_key) else [context_key]:
                self._contexts[key] = context
        self._recompute()
        self._wake_all()
        self._notify()
        return self._state

    async def async_request(
        self,
        resource_id: str,
        *,
        mode: str = "target",
        target: object = None,
        duration: float | None = None,
        expires_at: float | None = None,
        indefinite: bool = False,
        request_id: str | None = None,
        source: str = "service",
        context: Context | None = None,
    ) -> AcceptedRequestReceipt:
        self._manual_resource(resource_id)
        config = self._admit(resource_id)
        if mode not in {"target", "hands_off"}:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_request_mode"
            )
        if sum((duration is not None, expires_at is not None, indefinite)) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="ambiguous_expiry"
            )
        if mode == "hands_off" and target is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="hands_off_target"
            )
        try:
            requested = (
                None
                if mode == "hands_off"
                else target
                if isinstance(target, Target)
                else Target.from_dict({} if target is None else target)
            )
            fan_command = requested is not None and config["kind"] in {"fan", "relay_fan"}
            normalized = (
                requested
                if requested is None or fan_command
                else self.adapter(resource_id).normalize(requested)
            )
        except TargetValidationError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=err.translation_key,
                translation_placeholders=err.translation_placeholders,
            ) from err
        now = _now()
        if duration is not None:
            duration = _number(duration, "duration")
            if duration <= 0:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="positive_duration"
                )
        if expires_at is not None:
            expires_at = _number(expires_at, "expires_at")
        actual_duration = duration if duration is not None else config["manual_duration"]
        expiry = (
            None
            if indefinite
            else (expires_at if expires_at is not None else now + actual_duration)
        )
        request_id = str(uuid4()) if request_id is None else request_id
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_request_id"
            )

        def fingerprint_for(value: Target | None) -> str:
            return hashlib.sha256(
                json.dumps(
                    {
                        "resource_id": resource_id,
                        "mode": mode,
                        "target": value.to_dict() if value else None,
                        "duration": actual_duration,
                        "expires_at": expires_at,
                        "indefinite": indefinite,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()

        fingerprint = fingerprint_for(normalized)
        receipt: RequestReceipt = {
            "request_id": request_id,
            "resource_id": resource_id,
            "expires_at": expiry,
        }
        replayed = False

        def mutate(state: RuntimeSnapshot) -> None:
            nonlocal replayed
            # Admission can wait behind another write; observe mode or unload
            # may have changed while the request was queued.
            self._admit(resource_id)
            existing = state["requests"].get(request_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="request_id_conflict"
                    )
                receipt.update(existing["receipt"])
                replayed = True
                return
            if expiry is not None and expiry <= _now():
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="future_expiry"
                )
            accepted = normalized
            settings = None
            if fan_command and requested is not None:
                try:
                    accepted, settings = self._fan_command(resource_id, requested, state)
                except TargetValidationError as err:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key=err.translation_key,
                        translation_placeholders=err.translation_placeholders,
                    ) from err
            state["manuals"][resource_id] = {
                # The public mode check above admits exactly these two values.
                "mode": cast(Literal["target", "hands_off"], mode),
                "target": accepted.to_dict() if accepted else None,
                "expires_at": expiry,
                "request_id": request_id,
                "source": source,
            }
            if settings is not None:
                state["manuals"][resource_id]["fan_settings"] = settings
            state["requests"][request_id] = {"fingerprint": fingerprint, "receipt": receipt}

        await self._change(
            mutate,
            context_key=lambda: [] if replayed else [("manual", request_id)],
            context=context,
        )
        return {**receipt, "accepted": True}

    def _fan_command(
        self, resource_id: str, requested: Target, state: RuntimeSnapshot
    ) -> tuple[Target, JSONObject]:
        """Compose from current runtime intent in the same HA event-loop turn.

        Fan protocols send power, speed and direction separately. An off relay
        profile has no direction, so retain selected settings with its lease;
        those settings are intent only and never become observed feedback.
        """
        adapter = self.adapter(resource_id)
        if not requested.to_dict():
            raise TargetValidationError("empty_fan_target", "Fan target must not be empty")
        default = adapter.normalize(Target.from_dict(self.resources[resource_id]["default_target"]))
        prior = state["manuals"].get(resource_id)
        base = None
        if (
            prior
            and prior["mode"] == "target"
            and ((expiry := prior.get("expires_at")) is None or expiry > _now())
        ):
            # A validated target-mode lease has a nonempty target object; falsey legacy
            # targets are admitted only for hands-off and cannot enter this branch.
            prior_target = cast(Mapping[str, object], prior["target"])
            base = Target.from_dict({**prior_target, **prior.get("fan_settings", {})})
        if base is None:
            # Bare on uses the configured profile until the user selects one.
            base = (
                default
                if requested.to_dict() == {"on": True}
                else adapter.read_observation(_now()).target
            )
        direction = requested.direction or (base.direction if base else None) or default.direction
        percentage = (
            requested.percentage or (base.percentage if base else None) or default.percentage
        )
        command: dict[str, object] = dict(requested.to_dict())
        if requested.profile is None:
            if requested.on is None and requested.percentage is None and base is not None:
                command["on"] = base.on
            on = command.get("on")
            if requested.percentage is not None:
                on = requested.percentage > 0
            if on:
                if requested.percentage is None and percentage is not None:
                    command["percentage"] = percentage
                if requested.direction is None and direction is not None:
                    command["direction"] = direction
        normalized = adapter.normalize(Target.from_dict(command))
        settings: JSONObject = {
            key: value
            for key, value in {
                "direction": normalized.direction if normalized.on else direction,
                "percentage": normalized.percentage if normalized.on else percentage,
            }.items()
            if value is not None
        }
        return normalized, settings

    async def async_release(self, resource_id: str) -> None:
        self._manual_resource(resource_id)
        await self._change(
            lambda state: state["manuals"].pop(resource_id, None),
        )
        self._emergency_hands_off.pop(resource_id, None)
        self._recompute()
        self._wake_all()

    async def async_stop(self, resource_id: str, *, context: Context | None = None) -> None:
        task = self.hass.async_create_task(
            self._async_stop(resource_id, context=context),
            f"ha_operator:stop_request:{resource_id}",
        )
        self._stop_tasks.add(task)
        try:
            _, cancelled = await async_settle(task)
            if cancelled:
                raise asyncio.CancelledError
        finally:
            self._stop_tasks.discard(task)

    async def _async_stop(self, resource_id: str, *, context: Context | None = None) -> None:
        config = self._manual_resource(resource_id)
        if self.mode(resource_id) != "live":
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="observe_stop")
        self._generation[resource_id] += 1
        stop_id = str(uuid4())
        expiry = _now() + config["manual_duration"]
        self._emergency_hands_off[resource_id] = ManualLease(
            resource_id, "hands_off", expires_at=expiry, request_id=stop_id, source="stop"
        )
        if context is not None:
            self._contexts[("manual", stop_id)] = context
        self._recompute()
        self._notify()
        actuator_lock = self._actuator_locks[resource_id]
        had_inflight_command = actuator_lock.locked()
        stop_error = None
        context_token = DISPATCH_PARENT.set(context)
        try:
            await self.adapter(resource_id).async_stop()
        except HomeAssistantError as err:
            stop_error = err
        finally:
            DISPATCH_PARENT.reset(context_token)

        def mutate(state: RuntimeSnapshot) -> None:
            state["manuals"][resource_id] = {
                "mode": "hands_off",
                "expires_at": expiry,
                "request_id": stop_id,
                "source": "stop",
            }

        try:
            await self._change(
                mutate,
            )
            emergency = self._emergency_hands_off.get(resource_id)
            if emergency is not None and emergency.request_id == stop_id:
                self._emergency_hands_off.pop(resource_id)
        finally:
            # A transport already in progress can complete after the immediate
            # STOP. Drain it, then stop again before acknowledging this request.
            # A newer explicit lease supersedes this barrier as well as intent.
            if had_inflight_command and self.adapter(resource_id).supports_stop:

                async def finish_stop() -> bool:
                    async with actuator_lock:
                        lease = self.manual(resource_id)
                        if (
                            lease is not None
                            and lease.request_id == stop_id
                            and self.mode(resource_id) == "live"
                        ):
                            context_token = DISPATCH_PARENT.set(context)
                            try:
                                await self.adapter(resource_id).async_stop()
                            finally:
                                DISPATCH_PARENT.reset(context_token)
                            return True
                    return False

                stopped, cancelled = await async_settle(
                    self.hass.async_create_task(finish_stop(), f"ha_operator:stop:{resource_id}")
                )
                if stopped:
                    stop_error = None
                if cancelled:
                    raise asyncio.CancelledError
        if stop_error:
            raise stop_error

    async def async_set_mode(self, resource_id: str, mode: str) -> None:
        self._resource(resource_id)
        if mode not in {"observe", "live"}:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_control_mode"
            )

        def mutate(state: RuntimeSnapshot) -> None:
            if mode == "live" and self.shadow_locked:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="shadow_locked"
                )
            # The public mode guard admits only these two persisted values.
            state["modes"][resource_id] = cast(Literal["observe", "live"], mode)
            if mode == "observe":
                for policy_id, policy in self.policies.items():
                    if policy["resource_id"] == resource_id:
                        self._suppress_timer_input(state, policy_id)

        await self._change(
            mutate,
        )

    def _suppress_timer_input(self, state: RuntimeSnapshot, policy_id: str) -> None:
        record = state["policy_inputs"].get(policy_id)
        if record is None or record["type"] != "timer_episode":
            return
        timer = TimerState.from_record(record["state"])
        if timer.phase not in {"qualifying", "accepted"}:
            return
        # Only this timer input's own record can reach timer suppression.
        source = cast(TimerInputConfig, self.policies[policy_id]["input"])
        assert timer.episode_id is not None
        transition = timer_transition(
            TimerInput(source["qualification_seconds"], source["request_seconds"]),
            timer,
            TimerEvent("suppress", timer.episode_id),
            now=_now(),
        )
        state["policy_inputs"][policy_id] = self._input_record(policy_id, transition.state)
        for change in transition.occurrence_changes:
            key = json.dumps([policy_id, change.episode_id])
            item = state["occurrences"].setdefault(
                key,
                {
                    "policy_id": policy_id,
                    "occurrence_id": change.episode_id,
                    "expires_at": change.expires_at,
                },
            )
            item["skipped"] = True

    async def async_set_policy_enabled(self, policy_id: str, enabled: bool) -> None:
        if policy_id not in self.policies or not isinstance(enabled, bool):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_policy_enabled"
            )

        def mutate(state: RuntimeSnapshot) -> None:
            state["policy_enabled"][policy_id] = enabled
            if not enabled:
                self._suppress_timer_input(state, policy_id)
                for occurrence in state["occurrences"].values():
                    if occurrence["policy_id"] == policy_id:
                        occurrence["skipped"] = True

        await self._change(
            mutate,
        )

    def _occurrence(
        self, policy_id: str, occurrence_id: str, expires_at: object
    ) -> tuple[PolicyConfig, str, float]:
        config = self.policies.get(policy_id)
        if not config or config["kind"] != "occurrence":
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_occurrence_policy"
            )
        self._resource(config["resource_id"])
        if not isinstance(occurrence_id, str) or not 1 <= len(occurrence_id) <= 128:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_occurrence_id"
            )
        expiry = _number(expires_at, "expires_at")
        if expiry <= _now():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="future_expiry")
        return config, json.dumps([policy_id, occurrence_id]), expiry

    async def async_submit_occurrence(
        self,
        policy_id: str,
        occurrence_id: str,
        expires_at: object,
        *,
        context: Context | None = None,
    ) -> OccurrenceRecord:
        config, key, expiry = self._occurrence(policy_id, occurrence_id, expires_at)
        self._admit(config["resource_id"])
        target = self._policy_target(config)
        if target is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="occurrence_target_unavailable"
            )

        replayed = False

        def mutate(state: RuntimeSnapshot) -> None:
            nonlocal replayed
            self._admit(config["resource_id"])
            replayed = key in state["occurrences"]
            if key not in state["occurrences"]:
                state["occurrences"][key] = {
                    "policy_id": policy_id,
                    "occurrence_id": occurrence_id,
                    "expires_at": expiry,
                    "target": target.to_dict(),
                    "skipped": not self.policy_enabled(policy_id)
                    or not self._eligible(config, policy_id),
                }

        await self._change(
            mutate,
            context_key=lambda: [] if replayed else [("occurrence", policy_id, occurrence_id)],
            context=context,
        )
        return self._state["occurrences"][key].copy()

    async def async_skip_occurrence(
        self, policy_id: str, occurrence_id: str, expires_at: object
    ) -> None:
        _, key, expiry = self._occurrence(policy_id, occurrence_id, expires_at)

        def mutate(state: RuntimeSnapshot) -> None:
            item = state["occurrences"].setdefault(
                key,
                {
                    "policy_id": policy_id,
                    "occurrence_id": occurrence_id,
                    "expires_at": expiry,
                },
            )
            item["skipped"] = True

        await self._change(
            mutate,
        )

    async def async_reconcile(self, resource_id: str | None = None) -> None:
        if resource_id is not None:
            self._resource(resource_id)
        if self._input_fingerprints or self._return_fingerprints:
            self._queue_reconciliation(
                {resource_id} if resource_id else set(self.resources), policy_input=True
            )
            if self._input_flush is not None:
                _, cancelled = await async_settle(self._input_flush)
                if cancelled:
                    raise asyncio.CancelledError
        else:
            self._recompute()
            self._wake_all()
            self._notify()

    def _state_value(self, entity_id: str, attribute: str | None = None) -> Any:
        # Native HA attributes are dynamically supplied and narrowed by each consumer.
        state = self.hass.states.get(entity_id)
        if (
            state is None
            or state.state in _UNKNOWN
            or state.attributes.get("restored")
            or state.attributes.get("assumed_state")
            or state.attributes.get("optimistic")
        ):
            return None
        return state.attributes.get(attribute) if attribute else state.state

    def _eligible(self, config: PolicyConfig, policy_id: str) -> bool:
        if source := config.get("input"):
            record = self._state["policy_inputs"].get(policy_id)
            if record is None or record["fingerprint"] != self._input_fingerprints[policy_id]:
                return False
            if source["type"] == "qualified_numeric":
                state = NumericState.from_record(record["state"])
                return state.qualified and not state.recovery_pending
            return TimerState.from_record(record["state"]).phase == "accepted"
        entity_id = config.get("eligibility_entity")
        return not entity_id or self._state_value(entity_id) == config.get(
            "eligibility_state", "on"
        )

    def _policy_target(self, config: PolicyConfig) -> Target | None:
        target: dict[str, object] = dict(config.get("target", {}))
        if intent_id := config.get("intent_id"):
            desired = self.desired_value(intent_id)
            return Target(on=desired) if desired is not None else None
        if target_entity := config.get("target_entity"):
            value = self._state_value(target_entity, config.get("target_attribute"))
            if value is None:
                return None
            field = config.get("target_field", "position")
            if field in {"position", "percentage"}:
                if isinstance(value, bool):
                    return None
                try:
                    value = float(value)
                except ValueError, TypeError, OverflowError:
                    return None
            elif field == "on":
                if isinstance(value, bool):
                    pass
                elif isinstance(value, str) and value in {"on", "off"}:
                    value = value == "on"
                else:
                    return None
            target[field] = value
        try:
            return self.adapter(config["resource_id"]).normalize(Target.from_dict(target))
        except ValueError, TypeError, HomeAssistantError:
            return None

    def _requirement(self, key: str, config: RequirementConfig) -> Requirement:
        activations = [self._state_value(entity_id) for entity_id in config["activation_entities"]]
        active = (
            False
            if "off" in activations
            else (None if None in activations else all(value == "on" for value in activations))
        )
        providers = []
        for provider in config["providers"]:
            evidence = []
            for predicate in provider["evidence"]:
                value = self._state_value(predicate["entity_id"], predicate.get("attribute"))
                evidence.append(
                    Evidence(
                        value,
                        predicate["value"],
                        operator=predicate["operator"],
                        available=value is not None,
                        kind=predicate["kind"],
                    )
                )
            target = Target.from_dict(provider["target"]) if provider.get("target") else None
            eligible = True
            if provider.get("resource_id") and target is not None:
                try:
                    target = self.adapter(provider["resource_id"]).normalize(target)
                except ValueError, HomeAssistantError:
                    eligible = False
            providers.append(
                Provider(
                    provider["id"],
                    provider.get("resource_id"),
                    target,
                    confirmed=evaluate_evidence(evidence),
                    eligible=eligible,
                )
            )
        return Requirement(
            key,
            active,
            tuple(providers),
            config["acquisition_timeout"],
            # Requirement configuration admits no retry_interval override.
            300,
        )

    @callback
    def _input_changed(self, event: StateEvent | datetime) -> None:
        if self._closed:
            return
        if not _is_state_event(event):
            # Absolute lease/occurrence and provider deadlines need no telemetry.
            self._queue_reconciliation(
                set(self.resources), policy_input=bool(self._input_fingerprints)
            )
            return
        entity_id = event.data["entity_id"]
        affected = self._input_resources.get(entity_id, set())
        policy_input = False
        if entity_id in self._policy_sources:
            native = event.data.get("new_state")
            if entity_id.startswith("timer."):
                if event.event_type == EVENT_STATE_CHANGED:
                    # HA state-changed events carry EventStateChangedData.
                    changed_event = cast(Event[EventStateChangedData], event)
                    self._capture_timer_state(
                        entity_id, native, changed_event.data.get("old_state")
                    )
                    if (
                        native is None
                        or native.state in _UNKNOWN
                        or any(
                            native.attributes.get(key)
                            for key in ("restored", "assumed_state", "optimistic")
                        )
                    ):
                        for policy_id in self._policy_sources[entity_id]:
                            if episode := self._timer_episodes.get(policy_id):
                                record = self._state["policy_inputs"].get(policy_id)
                                accepted = (
                                    record is not None
                                    and record["state"].get("episode_id") == episode
                                    and record["state"].get("phase") == "accepted"
                                )
                                if accepted:
                                    continue
                                policy_input = True
                                self._policy_events.append(
                                    (
                                        policy_id,
                                        TimerEvent("suppress", episode),
                                        event.time_fired.timestamp(),
                                        event.context,
                                        False,
                                    )
                                )
            else:
                unchanged = (
                    event.event_type == EVENT_STATE_REPORTED
                    and not self.fault
                    and self._input_flush is None
                    and not self._pending_resources
                    and not self._policy_events
                )
                if unchanged:
                    for policy_id in self._policy_sources[entity_id]:
                        record = self._state["policy_inputs"].get(policy_id)
                        if (
                            record is None
                            or record["type"] != "qualified_numeric"
                            or record["fingerprint"] != self._input_fingerprints[policy_id]
                        ):
                            unchanged = False
                            break
                        previous_numeric = NumericState.from_record(record["state"])
                        # Validated sensor sources have qualified-numeric configuration.
                        source = cast(NumericInputConfig, self.policies[policy_id]["input"])
                        config = NumericInput(source["threshold"], source["qualification_seconds"])
                        report = self._numeric_reports.get(policy_id, NumericReport(None))
                        reported = numeric_transition(
                            config, previous_numeric, report, now=event.time_fired.timestamp()
                        )
                        current = numeric_transition(config, reported.state, None, now=_now())
                        if previous_numeric.recovery_pending or current.state != previous_numeric:
                            unchanged = False
                            break
                if not unchanged:
                    for policy_id in self._policy_sources[entity_id]:
                        if event.event_type == EVENT_STATE_CHANGED:
                            # Configuration validates sensor sources as qualified-numeric inputs.
                            self._numeric_reports[policy_id] = self._numeric_report(
                                cast(NumericInputConfig, self.policies[policy_id]["input"]), native
                            )
                        report = self._numeric_reports.get(policy_id, NumericReport(None))
                        self._policy_events.append(
                            (policy_id, report, event.time_fired.timestamp(), event.context, True)
                        )
                    self._queue_reconciliation(affected, policy_input=True)
                    return
                # Native input is unchanged; shared roles still need the ordinary report checks.
        if event.event_type == EVENT_STATE_REPORTED:
            # HA reports unchanged values separately from state/attribute changes.
            # Keep fresh observations (including relay report timestamps) without
            # turning a heartbeat into an engine run or native entity publication.
            changed = False
            for identifier in self._feedback_resources.get(entity_id, ()):
                previous = self.observations.get(identifier)
                observation = self.adapter(identifier).read_observation(_now())
                self.observations[identifier] = observation
                changed |= (
                    previous is None
                    or replace(previous, reported_at=observation.reported_at) != observation
                )
            # In-flight adapters may need this report for fresh confirmation.
            # Their own native event subscriptions still receive every report.
            deadline_due = self._next_evaluation is not None and self._next_evaluation <= _now()
            if not changed and not affected & self._applying and not deadline_due:
                return
        self._queue_reconciliation(affected, policy_input=policy_input)

    @callback
    def _queue_reconciliation(self, resource_ids: set[str], *, policy_input: bool = False) -> None:
        if self._closed:
            return
        self._pending_resources.update(resource_ids)
        if policy_input:
            for resource_id in resource_ids:
                self._input_ingress[resource_id] += 1
        if self._input_flush is None:
            # One HA-tracked task per pending batch, never one task per input.
            # Non-eager scheduling lets same-turn changes join the batch.
            self._input_flush = self.hass.async_create_task(
                self._async_flush_inputs(), "ha_operator:inputs", eager_start=False
            )

    async def _async_flush_inputs(self) -> None:
        # Keep ownership across input state changes; arriving edges join this drain.
        try:
            affected = set()
            while not self._closed:
                affected.update(self._pending_resources)
                self._pending_resources.clear()
                while self._policy_events and not self._closed:
                    policy_id, event, captured_at, context, admissible = (
                        self._policy_events.popleft()
                    )
                    await self._apply_policy_input(
                        policy_id, event, captured_at, context, admissible
                    )
                for policy_id in self._input_fingerprints:
                    await self._apply_policy_input(policy_id, None, _now(), None, True)
                if self._return_fingerprints:
                    self._recompute()
                    if self._return_dirty:
                        await self._apply_return_monitors()
                if (
                    not self._pending_resources
                    and not self._policy_events
                    and not self._return_dirty
                ):
                    break
            if self._closed:
                return
            self._input_processed.update(self._input_ingress)
            previous = self.decisions
            self._recompute()
            affected.update(
                key for key, value in self.decisions.items() if previous.get(key) != value
            )
            self._wake_resources(affected)
            self._notify()
        except HomeAssistantError:
            # Closed or unusable runtimes reject input changes.
            pass
        finally:
            self._input_flush = None
            self._pending_resources.update(
                key
                for key in self.resources
                if self._input_ingress[key] != self._input_processed[key]
            )
            if (
                not self._closed
                and not self.fault
                and (self._pending_resources or self._policy_events or self._return_dirty)
            ):
                self._queue_reconciliation(set())

    @callback
    def _wake_all(self) -> None:
        self._wake_resources(self.resources)

    @callback
    def _wake_resources(self, resource_ids: Iterable[str]) -> None:
        for identifier in resource_ids:
            if self.decisions[identifier].status in {"pending", "waiting"}:
                self._wake[identifier].set()

    def policy_input(self, policy_id: str) -> NumericState | TimerState | None:
        """Expose current input state without scheduling or mutating qualification."""
        record = self._state["policy_inputs"].get(policy_id)
        if record is None:
            return None
        model = NumericState if record["type"] == "qualified_numeric" else TimerState
        return model.from_record(record["state"])

    def return_monitor(self, resource_id: str) -> ReturnMonitorState:
        """Expose current monitor state; telemetry and desired values stay separate."""
        record = self._state["return_monitors"].get(resource_id)
        return ReturnMonitorState.from_record(record["state"]) if record else ReturnMonitorState()

    def _return_transition(
        self, resource_id: str, state: ReturnMonitorState, now: float
    ) -> ReturnMonitorTransition:
        config = self.resources[resource_id]
        settings = config["return_monitor"]
        decision = self.decisions.get(resource_id)
        observation = self.adapter(resource_id).read_observation(now)
        return return_transition(
            ReturnMonitorInput(
                settings["target_at_most"], settings["warning_after_seconds"], config["tolerance"]
            ),
            state,
            target_position=decision.target.position if decision and decision.target else None,
            observed_position=(
                observation.target.position
                if observation.available and observation.target
                else None
            ),
            active=bool(
                not self.fault
                and self.mode(resource_id) == "live"
                and decision
                and decision.status not in {"idle", "hands_off", "fault", "observe"}
            ),
            now=now,
        )

    async def _apply_return_monitors(self) -> None:
        self._return_dirty.clear()

        def mutate(state: RuntimeSnapshot) -> None:
            # Use current inputs in this synchronous state update.
            self._recompute(mark_returns=False)
            for key, fingerprint in self._return_fingerprints.items():
                previous = ReturnMonitorState.from_record(state["return_monitors"][key]["state"])
                state["return_monitors"][key] = {
                    "fingerprint": fingerprint,
                    "state": self._return_transition(key, previous, _now()).state.to_record(),
                }

        await self._change(mutate, skip_unchanged=True)

    def _sync_return_notifications(self, now: float) -> None:
        if self._closed or self.fault or self._policy_events:
            return
        for key in self._return_fingerprints:
            if self._input_ingress[key] != self._input_processed[key]:
                continue
            state = self.return_monitor(key)
            if self._return_transition(key, state, now).state != state:
                continue
            episode = (state.target_position, state.due_at) if state.overdue else None
            if key in self._return_notifications and self._return_notifications[key] == episode:
                continue
            notification_id = f"ha_operator_{self.entry.entry_id}_{key}_return"
            if state.overdue:
                observation = self.adapter(key).read_observation(now)
                detail = (
                    "Raw position feedback is unavailable; the return is unconfirmed."
                    if not observation.available or observation.target is None
                    else f"Return target {state.target_position:g}% has not been reached; "
                    f"raw position is {observation.target.position:g}%."
                )
                persistent_notification.async_create(
                    self.hass,
                    f"{self.resources[key]['name']}: {detail}",
                    title="HA Operator: return target not confirmed",
                    notification_id=notification_id,
                )
            else:
                persistent_notification.async_dismiss(self.hass, notification_id)
            self._return_notifications[key] = episode

    def _recompute(self, *, mark_returns: bool = True) -> None:
        if self._closed:
            return
        now = _now()
        resources = {}
        for key, config in self.resources.items():
            adapter = self.adapter(key)
            available = adapter.source_available
            previous = self._source_availability.get(key)
            if not available and previous is not False:
                _LOGGER.info("Resource %s source is unavailable", key)
            elif available and previous is False:
                _LOGGER.info("Resource %s source is available again", key)
            self._source_availability[key] = available
            observation = adapter.read_observation(now)
            fault = self.fault
            if config.get("fault_entity") and self._state_value(config["fault_entity"]) != "off":
                fault = "device_fault_or_unknown"
            if (
                config.get("restriction_entity")
                and self._state_value(config["restriction_entity"]) != "off"
            ):
                observation = replace(observation, restriction="restriction_or_unknown")
            self.observations[key] = observation
            if observation.moving:
                self._motion_started.setdefault(key, now)
            else:
                self._motion_started.pop(key, None)
            resources[key] = Resource(
                key, config["kind"], self.mode(key), config["tolerance"], fault
            )
        try:
            manuals = [lease for key in resources if (lease := self.manual(key)) is not None]
            occurrences = [
                Occurrence(
                    item["policy_id"],
                    item["occurrence_id"],
                    item["expires_at"],
                    item.get("skipped", False),
                    target=Target.from_dict(item["target"]) if item.get("target") else None,
                )
                for item in self._state["occurrences"].values()
            ]
        except (KeyError, ValueError, TypeError) as err:
            self._invalid_state(err)
            manuals, occurrences = [], []
            resources = {key: replace(item, fault=self.fault) for key, item in resources.items()}
        engine: EngineInputs = dict(
            now=now,
            resources=resources,
            observations=self.observations,
            manuals=manuals,
            policies=[
                Policy(
                    key,
                    config["resource_id"],
                    self._policy_target(config),
                    config["priority"],
                    self.policy_enabled(key),
                    self._eligible(config, key),
                    config["kind"],
                )
                for key, config in self.policies.items()
            ],
            occurrences=occurrences,
            requirements=[
                self._requirement(key, config) for key, config in self.requirements.items()
            ],
            requirement_memory=self._memory,
        )
        result = evaluate(**engine)
        valid_contexts = {
            *(("intent", key) for key in self.intents),
            *(("manual", lease.request_id) for lease in manuals),
            *(
                ("occurrence", item.policy_id, item.occurrence_id)
                for item in occurrences
                if not item.skipped and item.expires_at > now
            ),
        }
        self._contexts = {
            key: value for key, value in self._contexts.items() if key in valid_contexts
        }
        previous_decisions = self.decisions
        previous_selections = self.selections
        # HA state reads and evaluation are synchronous: retain this cause snapshot,
        # rather than scanning changed group members when a dashboard reads it later.
        activations = {
            key: self._active_inputs(config["activation_entities"])
            for key, config in self.requirements.items()
        }
        self.selections = {
            key: selection_for(
                decision,
                self.manual(key),
                self.policies,
                occurrences,
                self.requirements,
                activations,
            )
            for key, decision in result.decisions.items()
        }
        registry = er.async_get(self.hass)
        for key, selection in self.selections.items():
            policy = (
                self.policies.get(selection.source_id) if selection.source_id is not None else None
            )
            if policy is not None and (intent_id := policy.get("intent_id")):
                name = self.intents[intent_id]["name"][:128]
                entity_id = registry.async_get_entity_id("switch", DOMAIN, f"{intent_id}_desired")
                self.selections[key] = replace(
                    selection,
                    source_kind="intent",
                    source_id=intent_id,
                    source_name=name,
                    selection_reason=f"Following {name}",
                    related_entities=(entity_id,) if entity_id else (),
                )
        self.decisions = dict(result.decisions)
        for key, decision in self.decisions.items():
            signature = (
                decision.target,
                decision.source,
                self.selections[key].request_id,
                self.mode(key),
                decision.status
                in {
                    "idle",
                    "hands_off",
                    "unavailable",
                    "restricted",
                    "fault",
                    "observe",
                },
            )
            if self._signatures.get(key) != signature:
                self._generation[key] += 1
                self._signatures[key] = signature
            if decision.status == "pending":
                if key in self._applying:
                    decision = replace(decision, status="applying")
                elif self.next_attempts.get(key, 0) > now:
                    decision = replace(
                        decision,
                        status="waiting",
                        reason=self._errors.get(key, "target_not_reached"),
                    )
                self.decisions[key] = decision
            else:
                self.next_attempts.pop(key, None)
                self._last_identity.pop(key, None)
                if cancel := self._timers.pop(key, None):
                    cancel()
            old = previous_decisions.get(key)
            selection = self.selections[key]
            cause_changed = previous_selections.get(key) != selection
            if (
                cause_changed
                or old is None
                or (old.status, old.target, old.source)
                != (
                    decision.status,
                    decision.target,
                    decision.source,
                )
            ):
                if old is not None and (
                    cause_changed
                    or old.target != decision.target
                    or (
                        old.status != decision.status
                        and decision.status not in {"applying", "waiting", "pending"}
                    )
                ):
                    self.hass.bus.async_fire(
                        "logbook_entry",
                        {
                            "name": self.resources[key]["name"],
                            "message": (
                                f"{selection.selection_reason}; "
                                f"target {target_label(decision.target)}; "
                                f"{decision.reason}"
                            ),
                            "domain": DOMAIN,
                            "entity_id": er.async_get(self.hass).async_get_entity_id(
                                "fan"
                                if self.resources[key]["kind"] == "relay_fan"
                                else self.resources[key]["kind"],
                                DOMAIN,
                                f"{key}_managed",
                            )
                            or er.async_get(self.hass).async_get_entity_id(
                                "sensor",
                                DOMAIN,
                                f"{key}_observed",
                            ),
                        },
                        context=self._selection_context(key) if cause_changed else None,
                    )
        old_requirements = self.requirement_results
        self.requirement_results = dict(result.requirements)
        self._requirement_details = self._explain_requirements()
        self._memory = {key: item.memory for key, item in result.requirements.items()}
        unconfirmed = {"acquiring", "unmet", "unknown"}
        for key, item in self.requirement_results.items():
            old_requirement = old_requirements.get(key)
            notification_id = f"ha_operator_{self.entry.entry_id}_{key}"
            if item.status in unconfirmed and (
                old_requirement is None or old_requirement.status not in unconfirmed
            ):
                persistent_notification.async_create(
                    self.hass,
                    f"{self.requirements[key]['name']}: incoming air is not confirmed. "
                    "Eligible alternatives will be tried. "
                    "Extraction remains under its existing control.",
                    title="HA Operator: airflow requirement",
                    notification_id=notification_id,
                )
            elif (
                old_requirement
                and old_requirement.status in unconfirmed
                and item.status not in unconfirmed
            ):
                persistent_notification.async_dismiss(self.hass, notification_id)
        if self._deadline_timer:
            self._deadline_timer()
            self._deadline_timer = None
        deadlines = [result.next_evaluation] if result.next_evaluation is not None else []
        for policy_id, source_hash in self._input_fingerprints.items():
            record = self._state["policy_inputs"].get(policy_id)
            if record is None or record["fingerprint"] != source_hash:
                continue
            if record["type"] == "qualified_numeric":
                numeric_state = NumericState.from_record(record["state"])
                if (
                    not numeric_state.qualified
                    and not numeric_state.recovery_pending
                    and numeric_state.source_quality == "numeric"
                ):
                    if numeric_state.due_at is not None:
                        deadlines.append(numeric_state.due_at)
            else:
                timer_state = TimerState.from_record(record["state"])
                if timer_state.phase == "qualifying":
                    assert timer_state.due_at is not None
                    deadlines.append(timer_state.due_at)
                elif timer_state.phase == "accepted":
                    assert timer_state.expires_at is not None
                    deadlines.append(timer_state.expires_at)
        if mark_returns and not self.fault:
            for key in self._return_fingerprints:
                state = self.return_monitor(key)
                transition = self._return_transition(key, state, now)
                if transition.state != state:
                    self._return_dirty.add(key)
                if transition.next_deadline is not None and transition.next_deadline > now:
                    deadlines.append(transition.next_deadline)
            if self._return_dirty:
                self._queue_reconciliation(set(self._return_dirty))
            self._sync_return_notifications(now)
        self._next_evaluation = min(deadlines) if deadlines else None
        if self._next_evaluation is not None:
            self._deadline_timer = async_call_later(
                self.hass, max(0.01, self._next_evaluation - now), self._input_changed
            )

    def _selection_context(self, resource_id: str) -> Context | None:
        selection = self.selections[resource_id]
        if selection.source_kind == "intent":
            return self._contexts.get(("intent", selection.source_id))
        key = (
            ("manual", selection.request_id)
            if selection.source_kind == "manual"
            else ("occurrence", selection.source_id, selection.occurrence_id)
        )
        return self._contexts.get(key)

    def selection_attributes(self, resource_id: str) -> dict[str, object]:
        """Resolve a desired control's entity name after native registry renames."""
        selection = self.selections.get(resource_id)
        if selection is None:
            return {}
        attributes = selection.attributes()
        if selection.source_kind == "intent":
            entity_id = er.async_get(self.hass).async_get_entity_id(
                "switch", DOMAIN, f"{selection.source_id}_desired"
            )
            attributes["related_entities"] = (entity_id,) if entity_id else ()
        return attributes

    def _active_inputs(self, entity_ids: Iterable[str]) -> tuple[tuple[str, str], ...]:
        """Bound group expansion; report active inputs, never invent physical proof."""
        pending = list(entity_ids)
        seen: set[str] = set()
        active = []
        while pending and len(seen) < 32:
            entity_id = pending.pop(0)
            if entity_id in seen:
                continue
            seen.add(entity_id)
            if self._state_value(entity_id) != "on":
                continue
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            members = state.attributes.get("entity_id")
            if isinstance(members, (list, tuple)) and members:
                pending.extend(item for item in members[:32] if isinstance(item, str))
            else:
                active.append(
                    (entity_id, str(state.attributes.get("friendly_name", entity_id))[:80])
                )
        return tuple(active)

    @callback
    def _activation_input_changed(self, event: Event[EventStateChangedData]) -> None:
        """Group membership can change cause while the group's aggregate stays on."""
        entity_id = event.data["entity_id"]
        if self._closed or entity_id in self._observed_entities:
            return
        pending = [
            item for config in self.requirements.values() for item in config["activation_entities"]
        ]
        seen: set[str] = set()
        while pending and len(seen) < 32:
            item = pending.pop(0)
            if item in seen:
                continue
            seen.add(item)
            if item == entity_id:
                # Expanded members can affect provider selection without changing
                # the aggregate group state. Include every possible old/new route.
                self._queue_reconciliation(
                    {
                        provider["resource_id"]
                        for config in self.requirements.values()
                        for provider in config["providers"]
                        if provider.get("resource_id")
                    }
                )
                return
            state = self.hass.states.get(item)
            members = state.attributes.get("entity_id") if state else None
            if isinstance(members, (list, tuple)):
                pending.extend(member for member in members[:32] if isinstance(member, str))

    def _schedule(self, resource_id: str, when: float) -> None:
        if cancel := self._timers.pop(resource_id, None):
            cancel()

        @callback
        def wake(_: datetime) -> None:
            self._timers.pop(resource_id, None)
            self._wake[resource_id].set()

        self._timers[resource_id] = async_call_later(self.hass, max(0.01, when - _now()), wake)

    async def _worker(self, resource_id: str) -> None:
        wake = self._wake[resource_id]
        config = self.resources[resource_id]
        while not self._closed:
            await wake.wait()
            wake.clear()
            if self._input_ingress[resource_id] != self._input_processed[resource_id]:
                continue
            self._recompute()
            decision = self.decisions[resource_id]
            if decision.status not in {"pending", "waiting"} or decision.target is None:
                if cancel := self._timers.pop(resource_id, None):
                    cancel()
                self._notify()
                continue
            now = _now()
            identity = (decision.target, decision.source)
            due = self._last_send.get(resource_id, -math.inf) + config["command_interval"]
            if (
                self._last_identity.get(resource_id) == identity
                and resource_id in self.next_attempts
            ):
                due = max(due, self._last_send[resource_id] + config["retry_interval"])
            if self.observations[resource_id].moving:
                due = max(due, self._motion_started[resource_id] + config["movement_timeout"])
            if due > now:
                self.next_attempts[resource_id] = due
                self._schedule(resource_id, due)
                continue
            generation = self._generation[resource_id]
            target = decision.target

            def still_current(generation: int = generation, target: Target = target) -> bool:
                if self._input_ingress[resource_id] != self._input_processed[resource_id]:
                    return False
                self._recompute()
                return (
                    not self._closed
                    and not self.fault
                    and self.mode(resource_id) == "live"
                    and self._generation[resource_id] == generation
                    and self.decisions[resource_id].target == target
                    and self.decisions[resource_id].status in {"applying", "pending", "waiting"}
                )

            self._applying.add(resource_id)
            self.attempts[resource_id] = self.attempts.get(resource_id, 0) + 1
            self._last_identity[resource_id] = identity
            self._last_send[resource_id] = now
            self.next_attempts[resource_id] = now + max(
                config["retry_interval"], config["command_interval"]
            )
            context_token = DISPATCH_PARENT.set(self._selection_context(resource_id))
            try:
                async with self._actuator_locks[resource_id]:
                    sent = await self.adapter(resource_id).async_apply(target, still_current)
                if sent:
                    self.last_commands[resource_id] = {"target": target.to_dict(), "at": now}
                    self._errors.pop(resource_id, None)
            except (HomeAssistantError, ValueError) as err:
                self._errors[resource_id] = "delivery_failed"
                _LOGGER.debug("Resource %s delivery failed: %s", resource_id, err)
            finally:
                DISPATCH_PARENT.reset(context_token)
                self._applying.discard(resource_id)
                # A relay transaction may spend longer than the retry interval
                # awaiting off feedback, dead time, or a slow transport. Give
                # its final effects a full observation interval before retrying.
                # Telemetry never moves this completion-based pacing boundary.
                self._last_send[resource_id] = completed_at = _now()
                if not self._closed and self._generation[resource_id] == generation:
                    self.next_attempts[resource_id] = completed_at + max(
                        config["retry_interval"], config["command_interval"]
                    )
            if not self._closed:
                self._recompute()
                self._notify()
                if self.decisions[resource_id].status in {"pending", "waiting"}:
                    wake.set()

    def _explain_requirements(self) -> dict[str, dict[str, object]]:
        """Capture bounded predicates in the same synchronous evaluation turn."""
        requirements: dict[str, dict[str, object]] = {}
        for key, item in self.requirement_results.items():
            config = self.requirements[key]
            providers: dict[str, dict[str, object]] = {}
            for provider in config["providers"]:
                predicates = []
                evidence = []
                for predicate in provider["evidence"]:
                    observed = self._state_value(predicate["entity_id"], predicate.get("attribute"))
                    value = Evidence(
                        observed,
                        predicate["value"],
                        operator=predicate["operator"],
                        available=observed is not None,
                        kind=predicate["kind"],
                    )
                    evidence.append(value)
                    predicates.append(
                        {
                            "kind": predicate["kind"],
                            "entity_id": predicate["entity_id"],
                            "attribute": predicate.get("attribute"),
                            "operator": predicate["operator"],
                            "expected": predicate["value"],
                            "observed": observed,
                            "confirmed": evaluate_evidence((value,)),
                        }
                    )
                providers[provider["id"]] = {
                    "resource_id": provider.get("resource_id"),
                    "target": provider.get("target"),
                    "confirmed": evaluate_evidence(evidence),
                    "evidence": predicates,
                }
            requirements[key] = {
                **asdict(item),
                "activation_values": {
                    entity_id: self._state_value(entity_id)
                    for entity_id in config["activation_entities"]
                },
                "providers": providers,
            }
        return requirements

    def explain(self, resource_id: str | None = None) -> dict[str, object]:
        if resource_id is not None:
            self._resource(resource_id)
        ids = [resource_id] if resource_id is not None else list(self.resources)
        return {
            "fault": self.fault,
            "shadow_locked": self.shadow_locked,
            "resources": {
                key: {
                    "mode": self.mode(key),
                    "decision": asdict(self.decisions[key]) if key in self.decisions else None,
                    "selection": self.selection_attributes(key) if key in self.selections else None,
                    "observation": asdict(self.observations[key])
                    if key in self.observations
                    else None,
                    "manual": asdict(lease) if (lease := self.manual(key)) else None,
                    "last_command": self.last_commands.get(key),
                    "next_attempt": self.next_attempts.get(key),
                    "attempts": self.attempts.get(key, 0),
                }
                for key in ids
            },
            "requirements": deepcopy(self._requirement_details),
        }
