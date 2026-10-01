"""Observed switches and durable policy-enabled controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OperatorConfigEntry
from .entity import OperatorEntity, ResourceEntity

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
        if data["kind"] == "switch" and runtime.manual_control(identifier):
            async_add_entities([OperatorSwitch(runtime, identifier)], config_subentry_id=identifier)
    for identifier in runtime.policies:
        async_add_entities([PolicySwitch(runtime, identifier)], config_subentry_id=identifier)
    for identifier in runtime.intents:
        async_add_entities([DesiredSwitch(runtime, identifier)], config_subentry_id=identifier)


class DesiredSwitch(OperatorEntity, SwitchEntity):
    """A held logical intent; never a claim that any follower has reached it."""

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "desired")

    @callback
    def _runtime_updated(self) -> None:
        if context := self.runtime.intent_context(self.identifier):
            self._context = context
        super()._runtime_updated()

    @property
    def available(self) -> bool:
        return not self.runtime.fault and not self.runtime.store.fault

    @property
    def is_on(self) -> bool | None:
        return self.runtime.desired_value(self.identifier)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "state_role": "desired",
            "intent_id": self.identifier,
            "on_targets": self.runtime.intents[self.identifier]["on_targets"],
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_set_desired(self.identifier, True, context=self._context)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_desired(self.identifier, False, context=self._context)


class OperatorSwitch(ResourceEntity, SwitchEntity):
    """Switch exposing physical feedback."""

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "managed")

    @property
    def is_on(self) -> bool | None:
        target = self.observed_target
        return target.on if target else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier, target={"on": True}, source="entity", context=self._context
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_request(
            self.identifier, target={"on": False}, source="entity", context=self._context
        )


class PolicySwitch(OperatorEntity, SwitchEntity):
    """Enablement is a stored setting, not an actuator command."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: OperatorRuntime, identifier: str) -> None:
        super().__init__(runtime, identifier, "enabled")

    @property
    def is_on(self) -> bool:
        return self.runtime.policy_enabled(self.identifier)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_set_policy_enabled(self.identifier, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_policy_enabled(self.identifier, False)
