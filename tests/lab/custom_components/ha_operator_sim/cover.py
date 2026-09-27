"""Native raw cover capability and independently reported physical motion."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import ATTR_POSITION, CoverEntity, CoverEntityFeature

from . import DOMAIN
from .entity import SimulatorEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    coordinator = hass.data[DOMAIN]
    async_add_entities(
        [
            SimulatorCover(coordinator, device)
            for device in coordinator.data.values()
            if device["kind"] == "cover"
        ]
    )


class SimulatorCover(SimulatorEntity, CoverEntity):
    """Position is read from raw feedback, never echoed from the command."""

    def __init__(self, coordinator, device):
        super().__init__(coordinator, device)
        self._attr_supported_features = (
            CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION
        )
        if device.get("supports_stop", True):
            self._attr_supported_features |= CoverEntityFeature.STOP

    @property
    def current_cover_position(self) -> int | None:
        position = self.observation.get("position")
        return round(position) if position is not None else None

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        return position == 0 if position is not None else None

    @property
    def is_opening(self) -> bool:
        return self.observation.get("motion") == "opening"

    @property
    def is_closing(self) -> bool:
        return self.observation.get("motion") == "closing"

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.device_id, "open")

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.device_id, "close")

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self.coordinator.command(
            self.device_id, "set_position", position=kwargs[ATTR_POSITION]
        )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.device_id, "stop")
