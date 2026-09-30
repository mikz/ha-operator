"""HA event boundary, durable admission, and one actuator worker per resource."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from homeassistant.components import persistent_notification
from homeassistant.core import (
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    Context,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.util import dt as dt_util

from .adapters import DISPATCH_PARENT, create_adapter
from .async_utils import async_settle
from .configuration import validate_configuration
from .const import DOMAIN
from .core import (
    Decision,
    Evidence,
    ManualLease,
    Occurrence,
    Policy,
    Provider,
    Requirement,
    Resource,
    Target,
    evaluate,
    evaluate_evidence,
)
from .intents import apply_command
from .provenance import Selection, selection_for, target_label
from .shadow import ShadowTrace
from .storage import IntentStore, IntentStoreError

_LOGGER = logging.getLogger(__name__)
_UNKNOWN = {"unknown", "unavailable"}


def _now() -> float:
    return dt_util.utcnow().timestamp()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ServiceValidationError(f"{label} must be a finite number")
    return float(value)


def _validate_saved_state(state: dict) -> None:
    """Validate intent records before publishing or pruning a loaded snapshot."""

    def record(item):
        if not isinstance(item, dict):
            raise ValueError("Saved intent record must be an object")
        return item

    def text(value, label):
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ValueError(f"Saved {label} must contain 1 to 128 characters")
        return value

    for key, value in state["manuals"].items():
        item = record(value)
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
        if item.get("fingerprint_kind", "normalized") not in {"normalized", "requested"}:
            raise ValueError("Saved request fingerprint kind is invalid")
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


class OperatorRuntime:
    """Recompute current intent; never enqueue historical target commands."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass, self.entry = hass, entry
        configured = validate_configuration(entry.subentries)
        self.resources = configured["resources"]
        self.policies = configured["policies"]
        self.requirements = configured["requirements"]
        self.intents = configured["intents"]
        self._shadow_lock_latched = bool(entry.options.get("shadow_lock", False))
        self._trace = ShadowTrace(hass, entry, configured, entry.options.get("trace_entities", []))
        self.store = IntentStore(hass, Path(hass.config.path(f"ha_operator.{entry.entry_id}.json")))
        self.observations: dict[str, Any] = {}
        self.decisions: dict[str, Decision] = {}
        self.selections: dict[str, Selection] = {}
        self._contexts: dict[tuple, Context] = {}
        self.requirement_results: dict[str, Any] = {}
        self._requirement_details: dict[str, Any] = {}
        self.last_commands: dict[str, dict] = {}
        self.next_attempts: dict[str, float] = {}
        self.attempts: dict[str, int] = {}
        self.history: deque[dict] = deque(maxlen=100)
        self.fault: str | None = None
        self._adapters = {
            key: create_adapter(hass, key, config) for key, config in self.resources.items()
        }
        if self._trace.enabled:
            for adapter in self._adapters.values():
                adapter.audit_callback = lambda data: self._trace.event("dispatch", data)
        self._listeners: set[Callable[[], None]] = set()
        self._unsubscribers: list[Callable[[], None]] = []
        self._timers: dict[str, Callable[[], None]] = {}
        self._deadline_timer: Callable[[], None] | None = None
        self._next_evaluation: float | None = None
        self._wake = {key: asyncio.Event() for key in self.resources}
        self._actuator_locks = {key: asyncio.Lock() for key in self.resources}
        self._tasks: list[asyncio.Task] = []
        self._stop_tasks: set[asyncio.Task] = set()
        self._close_task: asyncio.Task | None = None
        self._generation = dict.fromkeys(self.resources, 0)
        self._signatures: dict[str, Any] = {}
        self._last_send: dict[str, float] = {}
        self._motion_started: dict[str, float] = {}
        self._last_identity: dict[str, Any] = {}
        self._applying: set[str] = set()
        self._errors: dict[str, str] = {}
        self._emergency_hands_off: dict[str, ManualLease] = {}
        self._memory: dict[str, Any] = {}
        self._closed = False
        self._state = self.store.state
        self._input_flush: asyncio.Task | None = None
        self._pending_resources: set[str] = set()
        self._feedback_resources: dict[str, set[str]] = {}
        self._input_resources = self._input_dependencies()
        self._decision_input_entities = set(self._input_resources)
        self._observed_entities = self._decision_input_entities | (
            self._trace.sanitizer.attrs.keys() if self._trace.enabled else set()
        )

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
                config[key] for key in ("restriction_entity", "fault_entity") if config.get(key)
            }
            for entity_id in inputs:
                dependencies.setdefault(entity_id, set()).add(identifier)
        for config in self.policies.values():
            for key in ("eligibility_entity", "target_entity"):
                if entity_id := config.get(key):
                    dependencies.setdefault(entity_id, set()).add(config["resource_id"])
        groups = []
        for config in self.requirements.values():
            owners = {p["resource_id"] for p in config["providers"] if p.get("resource_id")}
            groups.append(owners)
            inputs = set(config["activation_entities"]) | {
                evidence["entity_id"]
                for provider in config["providers"]
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

    async def async_start(self) -> None:
        try:
            await self.store.async_load(expected_existing=self.entry.data.get("initialized", False))
            _validate_saved_state(self.store.state)
            if any(key not in self.store.state["intents"] for key in self.intents):

                def initialize(state):
                    for key, config in self.intents.items():
                        state["intents"].setdefault(key, config["initial_value"])

                await self.store.async_update(initialize)
            if self.shadow_locked and any(
                self.store.state["modes"].get(key) == "live" for key in self.resources
            ):
                await self.store.async_update(
                    lambda state: state["modes"].update(dict.fromkeys(self.resources, "observe"))
                )
            if not self.entry.data.get("initialized"):
                await self.store.async_update(lambda _: None)
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, "initialized": True}
                )
            now = _now()

            def prune(state):
                state["manuals"] = {
                    key: item
                    for key, item in state["manuals"].items()
                    if (item.get("expires_at") is None or item["expires_at"] > now)
                    and self.manual_control(key)
                }

            if any(
                (item.get("expires_at") is not None and item["expires_at"] <= now)
                or not self.manual_control(key)
                for key, item in self.store.state["manuals"].items()
            ):
                await self.store.async_update(prune)
        except (IntentStoreError, ValueError, TypeError, KeyError) as err:
            self.store.fault = self.store.fault or "Saved intent records are invalid"
            self._storage_fault(err)
        else:
            self._state = self.store.state
        await self._trace.async_start(self._state, self.shadow_locked)
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
        if self._trace.enabled:
            self._unsubscribers.append(
                self.hass.bus.async_listen("homekit_state_change", self._external_command)
            )
            for entity_id in sorted(self._trace.sanitizer.attrs):
                self._trace.input(entity_id, self.hass.states.get(entity_id), "initial")
            self._trace.record("snapshot_end", {"entities": len(self._trace.sanitizer.attrs)})
        self._recompute()
        self._tasks = [
            self.entry.async_create_background_task(
                self.hass, self._worker(key), f"ha_operator:{key}"
            )
            for key in self.resources
        ]
        self._wake_all()

    async def async_close(self) -> None:
        if self._close_task is None:
            self._close_task = self.hass.async_create_task(
                self._async_close(), "ha_operator:quiesce"
            )
        _, cancelled = await async_settle(self._close_task)
        if cancelled:
            raise asyncio.CancelledError

    async def _async_close(self) -> None:
        self._closed = True
        flush, self._input_flush = self._input_flush, None
        if flush is not None:
            flush.cancel()
        self._pending_resources.clear()
        for key in self._generation:
            self._generation[key] += 1
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
        # A lock observed by this instance cannot be undone by an options race
        # before reload. Save observe before a replacement can start unlocked.
        if (
            self.shadow_locked
            and not self.fault
            and not self.store.fault
            and any(self.store.state["modes"].get(key) == "live" for key in self.resources)
        ):
            try:
                await self.store.async_update(
                    lambda state: state["modes"].update(dict.fromkeys(self.resources, "observe"))
                )
            except IntentStoreError as err:
                self._storage_fault(err)
                # Direct options mutations can bypass the native unlock flow.
                # Retain the lock for the replacement if demotion was uncertain.
                self.hass.config_entries.async_update_entry(
                    self.entry, options={**self.entry.options, "shadow_lock": True}
                )
        await self.store.async_close()
        await self._trace.async_close()
        self._listeners.clear()

    @callback
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def adapter(self, resource_id: str):
        return self._adapters[resource_id]

    def mode(self, resource_id: str) -> str:
        if self.shadow_locked:
            return "observe"
        return self._state["modes"].get(resource_id, "observe")

    @property
    def shadow_locked(self) -> bool:
        self._shadow_lock_latched |= bool(self.entry.options.get("shadow_lock", False))
        return self._shadow_lock_latched

    def trace_health(self) -> dict:
        return self._trace.health()

    async def async_export_trace(self, *, after: int | None = None, limit: int = 100) -> dict:
        return await self._trace.async_export(after, limit)

    async def async_prepare_unlock(self) -> None:
        """Durably demote all resources before the options flow removes its lock."""
        if self._closed:
            raise HomeAssistantError("HA Operator is unloaded")
        if self.shadow_locked:
            await self._commit(
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

    def _resource(self, resource_id: str) -> dict:
        if resource_id not in self.resources:
            raise ServiceValidationError("Unknown resource")
        if self._closed:
            raise HomeAssistantError("HA Operator is unloaded")
        return self.resources[resource_id]

    def _admit(self, resource_id: str) -> dict:
        config = self._resource(resource_id)
        if self.fault or self.store.fault:
            raise HomeAssistantError("HA Operator storage is inhibited; inspect Repairs")
        if self.mode(resource_id) != "live":
            raise ServiceValidationError("Resource is in observe mode; request not accepted")
        return config

    def manual_control(self, resource_id: str) -> bool:
        return self.resources.get(resource_id, {}).get("manual_control", True)

    def _manual_resource(self, resource_id: str) -> dict:
        config = self._resource(resource_id)
        if not self.manual_control(resource_id):
            raise ServiceValidationError(
                "This resource follows its source; manual control is disabled"
            )
        return config

    def desired_value(self, intent_id: str) -> bool | None:
        return self._state["intents"].get(intent_id) if not self.fault else None

    def intent_context(self, intent_id: str) -> Context | None:
        return self._contexts.get(("intent", intent_id))

    async def async_set_desired(self, intent_id: str, on: bool, *, context=None) -> None:
        if self._closed:
            raise HomeAssistantError("HA Operator is unloaded")
        replayed = False
        changed = []

        def mutate(state):
            nonlocal replayed
            if self._closed:
                raise HomeAssistantError("HA Operator is unloaded")
            try:
                previous = dict(state["intents"])
                apply_command(state["intents"], self.intents, intent_id, on)
                changed[:] = [
                    key for key, value in state["intents"].items() if previous.get(key) != value
                ]
                replayed = previous == state["intents"]
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

        await self._commit(
            mutate,
            lambda state: {
                "action": "set_desired",
                "intent_id": intent_id,
                "intents": state["intents"],
                "replayed": replayed,
            },
            context_key=lambda: [("intent", key) for key in changed],
            context=context,
            skip_unchanged=True,
        )

    async def async_seed_intents(self, values: dict) -> None:
        """Import a migration snapshot without replaying logical ON edges."""
        if self._closed:
            raise HomeAssistantError("HA Operator is unloaded")

        def mutate(state):
            if self._closed:
                raise HomeAssistantError("HA Operator is unloaded")
            if (
                not isinstance(values, dict)
                or not values
                or any(
                    key not in self.intents or type(value) is not bool
                    for key, value in values.items()
                )
            ):
                raise ServiceValidationError("Seed configured desired controls with boolean values")
            if any(
                config.get("intent_id") in values and self.mode(config["resource_id"]) != "observe"
                for config in self.policies.values()
            ):
                raise ServiceValidationError(
                    "Seed only while all affected followers are in observe mode"
                )
            state["intents"].update(values)

        await self._commit(mutate, lambda state: {"action": "seed_intents", "intents": values})

    def _storage_fault(self, error: Exception) -> None:
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

    async def _commit(
        self,
        mutator: Callable[[dict], None],
        admission=None,
        *,
        context_key=None,
        context=None,
        skip_unchanged=False,
    ) -> dict:
        if self.fault or self.store.fault:
            raise HomeAssistantError("HA Operator storage is inhibited; inspect Repairs")
        candidate = None

        def own_mutation(state):
            nonlocal candidate
            mutator(state)
            candidate = state

        try:
            return await self.store.async_update(
                own_mutation, **({"skip_unchanged": True} if skip_unchanged else {})
            )
        except IntentStoreError as err:
            self._storage_fault(err)
            raise HomeAssistantError("Request persistence failed; actuation inhibited") from err
        finally:
            # A cancelled request can still have committed. Store drains its executor
            # before cancellation escapes, so always observe the committed revision.
            if not self.fault:
                self._state = self.store.state
                if (
                    admission is not None
                    and candidate is not None
                    and candidate["revision"] <= self._state["revision"]
                ):
                    admitted = admission(candidate)
                    if (
                        context_key is not None
                        and context is not None
                        and not admitted.get("replayed")
                    ):
                        for key in context_key() if callable(context_key) else [context_key]:
                            self._contexts[key] = context
                    self._trace.event("admission", {**admitted, "revision": candidate["revision"]})
            if not self._closed:
                self._recompute()
                self._wake_all()
                self._notify()

    async def async_request(
        self,
        resource_id: str,
        *,
        mode: str = "target",
        target: Target | dict | None = None,
        duration: float | None = None,
        expires_at: float | None = None,
        indefinite: bool = False,
        request_id: str | None = None,
        source: str = "service",
        context: Context | None = None,
    ) -> dict:
        self._manual_resource(resource_id)
        config = self._admit(resource_id)
        if mode not in {"target", "hands_off"}:
            raise ServiceValidationError("mode must be target or hands_off")
        if sum((duration is not None, expires_at is not None, indefinite)) > 1:
            raise ServiceValidationError("Choose duration, expires_at, or indefinite")
        if mode == "hands_off" and target is not None:
            raise ServiceValidationError("hands_off cannot carry a target")
        try:
            requested = (
                None
                if mode == "hands_off"
                else target
                if isinstance(target, Target)
                else Target.from_dict(target or {})
            )
            fan_command = requested is not None and config["kind"] in {"fan", "relay_fan"}
            normalized = (
                requested
                if requested is None or fan_command
                else self.adapter(resource_id).normalize(requested)
            )
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err
        now = _now()
        if duration is not None:
            duration = _number(duration, "duration")
            if duration <= 0:
                raise ServiceValidationError("duration must be positive")
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
            raise ServiceValidationError("request_id must contain 1 to 128 characters")

        def fingerprint_for(value):
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
        receipt = {"request_id": request_id, "resource_id": resource_id, "expires_at": expiry}
        replayed = False

        def mutate(state):
            nonlocal replayed
            # Admission can wait behind another write; observe mode or unload
            # may have changed while the request was queued.
            self._admit(resource_id)
            existing = state["requests"].get(request_id)
            if existing:
                expected = fingerprint
                # Older snapshots fingerprinted the normalized target. Keep their
                # receipts usable; new fan receipts identify the submitted command.
                if fan_command and existing.get("fingerprint_kind") != "requested":
                    try:
                        expected = fingerprint_for(self.adapter(resource_id).normalize(requested))
                    except (TypeError, ValueError) as err:
                        raise ServiceValidationError(str(err)) from err
                if existing["fingerprint"] != expected:
                    raise ServiceValidationError("request_id was already used for different intent")
                receipt.update(existing["receipt"])
                replayed = True
                return
            if expiry is not None and expiry <= _now():
                raise ServiceValidationError("expires_at must be in the future")
            accepted = normalized
            settings = None
            if fan_command:
                try:
                    accepted, settings = self._fan_command(resource_id, requested, state)
                except (TypeError, ValueError) as err:
                    raise ServiceValidationError(str(err)) from err
            state["manuals"][resource_id] = {
                "mode": mode,
                "target": accepted.to_dict() if accepted else None,
                "expires_at": expiry,
                "request_id": request_id,
                "source": source,
            }
            if settings is not None:
                state["manuals"][resource_id]["fan_settings"] = settings
            state["requests"][request_id] = {"fingerprint": fingerprint, "receipt": receipt}
            if fan_command:
                state["requests"][request_id]["fingerprint_kind"] = "requested"

        await self._commit(
            mutate,
            lambda state: {
                "action": "request",
                "resource_id": resource_id,
                "manual": state["manuals"].get(resource_id),
                "replayed": replayed,
            },
            context_key=("manual", request_id),
            context=context,
        )
        return {**receipt, "accepted": True}

    def _fan_command(self, resource_id: str, requested: Target, state: dict) -> tuple[Target, dict]:
        """Compose under the intent writer lock, never from uncommitted state.

        Fan protocols send power, speed and direction separately. An off relay
        profile has no direction, so retain selected settings with its lease;
        those settings are intent only and never become observed feedback.
        """
        adapter = self.adapter(resource_id)
        if not requested.to_dict():
            raise ValueError("Fan target must not be empty")
        default = adapter.normalize(Target.from_dict(self.resources[resource_id]["default_target"]))
        prior = state["manuals"].get(resource_id)
        base = None
        if (
            prior
            and prior["mode"] == "target"
            and (prior["expires_at"] is None or prior["expires_at"] > _now())
        ):
            base = Target.from_dict({**prior["target"], **prior.get("fan_settings", {})})
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
        command = requested.to_dict()
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
        settings = {
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
        await self._commit(
            lambda state: state["manuals"].pop(resource_id, None),
            lambda state: {"action": "release", "resource_id": resource_id},
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
            raise ServiceValidationError("Resource is in observe mode; no STOP dispatched")
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

        def mutate(state):
            state["manuals"][resource_id] = {
                "mode": "hands_off",
                "expires_at": expiry,
                "request_id": stop_id,
                "source": "stop",
            }

        try:
            await self._commit(
                mutate,
                lambda state: {
                    "action": "stop",
                    "resource_id": resource_id,
                    "manual": state["manuals"][resource_id],
                },
            )
            emergency = self._emergency_hands_off.get(resource_id)
            if emergency is not None and emergency.request_id == stop_id:
                self._emergency_hands_off.pop(resource_id)
        finally:
            # A transport already in progress can complete after the immediate
            # STOP. Drain it, then stop again before acknowledging this request.
            # A newer explicit lease supersedes this barrier as well as intent.
            if had_inflight_command and self.adapter(resource_id).supports_stop:

                async def finish_stop():
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
            raise ServiceValidationError("mode must be observe or live")

        def mutate(state):
            if mode == "live" and self.shadow_locked:
                raise ServiceValidationError("Shadow lock prohibits live control")
            state["modes"][resource_id] = mode

        await self._commit(
            mutate,
            lambda state: {
                "action": "set_mode",
                "resource_id": resource_id,
                "mode": mode,
            },
        )

    async def async_set_policy_enabled(self, policy_id: str, enabled: bool) -> None:
        if policy_id not in self.policies or not isinstance(enabled, bool):
            raise ServiceValidationError("Unknown policy or invalid enabled value")

        def mutate(state):
            state["policy_enabled"][policy_id] = enabled
            if not enabled:
                for occurrence in state["occurrences"].values():
                    if occurrence["policy_id"] == policy_id:
                        occurrence["skipped"] = True

        await self._commit(
            mutate,
            lambda state: {
                "action": "set_policy_enabled",
                "policy_id": policy_id,
                "enabled": enabled,
            },
        )

    def _occurrence(self, policy_id, occurrence_id, expires_at):
        config = self.policies.get(policy_id)
        if not config or config["kind"] != "occurrence":
            raise ServiceValidationError("Unknown occurrence policy")
        self._resource(config["resource_id"])
        if not isinstance(occurrence_id, str) or not 1 <= len(occurrence_id) <= 128:
            raise ServiceValidationError("occurrence_id must contain 1 to 128 characters")
        expiry = _number(expires_at, "expires_at")
        if expiry <= _now():
            raise ServiceValidationError("expires_at must be in the future")
        return config, json.dumps([policy_id, occurrence_id]), expiry

    async def async_submit_occurrence(
        self, policy_id, occurrence_id, expires_at, *, context: Context | None = None
    ) -> dict:
        config, key, expiry = self._occurrence(policy_id, occurrence_id, expires_at)
        self._admit(config["resource_id"])
        target = self._policy_target(config)
        if target is None:
            raise ServiceValidationError("Occurrence target is unavailable")

        replayed = False

        def mutate(state):
            nonlocal replayed
            self._admit(config["resource_id"])
            replayed = key in state["occurrences"]
            if key not in state["occurrences"]:
                state["occurrences"][key] = {
                    "policy_id": policy_id,
                    "occurrence_id": occurrence_id,
                    "expires_at": expiry,
                    "target": target.to_dict(),
                    "skipped": not self.policy_enabled(policy_id) or not self._eligible(config),
                }

        await self._commit(
            mutate,
            lambda state: {
                "action": "submit_occurrence",
                "policy_id": policy_id,
                "occurrence": state["occurrences"][key],
                "replayed": replayed,
            },
            context_key=("occurrence", policy_id, occurrence_id),
            context=context,
        )
        return dict(self._state["occurrences"][key])

    async def async_skip_occurrence(self, policy_id, occurrence_id, expires_at) -> None:
        _, key, expiry = self._occurrence(policy_id, occurrence_id, expires_at)

        def mutate(state):
            item = state["occurrences"].setdefault(
                key,
                {
                    "policy_id": policy_id,
                    "occurrence_id": occurrence_id,
                    "expires_at": expiry,
                },
            )
            item["skipped"] = True

        await self._commit(
            mutate,
            lambda state: {
                "action": "skip_occurrence",
                "policy_id": policy_id,
                "occurrence": state["occurrences"][key],
            },
        )

    async def async_reconcile(self, resource_id: str | None = None) -> None:
        if resource_id is not None:
            self._resource(resource_id)
        self._recompute()
        self._wake_all()
        self._notify()

    def _state_value(self, entity_id: str, attribute: str | None = None):
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

    def _eligible(self, config: dict) -> bool:
        entity_id = config.get("eligibility_entity")
        return not entity_id or self._state_value(entity_id) == config.get(
            "eligibility_state", "on"
        )

    def _policy_target(self, config: dict) -> Target | None:
        target = dict(config.get("target", {}))
        if intent_id := config.get("intent_id"):
            value = self.desired_value(intent_id)
            return Target(on=value) if value is not None else None
        if config.get("target_entity"):
            value = self._state_value(config["target_entity"], config.get("target_attribute"))
            if value is None:
                return None
            field = config.get("target_field", "position")
            if field in {"position", "percentage"}:
                if isinstance(value, bool):
                    return None
                try:
                    value = float(value)
                except ValueError, TypeError:
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

    def _requirement(self, key: str, config: dict) -> Requirement:
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
            config.get("retry_interval", 300),
        )

    @callback
    def _input_changed(self, event) -> None:
        if self._closed:
            return
        if self._trace.enabled and hasattr(event, "data"):
            entity_id = event.data.get("entity_id")
            self._trace.input(
                entity_id,
                event.data.get("new_state"),
                event.event_type,
                event.time_fired.timestamp(),
                event.context,
                event.data.get("old_last_reported"),
                event.data.get("last_reported"),
            )
            if entity_id not in self._decision_input_entities:
                return
        if not hasattr(event, "data"):
            # Absolute lease/occurrence and provider deadlines need no telemetry.
            self._queue_reconciliation(set(self.resources))
            return
        entity_id = event.data["entity_id"]
        affected = self._input_resources.get(entity_id, set())
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
        self._queue_reconciliation(affected)

    @callback
    def _queue_reconciliation(self, resource_ids: set[str]) -> None:
        if self._closed:
            return
        self._pending_resources.update(resource_ids)
        if self._input_flush is None:
            # One HA-tracked task per pending batch, never one task per input.
            # Non-eager scheduling lets same-turn changes join the batch.
            self._input_flush = self.hass.async_create_task(
                self._async_flush_inputs(), "ha_operator:inputs", eager_start=False
            )

    async def _async_flush_inputs(self) -> None:
        self._input_flush = None
        affected, self._pending_resources = self._pending_resources, set()
        if self._closed:
            return
        previous = self.decisions
        self._recompute()
        # Time may have crossed another resource's deadline in this loop turn.
        affected.update(key for key, value in self.decisions.items() if previous.get(key) != value)
        self._wake_resources(affected)
        self._notify()

    @callback
    def _external_command(self, event) -> None:
        data = event.data
        entity_id = data.get("entity_id")
        if entity_id not in self._trace.sanitizer.attrs or data.get("service") not in {
            "open_cover",
            "close_cover",
            "stop_cover",
            "set_cover_position",
            "turn_on",
            "turn_off",
            "set_percentage",
            "set_direction",
        }:
            return
        self._trace.event(
            "external_command",
            {
                "source": "homekit",
                "entity_id": entity_id,
                "service": data["service"],
                "value": data.get("value"),
                "context_id": event.context.id,
                "evidence": "bridge_request",
                "at": event.time_fired.timestamp(),
            },
        )

    @callback
    def _wake_all(self) -> None:
        self._wake_resources(self.resources)

    @callback
    def _wake_resources(self, resource_ids) -> None:
        for identifier in resource_ids:
            if self.decisions[identifier].status in {"pending", "waiting"}:
                self._wake[identifier].set()

    def _recompute(self) -> None:
        if self._closed:
            return
        now = _now()
        resources = {}
        for key, config in self.resources.items():
            observation = self.adapter(key).read_observation(now)
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
            self._storage_fault(err)
            manuals, occurrences = [], []
            resources = {key: replace(item, fault=self.fault) for key, item in resources.items()}
        engine = dict(
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
                    self._eligible(config),
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
        trace_engine = None
        if self._trace.enabled:
            trace_engine = {
                "now": now,
                **{
                    key: {identifier: asdict(value) for identifier, value in engine[key].items()}
                    for key in ("resources", "observations", "requirement_memory")
                },
                **{
                    key: [asdict(value) for value in engine[key]]
                    for key in ("manuals", "policies", "occurrences", "requirements")
                },
            }
        previous = self.decisions
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
            policy = self.policies.get(selection.source_id, {})
            if intent_id := policy.get("intent_id"):
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
            old = previous.get(key)
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
                self.history.append(
                    {
                        "at": now,
                        "resource_id": key,
                        "status": decision.status,
                        "source": decision.source,
                        "target": decision.target.to_dict() if decision.target else None,
                    }
                )
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
            old = old_requirements.get(key)
            notification_id = f"ha_operator_{self.entry.entry_id}_{key}"
            if item.status in unconfirmed and (old is None or old.status not in unconfirmed):
                persistent_notification.async_create(
                    self.hass,
                    f"{self.requirements[key]['name']}: incoming air is not confirmed. "
                    "Eligible alternatives will be tried. "
                    "Extraction remains under its existing control.",
                    title="HA Operator: airflow requirement",
                    notification_id=notification_id,
                )
            elif old and old.status in unconfirmed and item.status not in unconfirmed:
                persistent_notification.async_dismiss(self.hass, notification_id)
        if self._deadline_timer:
            self._deadline_timer()
            self._deadline_timer = None
        self._next_evaluation = result.next_evaluation
        if result.next_evaluation is not None:
            self._deadline_timer = async_call_later(
                self.hass, max(0.01, result.next_evaluation - now), self._input_changed
            )

        if trace_engine is not None:
            self._trace.event(
                "decision",
                {
                    **self.explain(),
                    "engine": trace_engine,
                    "engine_result": {
                        "decisions": {
                            key: asdict(value) for key, value in result.decisions.items()
                        },
                        "requirements": {
                            key: asdict(value) for key, value in result.requirements.items()
                        },
                        "next_evaluation": result.next_evaluation,
                    },
                },
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

    def selection_attributes(self, resource_id: str) -> dict:
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

    def _active_inputs(self, entity_ids) -> tuple[tuple[str, str], ...]:
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
            members = state.attributes.get("entity_id")
            if isinstance(members, (list, tuple)) and members:
                pending.extend(item for item in members[:32] if isinstance(item, str))
            else:
                active.append(
                    (entity_id, str(state.attributes.get("friendly_name", entity_id))[:80])
                )
        return tuple(active)

    @callback
    def _activation_input_changed(self, event) -> None:
        """Group membership can change cause while the group's aggregate stays on."""
        entity_id = event.data["entity_id"]
        if self._closed or entity_id in self._observed_entities:
            return
        pending = [
            item for config in self.requirements.values() for item in config["activation_entities"]
        ]
        seen = set()
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
        def wake(_):
            self._timers.pop(resource_id, None)
            self._wake[resource_id].set()

        self._timers[resource_id] = async_call_later(self.hass, max(0.01, when - _now()), wake)

    async def _worker(self, resource_id: str) -> None:
        wake = self._wake[resource_id]
        config = self.resources[resource_id]
        while not self._closed:
            await wake.wait()
            wake.clear()
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

            def still_current(generation=generation, target=target):
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

    def _explain_requirements(self) -> dict:
        """Capture bounded predicates in the same synchronous evaluation turn."""
        requirements = {}
        for key, item in self.requirement_results.items():
            config = self.requirements[key]
            providers = {}
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

    def explain(self, resource_id: str | None = None) -> dict:
        if resource_id is not None:
            self._resource(resource_id)
        ids = [resource_id] if resource_id is not None else list(self.resources)
        return {
            "revision": self._state["revision"],
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
