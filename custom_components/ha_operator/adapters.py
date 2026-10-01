"""Capability-aware actuator boundaries; service success is never physical evidence."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import TypeGuard, cast

from homeassistant.components.cover import CoverEntityFeature
from homeassistant.components.fan import FanEntityFeature
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .async_utils import async_settle
from .const import DOMAIN
from .core import Observation, Target, TargetValidationError
from .data import ResourceConfig

DISPATCH_PARENT: ContextVar[Context | None] = ContextVar(
    "ha_operator_dispatch_parent", default=None
)

_LOGGER = logging.getLogger(__name__)
_FEATURES = "supported_features"
_UNKNOWN = {STATE_UNAVAILABLE, STATE_UNKNOWN}
_DIRECTIONS = {"forward", "reverse"}


def _number(value: object, label: str, maximum: float = 100) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetValidationError("numeric_target", f"{label} must be numeric", field=label)
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise TargetValidationError(
            "target_range",
            f"{label} must be between 0 and {maximum}",
            field=label,
            maximum=str(maximum),
        )
    return float(value)


def _usable(state: State | None) -> TypeGuard[State]:
    return bool(
        state is not None
        and state.state not in _UNKNOWN
        and not state.attributes.get("assumed_state", False)
        and not state.attributes.get("optimistic", False)
        and not state.attributes.get("restored", False)
    )


class Adapter:
    """Read feedback directly, dispatch only while the caller's generation is current."""

    def __init__(self, hass: HomeAssistant, resource_id: str, config: ResourceConfig) -> None:
        self.hass = hass
        self.resource_id = resource_id
        self.config = config
        self.entity_id: str = config.get("entity_id", "")
        self._source_entities = tuple(config.get("outputs", (self.entity_id,)))

    @property
    def supported_features(self) -> int:
        return 0

    @property
    def supports_stop(self) -> bool:
        return False

    def _state(self) -> State | None:
        return self.hass.states.get(self.entity_id)

    @property
    def source_available(self) -> bool:
        """Source presence is separate from usable, independently confirmed feedback."""
        return all(
            (state := self.hass.states.get(entity_id)) is not None
            and state.state != STATE_UNAVAILABLE
            for entity_id in self._source_entities
        )

    def _restriction(self) -> str | None:
        for entity_id, label in (
            (self.config.get("fault_entity"), "fault"),
            (self.config.get("restriction_entity"), "restricted"),
        ):
            if entity_id:
                state = self.hass.states.get(entity_id)
                if not _usable(state):
                    return f"{label}_unknown"
                if state.state == "on":
                    return label
        return None

    def read_observation(self, now: float) -> Observation:
        raise NotImplementedError

    def normalize(self, target: Target) -> Target:
        raise NotImplementedError

    async def async_apply(self, target: Target, still_current: Callable[[], bool]) -> bool:
        raise NotImplementedError

    async def async_stop(self) -> None:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="stop_unsupported")

    async def _call(
        self, domain: str, service: str, data: dict[str, object], current: Callable[[], bool]
    ) -> bool:
        # Re-read restrictions immediately before every individual physical command.
        if not current():
            return False
        if self._restriction():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="device_restricted")
        await self._async_service(domain, service, data)
        # The command was sent even if synchronous feedback satisfied the target
        # or a newer generation arrived while the service was running.
        return True

    async def _async_service(self, domain: str, service: str, data: dict[str, object]) -> None:
        """Drain physical I/O on cancellation before an old worker can disappear.

        HA services may await executor threads, whose physical writes cannot be
        cancelled. Shield the complete service invocation, then settle it before
        propagating cancellation so reload never overlaps an abandoned actuator.
        """
        parent = DISPATCH_PARENT.get()
        context = Context(parent_id=parent.id if parent else None)
        task = self.hass.async_create_task(
            self.hass.services.async_call(domain, service, data, blocking=True, context=context),
            f"ha_operator:physical:{self.resource_id}:{service}",
        )
        _, cancelled = await async_settle(task)
        if cancelled:
            raise asyncio.CancelledError

    def _validate_fields(self, target: Target, allowed: set[str]) -> None:
        extra = target.to_dict().keys() - allowed
        if extra:
            raise TargetValidationError(
                "unsupported_target_fields",
                f"Unsupported target fields: {', '.join(sorted(extra))}",
                fields=", ".join(sorted(extra)),
            )

    def _native_observation(self, target: Target | None, now: float) -> Observation:
        state = self._state()
        return Observation(
            target=target,
            available=_usable(state),
            moving=bool(state and state.state in {"opening", "closing"}),
            restriction=self._restriction(),
            reported_at=state.last_reported.timestamp() if state else now,
        )


