"""Manual ownership and requirement confirmation indicators."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import OperatorEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    runtime = entry.runtime_data
    for identifier in runtime.resources:
        async_add_entities([ManualSensor(runtime, identifier)], config_subentry_id=identifier)
    for identifier in runtime.requirements:
        async_add_entities([UnmetSensor(runtime, identifier)], config_subentry_id=identifier)


class ManualSensor(OperatorEntity, BinarySensorEntity):
    """Telemetry does not create or extend a manual lease."""

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "manual", "Manual active")

    @property
    def is_on(self) -> bool:
        return self.runtime.manual(self.identifier) is not None


class UnmetSensor(OperatorEntity, BinarySensorEntity):
    """Unknown evidence remains unknown instead of asserting adequate airflow."""

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "unmet", "Airflow unmet")

    @property
    def is_on(self) -> bool | None:
        result = self.runtime.requirement_results.get(self.identifier)
        if result is None or result.status == "unknown":
            return None
        return result.status in {"unmet", "acquiring"}
