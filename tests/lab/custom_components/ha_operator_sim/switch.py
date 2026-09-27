"""Raw relay channels with independent physical feedback."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            SimulatorSwitch(coordinator, device)
            for device in coordinator.data.values()
            if device["kind"] == "switch"
        ]
    )


class SimulatorSwitch(SimulatorEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        return self.observation.get("on")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.device_id, "turn_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.device_id, "turn_off")
