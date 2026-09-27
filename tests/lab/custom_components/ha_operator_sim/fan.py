"""Native raw fan with finite speeds and direction state while off."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            SimulatorFan(coordinator, device)
            for device in coordinator.data.values()
            if device["kind"] == "fan"
        ]
    )


class SimulatorFan(SimulatorEntity, FanEntity):
    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.SET_SPEED
        | FanEntityFeature.DIRECTION
    )

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device)
        self._attr_speed_count = device.get("speed_count", 3)
        self._attr_percentage_step = 100 / self._attr_speed_count

    @property
    def is_on(self) -> bool | None:
        return self.observation.get("on")

    @property
    def percentage(self) -> int | None:
        percentage = self.observation.get("percentage")
        return round(percentage) if percentage is not None else None

    @property
    def current_direction(self) -> str | None:
        return self.observation.get("direction")

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self.coordinator.command(
            self.device_id,
            "turn_on",
            percentage=100 if percentage is None else percentage,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.device_id, "turn_off")

    async def async_set_percentage(self, percentage: int) -> None:
        await self.coordinator.command(self.device_id, "set_percentage", percentage=percentage)

    async def async_set_direction(self, direction: str) -> None:
        await self.coordinator.command(self.device_id, "set_direction", direction=direction)
