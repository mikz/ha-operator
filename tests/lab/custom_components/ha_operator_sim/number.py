"""Raw numeric actuator with independently polled feedback."""

from homeassistant.components.number import NumberEntity

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            SimulatorNumber(coordinator, item)
            for item in coordinator.data.values()
            if item["kind"] == "number"
        ]
    )


class SimulatorNumber(SimulatorEntity, NumberEntity):
    @property
    def native_value(self):
        return self.observation.get("value")

    @property
    def native_min_value(self):
        return self.coordinator.data[self.device_id]["min"]

    @property
    def native_max_value(self):
        return self.coordinator.data[self.device_id]["max"]

    @property
    def native_step(self):
        return self.coordinator.data[self.device_id]["step"]

    @property
    def native_unit_of_measurement(self):
        return self.coordinator.data[self.device_id].get("unit")

    async def async_set_native_value(self, value):
        await self.coordinator.command(self.device_id, "set_value", value=value)
