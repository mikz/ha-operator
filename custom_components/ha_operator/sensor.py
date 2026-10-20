"""Native intent, control, expiry and confirmation observations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import Context, HomeAssistant, callback
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


def target_value(target: Target | None) -> float | str | None:
    """Compact scalar state; exact structured desired values remain attributes."""
    if target is None:
        return None
    if target.value is not None:
        return target.value
    if target.option is not None:
        return target.option
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

    @callback
    def _async_write_ha_state(self) -> None:
        if self.key != "reason":
            super()._async_write_ha_state()
            return
        context = self.runtime.publication_context(self.identifier)
        unique_id = self.unique_id
        if unique_id is not None:
            context = (
                self.runtime.begin_reason_publication(self.identifier, self.entity_id, unique_id)
                or context
            )
        self.async_set_context(context or Context())
        try:
            super()._async_write_ha_state()
        finally:
            self.async_set_context(Context())

    def _write_publication(self) -> None:
        if self.key not in {"desired", "effective_expiry"}:
            super()._write_publication()
            return
        self.async_set_context(self.runtime.publication_context(self.identifier) or Context())
        try:
            super()._write_publication()
        finally:
            # HA otherwise retains ingress attribution for later framework writes.
            self.async_set_context(Context())

    @property
    def native_unit_of_measurement(self) -> str | None:
        config = self.runtime.resources[self.identifier]
        if config["kind"] == "number" and self.key in {"desired", "observed"}:
            if self.key == "desired":
                return config["unit_of_measurement"]
            native = self.hass.states.get(config["entity_id"])
            unit = native.attributes.get("unit_of_measurement") if native else None
            return unit if isinstance(unit, str) else None
        return super().native_unit_of_measurement

    @property
    def available(self) -> bool:
        if self.key == "observed":
            if self.runtime.resources[self.identifier]["kind"] == "number":
                observation = self.runtime.observations.get(self.identifier)
                return (
                    not self.runtime._closed and observation is not None and observation.available
                )
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
            attributes = {
                "target": decision.target.to_dict() if decision and decision.target else None,
                **(self.runtime.selection_attributes(self.identifier) if selection else {}),
                **self.linked_entities,
            }
            if self.runtime.resources[self.identifier]["kind"] == "number":
                config = self.runtime.resources[self.identifier]
                native = self.hass.states.get(config["entity_id"])
                attributes.update(
                    target_unit=config["unit_of_measurement"],
                    observed_unit=native.attributes.get("unit_of_measurement") if native else None,
                )
            if self.key == "reason":
                attributes.update(self.runtime.reason_details(self.identifier))
                if checks := self.runtime.condition_details(self.identifier, published=True):
                    attributes["conditions"] = checks
                config = self.runtime.resources[self.identifier]
                fields = {
                    "number": ("value",),
                    "select": ("option",),
                    "cover": ("position",),
                    "switch": ("on",),
                    "fan": ("on", "percentage", "direction"),
                    "relay_fan": ("profile", "percentage", "direction"),
                }[config["kind"]]
                target_values: dict[str, object] = (
                    dict(decision.target.to_dict()) if decision and decision.target else {}
                )
                if config["kind"] == "relay_fan" and isinstance(
                    profile := target_values.get("profile"), str
                ):
                    configured = config["profiles"].get(profile)
                    if configured:
                        for field in ("percentage", "direction"):
                            target_values.setdefault(field, configured.get(field))
                attributes.update({f"target_{key}": target_values.get(key) for key in fields})
            return attributes
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
