"""Shared native entity lifecycle and observed-state access."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, NAME


class OperatorEntity(Entity):
    """An event-driven entity belonging to a stable configuration subentry."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: Any, identifier: str, key: str, name: str | None) -> None:
        self.runtime = runtime
        self.identifier = identifier
        self._attr_unique_id = f"{identifier}_{key}"
        self._attr_name = name
        data = (
            runtime.resources.get(identifier)
            or runtime.policies.get(identifier)
            or runtime.requirements.get(identifier)
            or {}
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=data.get("name", identifier),
            manufacturer=NAME,
            model=data.get("kind", "Requirement"),
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.subscribe(self.async_write_ha_state))


class ResourceEntity(OperatorEntity):
    """A managed device whose state is exclusively physical observation."""

    @property
    def observation(self) -> Any:
        return self.runtime.observations.get(self.identifier)

    @property
    def observed_target(self) -> Any:
        observation = self.observation
        return observation.target if observation and observation.available else None

    @property
    def available(self) -> bool:
        observation = self.observation
        return observation is not None and observation.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"control_mode": self.runtime.mode(self.identifier)}
