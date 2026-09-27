"""Scenario-controlled raw contact and activation signals."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            SimulatorBinarySensor(coordinator, device)
            for device in coordinator.data.values()
            if device["kind"] == "binary_sensor"
        ]
    )


class SimulatorBinarySensor(SimulatorEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        value = self.observation.get("value")
        return None if value is None else bool(value)