class CoverAdapter(Adapter):
    """Percentage-position covers with honest native STOP support."""

    @property
    def supported_features(self) -> int:
        state = self._state()
        raw = int(state.attributes.get(_FEATURES, 0)) if state else 0
        result = raw & CoverEntityFeature.STOP
        if raw & CoverEntityFeature.SET_POSITION:
            result |= (
                CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
            )
        return int(result)

    @property
    def supports_stop(self) -> bool:
        return bool(self.supported_features & CoverEntityFeature.STOP)

    def read_observation(self, now: float) -> Observation:
        state = self._state()
        position = state.attributes.get("current_position") if _usable(state) else None
        if isinstance(position, (int, float)) and not isinstance(position, bool):
            if 0 <= position <= 100 and math.isfinite(position):
                return self._native_observation(Target(position=float(position)), now)
        return self._native_observation(None, now)

    def normalize(self, target: Target) -> Target:
        self._validate_fields(target, {"position"})
        if not self.supported_features & CoverEntityFeature.SET_POSITION:
            raise TargetValidationError(
                "cover_position_unsupported", "Cover does not support percentage position"
            )
        return Target(position=_number(target.position, "position"))

    async def async_apply(self, target: Target, still_current: Callable[[], bool]) -> bool:
        target = self.normalize(target)
        return await self._call(
            "cover",
            "set_cover_position",
            {"entity_id": self.entity_id, "position": target.position},
            still_current,
        )

    async def async_stop(self) -> None:
        if not self.supports_stop:
            await super().async_stop()
        # STOP intentionally bypasses movement restrictions and normal pacing.
        await self._async_service("cover", "stop_cover", {"entity_id": self.entity_id})


class SwitchAdapter(Adapter):
    """An observed on/off switch, without interpreting successful calls as state."""

    def read_observation(self, now: float) -> Observation:
        state = self._state()
        target = (
            Target(on=state.state == "on")
            if _usable(state) and state.state in {"on", "off"}
            else None
        )
        return self._native_observation(target, now)

    def normalize(self, target: Target) -> Target:
        self._validate_fields(target, {"on"})
        if not isinstance(target.on, bool):
            raise TargetValidationError(
                "switch_boolean_required", "Switch target requires a boolean on value"
            )
        return target

    async def async_apply(self, target: Target, still_current: Callable[[], bool]) -> bool:
        target = self.normalize(target)
        return await self._call(
            "switch",
            "turn_on" if target.on else "turn_off",
            {"entity_id": self.entity_id},
            still_current,
        )


