"""Raw light reports simulator feedback, independently of service receipts."""

from homeassistant.components.light import ColorMode, LightEntity, LightEntityFeature

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities(
        [
            SimulatorLight(hass.data[DOMAIN], device)
            for device in hass.data[DOMAIN].data.values()
            if device["kind"] == "light"
        ]
    )


class SimulatorLight(SimulatorEntity, LightEntity):
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.HS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 6500
    _attr_effect_list = ["none", "pulse"]

    @property
    def is_on(self):
        return self.observation.get("on")

    @property
    def brightness(self):
        return self.observation.get("brightness")

    @property
    def color_mode(self):
        return self.observation.get("color_mode")

    @property
    def color_temp_kelvin(self):
        return self.observation.get("color_temp_kelvin")

    @property
    def hs_color(self):
        return self.observation.get("hs_color")

    @property
    def effect(self):
        return self.observation.get("effect")

    async def async_turn_on(self, **kwargs):
        await self.coordinator.command(self.device_id, "turn_on", **kwargs)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.command(self.device_id, "turn_off")
