"""Manual ownership and requirement confirmation indicators."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import OperatorEntity
from .policy_inputs import NumericState


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    runtime = entry.runtime_data
    for identifier in runtime.resources:
        entities = []
        if runtime.manual_control(identifier):
            entities.append(ManualSensor(runtime, identifier))
        if "return_monitor" in runtime.resources[identifier]:
            entities.append(ReturnOverdueSensor(runtime, identifier))
        if entities:
            async_add_entities(entities, config_subentry_id=identifier)
    for identifier in runtime.requirements:
        async_add_entities([UnmetSensor(runtime, identifier)], config_subentry_id=identifier)
    for identifier, policy in runtime.policies.items():
        if policy.get("input", {}).get("type") == "qualified_numeric":
            async_add_entities(
                [QualifiedSensor(runtime, identifier)], config_subentry_id=identifier
            )


class QualifiedSensor(OperatorEntity, BinarySensorEntity):
    """Expose qualification without becoming a policy source or scheduling owner."""

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "qualified", "Qualified")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = False

    @property
    def is_on(self) -> bool | None:
        state = self.runtime.policy_input(self.identifier)
        if not isinstance(state, NumericState) or state.recovery_pending:
            return None
        return state.qualified


class ReturnOverdueSensor(OperatorEntity, BinarySensorEntity):
    """An overdue effective return still needs independent raw position feedback."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "return_overdue", "Return overdue")

    @property
    def is_on(self) -> bool:
        return self.runtime.return_monitor(self.identifier).overdue


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
