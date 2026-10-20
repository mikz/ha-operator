"""Observed native light facade; all user intent enters the manual request boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import ColorMode, LightEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .adapters import LIGHT_FIELDS
from .entity import ResourceEntity

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for identifier, data in entry.runtime_data.resources.items():
        if data["kind"] == "light" and entry.runtime_data.manual_control(identifier):
            async_add_entities(
                [OperatorLight(entry.runtime_data, identifier)], config_subentry_id=identifier
            )


class OperatorLight(ResourceEntity, LightEntity):
    """Publish independently observed light state and native color capabilities."""

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed")

    def _attribute(self, key: str) -> Any:
        state = self.hass.states.get(self.runtime.resources[self.identifier]["entity_id"])
        return state.attributes.get(key) if state else None

    @property
    def supported_features(self) -> LightEntityFeature:
        return LightEntityFeature(self.runtime.adapter(self.identifier).supported_features)

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {ColorMode(mode) for mode in self._attribute("supported_color_modes") or []}

    @property
    def capability_attributes(self) -> dict[str, Any]:
        if not self.supported_color_modes:
            return {}
        return super().capability_attributes

    @property
    def color_mode(self) -> ColorMode | None:
        value = self._attribute("color_mode")
        return ColorMode(value) if value else None

    @property
    def is_on(self) -> bool | None:
        return self.observed_target.on if self.observed_target else None

    @property
    def brightness(self) -> int | None:
        value = self.observed_target.brightness if self.observed_target else None
        return round(value) if value is not None else None

    @property
    def color_temp_kelvin(self) -> int | None:
        value = self.observed_target.color_temp_kelvin if self.observed_target else None
        return round(value) if value is not None else None

    @property
    def min_color_temp_kelvin(self) -> int:
        return self._attribute("min_color_temp_kelvin") or 2000

    @property
    def max_color_temp_kelvin(self) -> int:
        return self._attribute("max_color_temp_kelvin") or 6535

    @property
    def hs_color(self) -> tuple[float, float] | None:
        return self.observed_target.hs_color if self.observed_target else None

    @property
    def xy_color(self) -> tuple[float, float] | None:
        return self.observed_target.xy_color if self.observed_target else None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return cast(tuple[int, int, int] | None, self._attribute("rgb_color"))

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        return cast(tuple[int, int, int, int] | None, self._attribute("rgbw_color"))

    @property
    def rgbww_color(self) -> tuple[int, int, int, int, int] | None:
        return cast(tuple[int, int, int, int, int] | None, self._attribute("rgbww_color"))

    @property
    def effect(self) -> str | None:
        return self.observed_target.effect if self.observed_target else None

    @property
    def effect_list(self) -> list[str] | None:
        return cast(list[str] | None, self._attribute("effect_list"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier,
            target={
                "on": True,
                **{key: value for key, value in kwargs.items() if key in LIGHT_FIELDS},
            },
            source="entity",
            context=self._context,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier,
            target={"on": False},
            source="entity",
            context=self._context,
        )
