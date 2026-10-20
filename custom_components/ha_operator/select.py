"""Explicit control activation, initially observe-only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .adapters import SelectAdapter
from .const import DOMAIN
from .entity import OperatorEntity, ResourceEntity

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

# The runtime updates state on the HA loop and owns one worker per resource and its outputs.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for identifier, config in entry.runtime_data.resources.items():
        if config["kind"] == "select" and entry.runtime_data.manual_control(identifier):
            async_add_entities(
                [OperatorSelect(entry.runtime_data, identifier)], config_subentry_id=identifier
            )
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


class OperatorSelect(ResourceEntity, SelectEntity):
    """Native options and observed selection, with ordinary manual ownership."""

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed")

    @property
    def options(self) -> list[str]:
        adapter = self.runtime.adapter(self.identifier)
        assert isinstance(adapter, SelectAdapter)
        return adapter.options

    @property
    def current_option(self) -> str | None:
        return self.observed_target.option if self.observed_target else None

    async def async_select_option(self, option: str) -> None:
        await self.runtime.async_request(
            self.identifier, target={"option": option}, source="managed", context=self._context
        )
