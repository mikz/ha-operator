"""Explicit control activation, initially observe-only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .const import DOMAIN
from .entity import OperatorEntity

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

# The runtime updates state on the HA loop and owns one worker per resource and its outputs.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for identifier in entry.runtime_data.resources:
        async_add_entities(
            [ModeSelect(entry.runtime_data, identifier)], config_subentry_id=identifier
        )


class ModeSelect(OperatorEntity, SelectEntity):
    """Observe/live control backed by current runtime state."""

    _attr_options = ["observe", "live"]
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "mode")

    @property
    def current_option(self) -> str:
        return self.runtime.mode(self.identifier)

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_select_option"
            )
        await self.runtime.async_set_mode(self.identifier, option)
