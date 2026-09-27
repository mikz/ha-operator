"""Explicit ownership release and reconciliation controls."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity

from .entity import OperatorEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    for identifier in entry.runtime_data.resources:
        async_add_entities(
            [
                OperatorButton(entry.runtime_data, identifier, action)
                for action in ("release", "reconcile")
            ],
            config_subentry_id=identifier,
        )


class OperatorButton(OperatorEntity, ButtonEntity):
    """Actions operate through the runtime so they cannot bypass safety rules."""

    def __init__(self, runtime: Any, identifier: str, action: str) -> None:
        super().__init__(
            runtime,
            identifier,
            action,
            "Resume automatic" if action == "release" else "Reconcile",
        )
        self.action = action

    async def async_press(self) -> None:
        if self.action == "release":
            await self.runtime.async_release(self.identifier)
        else:
            await self.runtime.async_reconcile(self.identifier)
