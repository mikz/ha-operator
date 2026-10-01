"""Observed native covers; commands enter the request boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import ATTR_POSITION, CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .entity import ResourceEntity

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

# The runtime updates state on the HA loop and owns one worker per resource and its outputs.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for identifier, data in runtime.resources.items():
        if data["kind"] == "cover" and runtime.manual_control(identifier):
            async_add_entities([OperatorCover(runtime, identifier)], config_subentry_id=identifier)


class OperatorCover(ResourceEntity, CoverEntity):
    """Cover reporting raw position rather than accepted intent."""

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed")

    @property
    def supported_features(self) -> CoverEntityFeature:
        return CoverEntityFeature(self.runtime.adapter(self.identifier).supported_features) & (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
            | CoverEntityFeature.STOP
        )

    @property
    def current_cover_position(self) -> int | None:
        target = self.observed_target
        return round(target.position) if target and target.position is not None else None

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        return position == 0 if position is not None else None

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier, target={"position": 100}, source="entity", context=self._context
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier, target={"position": 0}, source="entity", context=self._context
        )

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier,
            target={"position": kwargs[ATTR_POSITION]},
            source="entity",
            context=self._context,
        )

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.runtime.async_stop(self.identifier, context=self._context)
