"""Native fan facade over observed fan or finite relay profiles."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.exceptions import ServiceValidationError

from .entity import ResourceEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    runtime = entry.runtime_data
    for identifier, data in runtime.resources.items():
        if data["kind"] in {"fan", "relay_fan"}:
            async_add_entities([OperatorFan(runtime, identifier)], config_subentry_id=identifier)


class OperatorFan(ResourceEntity, FanEntity):
    """Fan commands never optimistically replace feedback."""

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed", None)

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
            raise ServiceValidationError("Preset modes are not supported")
        target: dict[str, Any] = {"on": True}
        if percentage is not None:
            target.update(percentage=percentage, on=percentage > 0)
        await self.runtime.async_request(self.identifier, target=target, source="entity")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_request(self.identifier, target={"on": False}, source="entity")

    async def async_set_percentage(self, percentage: int) -> None:
        await self.runtime.async_request(
            self.identifier,
            target={"percentage": percentage, "on": percentage > 0},
            source="entity",
        )

    async def async_set_direction(self, direction: str) -> None:
        # Adapter normalization preserves observed off state for direction-only intent.
        await self.runtime.async_request(
            self.identifier, target={"direction": direction}, source="entity"
        )
