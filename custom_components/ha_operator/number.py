"""A managed number exposes independent observation and submits ordinary requests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .adapters import NumberAdapter
from .entity import ResourceEntity

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for identifier, config in runtime.resources.items():
        if config["kind"] == "number" and runtime.manual_control(identifier):
            async_add_entities([OperatorNumber(runtime, identifier)], config_subentry_id=identifier)


class OperatorNumber(ResourceEntity, NumberEntity):
    """State is feedback; bounds/step/unit are native actor metadata."""

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.observed_target is not None
            and self._adapter.metadata is not None
        )

    @property
    def native_value(self) -> float | None:
        return self.observed_target.value if self.observed_target else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        state = self.hass.states.get(self.runtime.resources[self.identifier]["entity_id"])
        unit = state.attributes.get("unit_of_measurement") if state else None
        return unit if isinstance(unit, str) else None

    @property
    def _adapter(self) -> NumberAdapter:
        adapter = self.runtime.adapter(self.identifier)
        assert isinstance(adapter, NumberAdapter)
        return adapter

    @property
    def capability_attributes(self) -> dict[str, Any]:
        if self._adapter.metadata is None:
            return {}
        return super().capability_attributes

    def _metadata(self, index: int) -> float:
        metadata = self._adapter.metadata
        # NumberEntity requires float properties; invalid controls expose no capabilities.
        return metadata[index] if metadata is not None else 0.0

    @property
    def native_min_value(self) -> float:
        return self._metadata(0)

    @property
    def native_max_value(self) -> float:
        return self._metadata(1)

    @property
    def native_step(self) -> float:
        return self._metadata(2)

    async def async_set_native_value(self, value: float) -> None:
        await self.runtime.async_request(
            self.identifier, target={"value": value}, source="managed", context=self._context
        )
