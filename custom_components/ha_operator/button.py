"""Explicit ownership release and reconciliation controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .entity import OperatorEntity

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

# The runtime serializes durable admission and owns one worker per resource, owning all its outputs.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OperatorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for identifier in entry.runtime_data.resources:
        async_add_entities(
            [
                OperatorButton(entry.runtime_data, identifier, action)
                for action in ("release", "reconcile")
                if action == "reconcile" or entry.runtime_data.manual_control(identifier)
            ],
            config_subentry_id=identifier,
        )


class OperatorButton(OperatorEntity, ButtonEntity):
    """Actions operate through the runtime so they cannot bypass safety rules."""

    def __init__(self, runtime: OperatorRuntime, identifier: str, action: str) -> None:
        super().__init__(runtime, identifier, action)
        self.action = action

    async def async_press(self) -> None:
        if self.action == "release":
            await self.runtime.async_release(self.identifier)
        else:
            await self.runtime.async_reconcile(self.identifier)
