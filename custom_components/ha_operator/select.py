"""Explicit control activation, initially observe-only."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory

from .entity import OperatorEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    for identifier in entry.runtime_data.resources:
        async_add_entities(
            [ModeSelect(entry.runtime_data, identifier)], config_subentry_id=identifier
        )


class ModeSelect(OperatorEntity, SelectEntity):
    """Observe/live control that persists before publishing its selection."""

    _attr_options = ["observe", "live"]
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "mode", "Control mode")

    @property
    def current_option(self) -> str:
        return self.runtime.mode(self.identifier)

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError("Choose observe or live")
        await self.runtime.async_set_mode(self.identifier, option)
