"""Native intent, control, expiry and confirmation observations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .core import Target
from .entity import OperatorEntity

RESOURCE_SENSORS = (
    "desired",
    "observed",
    "reason",
    "status",
    "expiry",
    "effective_expiry",
    "next_attempt",
    "attempts",
)
REQUIREMENT_SENSORS = ("status", "provider")


if TYPE_CHECKING:
    from .runtime import OperatorRuntime

# The runtime updates state on the HA loop and owns one worker per resource and its outputs.
PARALLEL_UPDATES = 0


def _timestamp(stamp: float | None) -> datetime | None:
    """Project an accepted numeric deadline only when HA can represent its date."""
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp, UTC)
    except OverflowError, ValueError, OSError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for identifier in runtime.resources:
        async_add_entities(
            [
                ResourceSensor(runtime, identifier, key)
                for key in RESOURCE_SENSORS
                if key != "expiry" or runtime.manual_control(identifier)
            ],
            config_subentry_id=identifier,
        )
    for identifier in runtime.requirements:
        async_add_entities(
            [RequirementSensor(runtime, identifier, key) for key in REQUIREMENT_SENSORS],
            config_subentry_id=identifier,
        )
    for identifier, policy in runtime.policies.items():
        if "input" in policy:
            async_add_entities(
                [
                    PolicyInputSensor(runtime, identifier, key)
                    for key in ("input_phase", "qualification_due")
                ],
                config_subentry_id=identifier,
            )


def target_value(target: Target | None) -> float | str | None:
    """Compact scalar state; exact structured desired values remain attributes."""
    if target is None:
        return None
    if target.profile is not None:
        return target.profile
    if target.position is not None:
        return target.position
    if target.on is False:
        return "off"
    if target.percentage is not None:
        return target.percentage
    if target.on is True:
        return "on"
    return target.direction


class ResourceSensor(OperatorEntity, SensorEntity):
    """Resource state keeps desired values distinct from observed entities."""

    def __init__(self, runtime: OperatorRuntime, identifier: str, key: str) -> None:
        super().__init__(runtime, identifier, key)
        self.key = key
        if key == "status":
            self._attr_translation_key = "control_status"
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [
                "initializing",
                "fault",
                "hands_off",
                "idle",
                "observe",
                "unavailable",
                "restricted",
                "satisfied",
                "pending",
                "applying",
                "waiting",
            ]
        if key in {"desired", "observed"} and runtime.resources[identifier]["kind"] == "cover":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_suggested_display_precision = 0
            if key == "observed":
                self._attr_state_class = SensorStateClass.MEASUREMENT
        if key in {"expiry", "effective_expiry", "next_attempt"}:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if key in {"next_attempt", "attempts"}:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self) -> bool:
        if self.key == "observed":
            return self.runtime.source_available(self.identifier)
        return True

    @property
    def native_value(self) -> float | str | datetime | None:
        if self.key == "observed":
            if self.runtime._closed:
                return None
            observation = self.runtime.observations.get(self.identifier)
            return (
                target_value(observation.target) if observation and observation.available else None
            )
        if self.key == "reason":
            selection = self.runtime.selections.get(self.identifier)
            return selection.selection_reason if selection else None
        if self.key == "expiry":
            lease = self.runtime.manual(self.identifier)
            stamp = lease.expires_at if lease else None
            return _timestamp(stamp)
        if self.key == "effective_expiry":
            selection = self.runtime.selections.get(self.identifier)
            stamp = selection.expires_at if selection else None
            return _timestamp(stamp)
        if self.key == "next_attempt":
            stamp = self.runtime.next_attempts.get(self.identifier)
            return _timestamp(stamp)
        if self.key == "attempts":
            return self.runtime.attempts.get(self.identifier, 0)
        decision = self.runtime.decisions.get(self.identifier)
        if self.key == "status":
            if self.runtime.fault:
                return "fault"
            return decision.status if decision else "initializing"
        return target_value(decision.target) if decision else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        if self.key in {"desired", "reason"}:
            decision = self.runtime.decisions.get(self.identifier)
            selection = self.runtime.selections.get(self.identifier)
            return {
                "target": decision.target.to_dict() if decision and decision.target else None,
                **(self.runtime.selection_attributes(self.identifier) if selection else {}),
                **self.linked_entities,
            }
        if self.key == "observed":
            observation = self.runtime.observations.get(self.identifier)
            target = (
                observation.target
                if not self.runtime._closed and observation and observation.available
                else None
            )
            return {"target": target.to_dict() if target else None, **self.linked_entities}
        if self.key == "status":
            if self.runtime.fault:
                return {"reason": self.runtime.fault, "execution_reason": self.runtime.fault}
            decision = self.runtime.decisions.get(self.identifier)
            return (
                {
                    "reason": decision.reason,
                    "execution_reason": decision.reason,
                    "source": decision.source,
                }
                if decision
                else None
            )
        return None


class PolicyInputSensor(OperatorEntity, SensorEntity):
    """Read current input phase and deadline; execution never depends on this entity."""

    def __init__(self, runtime: OperatorRuntime, identifier: str, key: str) -> None:
        super().__init__(runtime, identifier, key)
        self.key = key
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = False
        if key == "input_phase":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = (
                ["idle", "qualifying", "qualified", "recovering"]
                if runtime.policies[identifier]["input"]["type"] == "qualified_numeric"
                else ["idle", "qualifying", "accepted", "suppressed", "expired"]
            )
        else:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> str | datetime | None:
        state = self.runtime.policy_input(self.identifier)
        if state is None:
            return None
        if self.key == "input_phase":
            return state.phase
        return _timestamp(state.due_at)


class RequirementSensor(OperatorEntity, SensorEntity):
    """Confirmation status and selected source, without optimistic satisfaction."""

    def __init__(self, runtime: OperatorRuntime, identifier: str, key: str) -> None:
        super().__init__(runtime, identifier, key)
        self.key = key
        if key == "status":
            self._attr_translation_key = "requirement_status"
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["inactive", "unknown", "satisfied", "acquiring", "unmet"]

    @property
    def native_value(self) -> str | None:
        result = self.runtime.requirement_results.get(self.identifier)
        if result is None:
            return None
        return result.status if self.key == "status" else result.selected_provider

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        result = self.runtime.requirement_results.get(self.identifier)
        if self.key == "status" and result is not None:
            return {"reason": result.reason, "acquiring_provider": result.acquiring_provider}
        return None
