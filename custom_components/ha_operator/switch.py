"""Observed switches and durable policy-enabled controls."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import OperatorEntity, ResourceEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    runtime = entry.runtime_data
    for identifier, data in runtime.resources.items():
        if data["kind"] == "switch":
            async_add_entities([OperatorSwitch(runtime, identifier)], config_subentry_id=identifier)
    for identifier in runtime.policies:
        async_add_entities([PolicySwitch(runtime, identifier)], config_subentry_id=identifier)


class OperatorSwitch(ResourceEntity, SwitchEntity):
    """Switch exposing physical feedback."""

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed", None)

    @property
    def is_on(self) -> bool | None:
        target = self.observed_target
        return target.on if target else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_request(self.identifier, target={"on": True}, source="entity")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_request(self.identifier, target={"on": False}, source="entity")


class PolicySwitch(OperatorEntity, SwitchEntity):
    """Enablement is a stored setting, not an actuator command."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: Any, identifier: str) -> None:
        super().__init__(runtime, identifier, "enabled", "Enabled")

    @property
    def is_on(self) -> bool:
        return self.runtime.policy_enabled(self.identifier)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_set_policy_enabled(self.identifier, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_policy_enabled(self.identifier, False)
