"""Independent measured-flow telemetry."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            SimulatorSensor(coordinator, device)
            for device in coordinator.data.values()
            if device["kind"] == "sensor"
        ]
    )


class SimulatorSensor(SimulatorEntity, SensorEntity):
    def __init__(self, coordinator, device):
        super().__init__(coordinator, device)
        self._attr_native_unit_of_measurement = device.get("unit")

    @property
    def native_value(self) -> float | None:
        return self.observation.get("value")
