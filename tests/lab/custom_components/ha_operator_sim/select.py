"""Raw mode actuator with independent feedback and changing options."""

from homeassistant.components.select import SelectEntity

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            SimulatorSelect(coordinator, item)
            for item in coordinator.data.values()
            if item["kind"] == "select"
        ]
    )


class SimulatorSelect(SimulatorEntity, SelectEntity):
    @property
    def current_option(self):
        return self.observation.get("value")

    @property
    def options(self):
        return self.coordinator.data[self.device_id]["options"]

    async def async_select_option(self, option):
        await self.coordinator.command(self.device_id, "select_option", option=option)