class FanAdapter(Adapter):
    """A native fan, with speed quantization matching Home Assistant's helpers."""

    @property
    def supported_features(self) -> int:
        state = self._state()
        raw = int(state.attributes.get(_FEATURES, 0)) if state else 0
        return raw & int(
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.DIRECTION
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )

    def _speed_count(self) -> int:
        state = self._state()
        step = state.attributes.get("percentage_step", 1) if state else 1
        if not isinstance(step, (float, int)) or isinstance(step, bool) or not 0 < step <= 100:
            raise TargetValidationError(
                "invalid_percentage_step", "Fan reports invalid percentage_step"
            )
        return max(1, round(100 / step))

    @property
    def speed_count(self) -> int:
        """Number of native physical speeds; malformed feedback exposes a safe minimum."""
        if not self.supported_features & FanEntityFeature.SET_SPEED:
            return 1
        try:
            return self._speed_count()
        except ValueError:
            return 1

    def _percentage(self, percentage: object) -> int:
        percentage = _number(percentage, "percentage")
        if percentage == 0:
            return 0
        speeds = list(range(1, self._speed_count() + 1))
        # HA's ordered helper rounds upward into the device's discrete speed bins.
        selected = percentage_to_ordered_list_item(speeds, math.ceil(percentage))
        return ordered_list_item_to_percentage(speeds, selected)

    def read_observation(self, now: float) -> Observation:
        state = self._state()
        if not _usable(state) or state.state not in {"on", "off"}:
            return self._native_observation(None, now)
        percentage = state.attributes.get("percentage")
        if (
            not isinstance(percentage, (int, float))
            or isinstance(percentage, bool)
            or not 0 <= percentage <= 100
            or not math.isfinite(percentage)
        ):
            percentage = None
        direction = state.attributes.get("direction")
        return self._native_observation(
            Target(
                on=state.state == "on",
                percentage=percentage,
                direction=direction if direction in _DIRECTIONS else None,
            ),
            now,
        )

    def normalize(self, target: Target) -> Target:
        self._validate_fields(target, {"on", "percentage", "direction"})
        if not target.to_dict():
            raise TargetValidationError("empty_fan_target", "Fan target must not be empty")
        features = self.supported_features
        if target.percentage is not None and not features & FanEntityFeature.SET_SPEED:
            raise TargetValidationError(
                "fan_speed_unsupported", "Fan does not support percentage speed"
            )
        if target.direction is not None:
            if target.direction not in _DIRECTIONS or not features & FanEntityFeature.DIRECTION:
                raise TargetValidationError(
                    "fan_direction_unsupported", "Fan does not support this direction"
                )
        observed = self.read_observation(0).target
        on = target.on
        if target.percentage is not None:
            percentage = self._percentage(target.percentage)
            if on is False and percentage > 0 or on is True and percentage == 0:
                raise TargetValidationError(
                    "fan_power_conflict", "Fan on and percentage targets conflict"
                )
            on = percentage > 0
        else:
            percentage = None
        if on is None:
            if observed is None or observed.on is None:
                raise TargetValidationError(
                    "direction_requires_feedback",
                    "Direction-only command requires known on/off feedback",
                )
            on = observed.on
        if on and not features & (FanEntityFeature.TURN_ON | FanEntityFeature.SET_SPEED):
            raise TargetValidationError("fan_on_unsupported", "Fan does not support turning on")
        if not on and not features & (FanEntityFeature.TURN_OFF | FanEntityFeature.SET_SPEED):
            raise TargetValidationError("fan_off_unsupported", "Fan does not support turning off")
        if on and features & FanEntityFeature.SET_SPEED and percentage is None:
            default = self.config.get("default_target", {})
            value = (
                observed.percentage if target.on is None and observed else default.get("percentage")
            )
            if value is None and observed:
                value = observed.percentage
            if not value:
                raise TargetValidationError(
                    "fan_default_speed_required", "Bare turn_on requires a configured default speed"
                )
            percentage = self._percentage(value)
        if not on:
            percentage = None
        direction = target.direction
        if target.on is True and target.percentage is None and direction is None:
            direction = self.config.get("default_target", {}).get("direction")
            if direction is not None and (
                direction not in _DIRECTIONS or not features & FanEntityFeature.DIRECTION
            ):
                raise TargetValidationError(
                    "fan_default_direction_unsupported", "Fan default direction is unsupported"
                )
        return Target(on=on, percentage=percentage, direction=direction)

    async def async_apply(self, target: Target, still_current: Callable[[], bool]) -> bool:
        target = self.normalize(target)
        if target.direction is not None:
            if not await self._call(
                "fan",
                "set_direction",
                {"entity_id": self.entity_id, "direction": target.direction},
                still_current,
            ):
                return False
        feature = FanEntityFeature.TURN_ON if target.on else FanEntityFeature.TURN_OFF
        if not self.supported_features & feature:
            return await self._call(
                "fan",
                "set_percentage",
                {"entity_id": self.entity_id, "percentage": target.percentage if target.on else 0},
                still_current,
            )
        service = "turn_on" if target.on else "turn_off"
        data: dict[str, object] = {"entity_id": self.entity_id}
        if target.on and target.percentage is not None:
            data["percentage"] = target.percentage
        return await self._call("fan", service, data, still_current)


