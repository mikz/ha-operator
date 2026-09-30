"""Strict, serialized persistence of accepted intent.

The snapshot records intent, never an actuator queue or proof of physical state.
A failed atomic write has an uncertain outcome, including when replacement already
occurred. Such a store cannot accept another mutation until a fresh instance loads
and validates the file.
"""

from __future__ import annotations

import asyncio
import json
import math
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.json import save_json

from .async_utils import async_settle as _settle
from .policy_inputs import NumericState, TimerState

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

_VERSION = 3
_MAP_KEYS = (
    "manuals",
    "occurrences",
    "modes",
    "policy_enabled",
    "requests",
    "intents",
    "policy_inputs",
    "return_monitors",
)


class IntentStoreError(RuntimeError):
    """Intent cannot safely be read or accepted by this store instance."""


class InvalidSnapshot(ValueError):
    """A snapshot does not conform to the supported on-disk schema."""


def _empty_state() -> dict[str, Any]:
    return {"revision": 0, **{key: {} for key in _MAP_KEYS}}


def _validate_json(value: Any) -> None:
    """Reject values whose representation would change or fail in JSON."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise InvalidSnapshot("Snapshot values must be finite JSON values with string keys")


def _validate_state(state: Any) -> None:
    if not isinstance(state, dict):
        raise InvalidSnapshot("Snapshot data must be an object")
    if type(state.get("revision")) is not int or state["revision"] < 0:
        raise InvalidSnapshot("Snapshot revision must be a non-negative integer")
    if any(not isinstance(state.get(key), dict) for key in _MAP_KEYS):
        raise InvalidSnapshot("Snapshot is missing required intent maps")
    if any(value not in ("observe", "live") for value in state["modes"].values()):
        raise InvalidSnapshot("Resource mode must be observe or live")
    if any(type(value) is not bool for value in state["policy_enabled"].values()):
        raise InvalidSnapshot("Policy enablement must be boolean")
    if any(type(value) is not bool for value in state["intents"].values()):
        raise InvalidSnapshot("Desired control values must be boolean")
    if state["return_monitors"]:
        raise InvalidSnapshot("Return monitor records are not supported yet")
    for item in state["policy_inputs"].values():
        if not isinstance(item, dict) or set(item) != {"type", "fingerprint", "state"}:
            raise InvalidSnapshot("Invalid policy input record")
        fingerprint = item["fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)
        ):
            raise InvalidSnapshot("Invalid policy input fingerprint")
        if not isinstance(item["type"], str):
            raise InvalidSnapshot("Invalid policy input type")
        model = {"qualified_numeric": NumericState, "timer_episode": TimerState}.get(item["type"])
        if model is None or not isinstance(item["state"], dict):
            raise InvalidSnapshot("Invalid policy input type or state")
        try:
            model.from_record(item["state"])
        except (ValueError, TypeError) as err:
            raise InvalidSnapshot("Invalid policy input state") from err
    try:
        _validate_json(state)
    except RecursionError as err:
        raise InvalidSnapshot("Snapshot must not contain recursive values") from err


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSnapshot("Snapshot contains duplicate object keys")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise InvalidSnapshot(f"Snapshot contains non-finite JSON constant: {value}")


def _read_snapshot(path: Path, expected_existing: bool) -> dict[str, Any]:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if expected_existing:
            raise InvalidSnapshot("Previously initialized intent snapshot is missing") from None
        return _empty_state()
    envelope = json.loads(
        contents, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
    )
    if not isinstance(envelope, dict):
        raise InvalidSnapshot("Snapshot envelope must be an object")
    if type(envelope.get("version")) is not int or envelope["version"] not in (1, 2, _VERSION):
        raise InvalidSnapshot("Unsupported intent snapshot version")
    state = envelope.get("data")
    if envelope["version"] == 1 and isinstance(state, dict):
        state = {**state, "intents": {}}
    if envelope["version"] in (1, 2) and isinstance(state, dict):
        state = {**state, "policy_inputs": {}, "return_monitors": {}}
    _validate_state(state)
    return state


def _write_snapshot(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    save_json(str(path), {"version": _VERSION, "data": state}, private=True, atomic_writes=True)


class IntentStore:
    """Persist immutable committed snapshots before exposing accepted intent."""

    def __init__(self, hass: HomeAssistant, path: Path) -> None:
        self._hass = hass
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._state = _empty_state()
        self._loaded = False
        self._closed = False
        self.fault: str | None = None

    @property
    def state(self) -> dict[str, Any]:
        """Return a detached view; callers must mutate through async_update."""
        return deepcopy(self._state)

    def _check_usable(self) -> None:
        if self.fault is not None:
            raise IntentStoreError(self.fault)
        if self._closed:
            raise IntentStoreError("Intent store is closed")

    async def async_load(self, expected_existing: bool = False) -> None:
        """Load once, preserving bad files and inhibiting uncertain state."""
        self._check_usable()
        async with self._lock:
            self._check_usable()
            if self._loaded:
                return
            try:
                future = self._hass.async_add_executor_job(
                    _read_snapshot, self._path, expected_existing
                )
                state, cancelled = await _settle(future)
            except Exception as err:
                self.fault = f"Intent snapshot load failed: {type(err).__name__}"
                raise IntentStoreError(self.fault) from err
            self._state = state
            self._loaded = True
            if cancelled:
                raise asyncio.CancelledError

    async def async_update(
        self, mutator: Callable[[dict[str, Any]], None], *, skip_unchanged: bool = False
    ) -> dict[str, Any]:
        """Save a mutation of the current revision before publishing it.

        Caller cancellation cannot undo a committed update. After commit this
        method publishes the new state, then propagates cancellation; consumers
        must re-evaluate current state even when their request was cancelled.
        """
        self._check_usable()
        async with self._lock:
            self._check_usable()
            if not self._loaded:
                raise IntentStoreError("Intent snapshot has not been loaded")
            candidate = deepcopy(self._state)
            mutator(candidate)
            if skip_unchanged and candidate == self._state:
                return self.state
            candidate["revision"] = self._state["revision"] + 1
            _validate_state(candidate)
            # A mutator may retain its argument. Detach it before executor access.
            candidate = deepcopy(candidate)
            try:
                future = self._hass.async_add_executor_job(
                    partial(_write_snapshot, self._path, candidate)
                )
                _, cancelled = await _settle(future)
            except Exception as err:
                self.fault = f"Intent snapshot save failed: {type(err).__name__}"
                raise IntentStoreError(self.fault) from err
            self._state = candidate
            if cancelled:
                raise asyncio.CancelledError
            return self.state

    async def async_close(self) -> None:
        """Reject new work and drain the active executor before returning."""
        self._closed = True

        # Use a separate task so cancellation of close cannot release the drain.
        async def drain() -> None:
            async with self._lock:
                pass

        _, cancelled = await _settle(asyncio.create_task(drain()))
        if cancelled:
            raise asyncio.CancelledError
