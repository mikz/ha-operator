"""Native intent, control, expiry and confirmation observations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import OperatorEntity

RESOURCE_SENSORS = {
    "desired": "Desired target",
    "status": "Control status",
    "expiry": "Manual expiry",
    "next_attempt": "Next attempt",
    "attempts": "Command attempts",
}
REQUIREMENT_SENSORS = {"status": "Requirement status", "provider": "Selected provider"}


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    runtime = entry.runtime_data
    for identifier in runtime.resources:
        async_add_entities(
            [ResourceSensor(runtime, identifier, key) for key in RESOURCE_SENSORS],
            config_subentry_id=identifier,
        )
    for identifier in runtime.requirements:
        async_add_entities(
            [RequirementSensor(runtime, identifier, key) for key in REQUIREMENT_SENSORS],
            config_subentry_id=identifier,
        )


def target_value(target: Any) -> float | str | None:
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

    def __init__(self, runtime: Any, identifier: str, key: str) -> None:
        super().__init__(runtime, identifier, key, RESOURCE_SENSORS[key])
        self.key = key
        if key in {"expiry", "next_attempt"}:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if key in {"next_attempt", "attempts"}:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> float | str | datetime | None:
        if self.key == "expiry":
            lease = self.runtime.manual(self.identifier)
            stamp = lease.expires_at if lease else None
            return datetime.fromtimestamp(stamp, UTC) if stamp is not None else None
        if self.key == "next_attempt":
            stamp = self.runtime.next_attempts.get(self.identifier)
            return datetime.fromtimestamp(stamp, UTC) if stamp is not None else None
        if self.key == "attempts":
            return self.runtime.attempts.get(self.identifier, 0)
        decision = self.runtime.decisions.get(self.identifier)
        if self.key == "status":
            if self.runtime.fault:
                return "fault"
            return decision.status if decision else "initializing"
        return target_value(decision.target) if decision else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.key == "desired":
            decision = self.runtime.decisions.get(self.identifier)
            return {"target": decision.target.to_dict() if decision and decision.target else None}
        if self.key == "status":
            decision = self.runtime.decisions.get(self.identifier)
            return {"reason": decision.reason, "source": decision.source} if decision else None
        return None


class RequirementSensor(OperatorEntity, SensorEntity):
    """Confirmation status and selected source, without optimistic satisfaction."""

    def __init__(self, runtime: Any, identifier: str, key: str) -> None:
        super().__init__(runtime, identifier, key, REQUIREMENT_SENSORS[key])
        self.key = key

    @property
    def native_value(self) -> str | None:
        result = self.runtime.requirement_results.get(self.identifier)
        if result is None:
            return None
        return result.status if self.key == "status" else result.selected_provider

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        result = self.runtime.requirement_results.get(self.identifier)
        if self.key == "status" and result is not None:
            return {"reason": result.reason, "acquiring_provider": result.acquiring_provider}
        return None
