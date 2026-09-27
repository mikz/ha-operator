"""HA event boundary, durable admission, and one actuator worker per resource."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from homeassistant.components import persistent_notification
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.util import dt as dt_util

from .adapters import create_adapter
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


class OperatorRuntime:
    """Recompute current intent; never enqueue historical target commands."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass, self.entry = hass, entry
        configured = validate_configuration(entry.subentries)
        self.resources = configured["resources"]
        self.policies = configured["policies"]
        self.requirements = configured["requirements"]
        self._shadow_lock_latched = bool(entry.options.get("shadow_lock", False))
        self._trace = ShadowTrace(hass, entry, configured, entry.options.get("trace_entities", []))
        self.store = IntentStore(hass, Path(hass.config.path(f"ha_operator.{entry.entry_id}.json")))
        self.observations: dict[str, Any] = {}
        self.decisions: dict[str, Decision] = {}
        self.requirement_results: dict[str, Any] = {}
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
        self._decision_input_entities = self._input_entities()
        self._observed_entities = self._decision_input_entities | (
            self._trace.sanitizer.attrs.keys() if self._trace.enabled else set()
        )

    def _input_entities(self) -> set[str]:
        ids: set[str] = set()
        for config in self.resources.values():
            ids.update(config.get("outputs", []))
            for key in ("entity_id", "restriction_entity", "fault_entity"):
                if config.get(key):
                    ids.add(config[key])
        for config in self.policies.values():
            for key in ("eligibility_entity", "target_entity"):
                if config.get(key):
                    ids.add(config[key])
        for config in self.requirements.values():
            ids.update(config["activation_entities"])
            for provider in config["providers"]:
                ids.update(item["entity_id"] for item in provider["evidence"])
        return ids

    async def async_start(self) -> None:
        try:
            await self.store.async_load(expected_existing=self.entry.data.get("initialized", False))
            _validate_saved_state(self.store.state)
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
                    if item.get("expires_at") is None or item["expires_at"] > now
                }

            if any(
                item.get("expires_at") is not None and item["expires_at"] <= now
                for item in self.store.state["manuals"].values()
            ):
                await self.store.async_update(prune)
        except (IntentStoreError, ValueError, TypeError, KeyError) as err:
            self.store.fault = self.store.fault or "Saved intent records are invalid"
            self._storage_fault(err)
        else:
            self._state = self.store.state
        await self._trace.async_start(self._state, self.shadow_locked)
        self._unsubscribers.append(self.hass.async_add_shutdown_job(HassJob(self.async_close)))
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

    async def _commit(self, mutator: Callable[[dict], None], admission=None) -> dict:
        if self.fault or self.store.fault:
            raise HomeAssistantError("HA Operator storage is inhibited; inspect Repairs")
        candidate = None

        def own_mutation(state):
            nonlocal candidate
            mutator(state)
            candidate = state

        try:
            return await self.store.async_update(own_mutation)
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
                    self._trace.event(
                        "admission", {**admission(candidate), "revision": candidate["revision"]}
                    )
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
    ) -> dict:
        config = self._admit(resource_id)
        if mode not in {"target", "hands_off"}:
            raise ServiceValidationError("mode must be target or hands_off")
        if sum((duration is not None, expires_at is not None, indefinite)) > 1:
            raise ServiceValidationError("Choose duration, expires_at, or indefinite")
        if mode == "hands_off" and target is not None:
            raise ServiceValidationError("hands_off cannot carry a target")
        try:
            normalized = (
                None
                if mode == "hands_off"
                else self.adapter(resource_id).normalize(
                    target if isinstance(target, Target) else Target.from_dict(target or {})
                )
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
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "resource_id": resource_id,
                    "mode": mode,
                    "target": normalized.to_dict() if normalized else None,
                    "duration": actual_duration,
                    "expires_at": expires_at,
                    "indefinite": indefinite,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        receipt = {"request_id": request_id, "resource_id": resource_id, "expires_at": expiry}
        replayed = False

        def mutate(state):
            nonlocal replayed
            # Admission can wait behind another write; observe mode or unload
            # may have changed while the request was queued.
            self._admit(resource_id)
            existing = state["requests"].get(request_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ServiceValidationError("request_id was already used for different intent")
                receipt.update(existing["receipt"])
                replayed = True
                return
            if expiry is not None and expiry <= _now():
                raise ServiceValidationError("expires_at must be in the future")
            state["manuals"][resource_id] = {
                "mode": mode,
                "target": normalized.to_dict() if normalized else None,
                "expires_at": expiry,
                "request_id": request_id,
                "source": source,
            }
            state["requests"][request_id] = {"fingerprint": fingerprint, "receipt": receipt}

        await self._commit(
            mutate,
            lambda state: {
                "action": "request",
                "resource_id": resource_id,
                "manual": state["manuals"].get(resource_id),
                "replayed": replayed,
            },
        )
        return {**receipt, "accepted": True}

    async def async_release(self, resource_id: str) -> None:
        self._resource(resource_id)
        await self._commit(
            lambda state: state["manuals"].pop(resource_id, None),
            lambda state: {"action": "release", "resource_id": resource_id},
        )
        self._emergency_hands_off.pop(resource_id, None)
        self._recompute()
        self._wake_all()

    async def async_stop(self, resource_id: str) -> None:
        task = self.hass.async_create_task(
            self._async_stop(resource_id), f"ha_operator:stop_request:{resource_id}"
        )
        self._stop_tasks.add(task)
        try:
            _, cancelled = await async_settle(task)
            if cancelled:
                raise asyncio.CancelledError
        finally:
            self._stop_tasks.discard(task)

    async def _async_stop(self, resource_id: str) -> None:
        config = self._resource(resource_id)
        if self.mode(resource_id) != "live":
            raise ServiceValidationError("Resource is in observe mode; no STOP dispatched")
        self._generation[resource_id] += 1
        stop_id = str(uuid4())
        expiry = _now() + config["manual_duration"]
        self._emergency_hands_off[resource_id] = ManualLease(
            resource_id, "hands_off", expires_at=expiry, request_id=stop_id, source="stop"
        )
        self._recompute()
        self._notify()
        actuator_lock = self._actuator_locks[resource_id]
        had_inflight_command = actuator_lock.locked()
        stop_error = None
        try:
            await self.adapter(resource_id).async_stop()
        except HomeAssistantError as err:
            stop_error = err

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
                            await self.adapter(resource_id).async_stop()
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

    async def async_submit_occurrence(self, policy_id, occurrence_id, expires_at) -> dict:
        config, key, expiry = self._occurrence(policy_id, occurrence_id, expires_at)
        self._admit(config["resource_id"])
        target = self._policy_target(config)
        if target is None:
            raise ServiceValidationError("Occurrence target is unavailable")

        def mutate(state):
            self._admit(config["resource_id"])
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
            },
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
        self._recompute()
        self._wake_all()
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
        for event in self._wake.values():
            event.set()

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
        self.decisions = dict(result.decisions)
        for key, decision in self.decisions.items():
            signature = (
                decision.target,
                decision.source,
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
            if old is None or (old.status, old.target, old.source) != (
                decision.status,
                decision.target,
                decision.source,
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
                if (
                    old is not None
                    and old.status != decision.status
                    and decision.status not in {"applying", "waiting", "pending"}
                ):
                    self.hass.bus.async_fire(
                        "logbook_entry",
                        {
                            "name": self.resources[key]["name"],
                            "message": decision.status,
                            "domain": DOMAIN,
                        },
                    )
        old_requirements = self.requirement_results
        self.requirement_results = dict(result.requirements)
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

    def explain(self, resource_id: str | None = None) -> dict:
        if resource_id is not None:
            self._resource(resource_id)
        ids = [resource_id] if resource_id is not None else list(self.resources)
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
        return {
            "revision": self._state["revision"],
            "fault": self.fault,
            "shadow_locked": self.shadow_locked,
            "resources": {
                key: {
                    "mode": self.mode(key),
                    "decision": asdict(self.decisions[key]) if key in self.decisions else None,
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
            "requirements": requirements,
        }
