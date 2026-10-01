"""Native fan facade over observed fan or finite relay profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .const import DOMAIN
from .entity import ResourceEntity

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

# The runtime serializes durable admission and owns one worker per resource, owning all its outputs.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for identifier, data in runtime.resources.items():
        if data["kind"] in {"fan", "relay_fan"} and runtime.manual_control(identifier):
            async_add_entities([OperatorFan(runtime, identifier)], config_subentry_id=identifier)


class OperatorFan(ResourceEntity, FanEntity):
    """Fan commands never optimistically replace feedback."""

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed")

    @property
    def supported_features(self) -> FanEntityFeature:
        return FanEntityFeature(self.runtime.adapter(self.identifier).supported_features) & (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.DIRECTION
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )

    @property
    def is_on(self) -> bool | None:
        target = self.observed_target
        return target.on if target else None

    @property
    def percentage(self) -> int | None:
        target = self.observed_target
        return round(target.percentage) if target and target.percentage is not None else None

    @property
    def current_direction(self) -> str | None:
        target = self.observed_target
        return target.direction if target else None

    @property
    def speed_count(self) -> int:
        data = self.runtime.resources[self.identifier]
        if data["kind"] == "relay_fan":
            return max(
                1,
                len(
                    {
                        profile["percentage"]
                        for profile in data["profiles"].values()
                        if profile.get("percentage", 0) > 0
                    }
                ),
            )
        return int(getattr(self.runtime.adapter(self.identifier), "speed_count", 100))

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        if preset_mode is not None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="preset_unsupported"
            )
        target: dict[str, object] = {"on": True}
        if percentage is not None:
            target.update(percentage=percentage, on=percentage > 0)
        await self.runtime.async_request(
            self.identifier, target=target, source="entity", context=self._context
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier, target={"on": False}, source="entity", context=self._context
        )

    async def async_set_percentage(self, percentage: int) -> None:
        await self.runtime.async_request(
            self.identifier,
            target={"percentage": percentage, "on": percentage > 0},
            source="entity",
            context=self._context,
        )

    async def async_set_direction(self, direction: str) -> None:
        # Admission composes against accepted intent; feedback remains observational.
        await self.runtime.async_request(
            self.identifier, target={"direction": direction}, source="entity", context=self._context
        )
