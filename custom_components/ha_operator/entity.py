"""Shared native entity lifecycle and observed-state access."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
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
        self._last_publication: tuple | None = None
        data = (
            runtime.resources.get(identifier)
            or runtime.policies.get(identifier)
            or runtime.requirements.get(identifier)
            or runtime.intents.get(identifier)
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
        # HA publishes the initial state when adding the entity to its platform.
        self._last_publication = deepcopy(self._publication_state())
        self.async_on_remove(self.runtime.subscribe(self._runtime_updated))
        self.async_on_remove(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_updated)
        )

    @callback
    def _registry_updated(self, event) -> None:
        """Resolve links again after a rename without waking actuator workers."""
        self._runtime_updated()

    def _publication_state(self) -> tuple:
        """Compare public entity values; HA still owns their rendering/validation."""
        available = self.available
        return (
            available,
            self.state if available else None,
            self.state_attributes if available else None,
            self.extra_state_attributes if available else None,
            self.capability_attributes,
            self.supported_features,
            self.assumed_state,
            self.unit_of_measurement,
            self.device_class,
            self.icon,
            self.entity_picture,
            self.name,
        )

    @callback
    def _runtime_updated(self) -> None:
        publication = self._publication_state()
        if publication != self._last_publication:
            self.async_write_ha_state()
            # Retain a detached snapshot only after a change. Copying every
            # unchanged entity on every update dominated the sampled burst path.
            self._last_publication = deepcopy(publication)

    @property
    def linked_entities(self) -> dict[str, str]:
        if self.hass is None:
            return {}
        registry = er.async_get(self.hass)
        kind = self.runtime.resources[self.identifier]["kind"]
        managed_domain = "fan" if kind == "relay_fan" else kind
        return {
            f"{key}_entity": entity_id
            for key in ("managed", "desired", "observed", "reason", "status")
            if (
                entity_id := registry.async_get_entity_id(
                    managed_domain if key == "managed" else "sensor",
                    DOMAIN,
                    f"{self.identifier}_{key}",
                )
            )
            is not None
        }


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