class RelayFanAdapter(Adapter):
    """One finite relay bundle, with confirmed break-before-make transitions."""

    def __init__(self, hass: HomeAssistant, resource_id: str, config: ResourceConfig) -> None:
        super().__init__(hass, resource_id, config)
        self.outputs = tuple(config["outputs"])
        if not self.outputs or len(set(self.outputs)) != len(self.outputs):
            raise ValueError("Relay outputs must be nonempty and unique")
        self.profiles = config["profiles"]
        if not isinstance(self.profiles, Mapping) or not self.profiles:
            raise ValueError("Relay fan requires profiles")
        off = []
        signatures = set()
        for name, profile in self.profiles.items():
            outputs = profile["outputs"]
            if set(outputs) != set(self.outputs) or any(
                type(v) is not bool for v in outputs.values()
            ):
                raise ValueError("Every profile must specify all relay outputs as booleans")
            signature = tuple(outputs[output] for output in self.outputs)
            if signature in signatures:
                raise ValueError("Relay profiles must have distinct output combinations")
            signatures.add(signature)
            percentage = _number(
                profile.get("percentage", 100 if any(outputs.values()) else 0), "percentage"
            )
            if any(outputs.values()) != (percentage > 0):
                raise ValueError("Relay profile speed must agree with its on/off outputs")
            if profile.get("direction") is not None and profile["direction"] not in _DIRECTIONS:
                raise ValueError("Relay direction must be forward or reverse")
            if not any(outputs.values()):
                off.append(name)
        if len(off) != 1:
            raise ValueError("Relay fan requires exactly one all-off profile")
        self.off_profile = off[0]
        self.dead_time = _number(config["reversal_dead_time"], "reversal_dead_time", 3600)
        if self.dead_time == 0:
            raise ValueError("reversal_dead_time must be positive")
        self.timeout = _number(config.get("movement_timeout", 120), "movement_timeout", 86400)
        default_target = config.get("default_target", {})
        default = (
            default_target if isinstance(default_target, str) else default_target.get("profile")
        )
        if default not in self.profiles or default == self.off_profile:
            raise ValueError("Relay default_target must name an on profile")
        self.default_profile: str = default

    def _target(self, name: str) -> Target:
        profile = self.profiles[name]
        on = any(profile["outputs"].values())
        return Target(
            on=on,
            percentage=float(profile.get("percentage", 100 if on else 0)),
            direction=profile.get("direction") if on else None,
            profile=name,
        )

    @property
    def supported_features(self) -> int:
        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF | FanEntityFeature.SET_SPEED
        directions = {p.get("direction") for p in self.profiles.values()} - {None}
        if len(directions) > 1:
            features |= FanEntityFeature.DIRECTION
        return int(features)

    def _feedback(self) -> dict[str, bool] | None:
        result = {}
        for output in self.outputs:
            state = self.hass.states.get(output)
            if not _usable(state) or state.state not in {"on", "off"}:
                return None
            result[output] = state.state == "on"
        return result

    def read_observation(self, now: float) -> Observation:
        feedback = self._feedback()
        matches = [
            name for name, profile in self.profiles.items() if profile["outputs"] == feedback
        ]
        states = [self.hass.states.get(output) for output in self.outputs]
        return Observation(
            target=self._target(matches[0]) if len(matches) == 1 else None,
            available=feedback is not None,
            moving=False,
            restriction=self._restriction(),
            reported_at=min(
                (state.last_reported.timestamp() for state in states if state), default=now
            ),
        )

    def normalize(self, target: Target) -> Target:
        self._validate_fields(target, {"on", "percentage", "direction", "profile"})
        if not target.to_dict():
            raise TargetValidationError("empty_fan_target", "Fan target must not be empty")
        if target.direction is not None and target.direction not in _DIRECTIONS:
            raise TargetValidationError(
                "relay_direction_invalid", "Relay direction must be forward or reverse"
            )
        percentage = None if target.percentage is None else _number(target.percentage, "percentage")
        if target.profile is not None:
            if target.profile not in self.profiles:
                raise TargetValidationError("unknown_relay_profile", "Unknown relay profile")
            result = self._target(target.profile)
            for field in ("on", "percentage", "direction"):
                value = getattr(target, field)
                if value is not None and value != getattr(result, field):
                    raise TargetValidationError(
                        "relay_profile_conflict", "Relay profile conflicts with other target fields"
                    )
            return result
        if target.on is False or percentage == 0:
            if target.on is True or percentage is not None and percentage > 0:
                raise TargetValidationError(
                    "fan_power_conflict", "Fan on and percentage targets conflict"
                )
            return self._target(self.off_profile)
        if target.on is True and percentage is None and target.direction is None:
            return self._target(self.default_profile)
        observed = self.read_observation(0).target
        if target.on is None and percentage is None:
            if observed is None:
                raise TargetValidationError(
                    "direction_requires_feedback",
                    "Direction-only command requires known on/off feedback",
                )
            if observed.on is False:
                return self._target(self.off_profile)
        default = self._target(self.default_profile)
        direction = (
            target.direction
            or (observed.direction if observed and observed.on else None)
            or default.direction
        )
        candidates = [
            name
            for name in self.profiles
            if name != self.off_profile
            and (direction is None or self._target(name).direction == direction)
        ]
        if not candidates:
            raise TargetValidationError(
                "relay_direction_unsupported", "No relay profile supports this direction"
            )
        candidates.sort(key=lambda name: (self._target(name).percentage, name))
        if percentage is None:
            value = (
                default.percentage
                if target.on is True
                else (observed.percentage if observed else default.percentage)
            )
        else:
            value = percentage
        # HA maps requests onto a finite ordered set, consistently with native fans.
        name = percentage_to_ordered_list_item(candidates, math.ceil(value))
        return self._target(name)

    async def _wait_all_off(self, current: Callable[[], bool]) -> bool:
        changed = asyncio.Event()

        @callback
        def on_change(event: Event[EventStateChangedData]) -> None:
            changed.set()

        unsubscribe = async_track_state_change_event(self.hass, self.outputs, on_change)
        try:
            async with asyncio.timeout(self.timeout):
                while current():
                    changed.clear()
                    if self._feedback() == dict.fromkeys(self.outputs, False):
                        return True
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=0.1)
                    except TimeoutError:
                        pass
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="relay_interlock_timeout"
            ) from err
        finally:
            unsubscribe()
        return False

    async def async_apply(self, target: Target, still_current: Callable[[], bool]) -> bool:
        target = self.normalize(target)
        # Every successful relay normalization returns _target(configured_name).
        profile = cast(str, target.profile)
        expected = self.profiles[profile]["outputs"]
        if not still_current():
            return False
        if self._feedback() == expected:
            return False
        # Always command the entire bundle off. Never infer this from a service response.
        for output in self.outputs:
            if not await self._call("switch", "turn_off", {"entity_id": output}, still_current):
                return False
        if not target.on:
            return True
        if not await self._wait_all_off(still_current):
            return False
        deadline = asyncio.get_running_loop().time() + self.dead_time
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            if not still_current():
                return False
            await asyncio.sleep(min(remaining, 0.1))
        if not still_current():
            return False
        if self._feedback() != dict.fromkeys(self.outputs, False):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="relay_feedback_changed"
            )
        for output, on in expected.items():
            if on:
                feedback = self._feedback()
                if feedback is None or any(
                    feedback[item] for item in self.outputs if not expected[item]
                ):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="relay_conflict"
                    )
                if not await self._call("switch", "turn_on", {"entity_id": output}, still_current):
                    return False
        return True


def create_adapter(hass: HomeAssistant, resource_id: str, config: ResourceConfig) -> Adapter:
    """Create an adapter without claiming any service result is observed feedback."""
    classes = {
        "cover": CoverAdapter,
        "switch": SwitchAdapter,
        "fan": FanAdapter,
        "relay_fan": RelayFanAdapter,
    }
    try:
        cls = classes[config["kind"]]
    except KeyError as err:
        raise ValueError("Unsupported resource kind") from err
    return cls(hass, resource_id, config)
