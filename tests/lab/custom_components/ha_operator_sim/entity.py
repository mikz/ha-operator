"""Shared feedback entity for the disposable lab bridge."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SimulatorCoordinator


class SimulatorEntity(CoordinatorEntity[SimulatorCoordinator]):
    """Only the simulator's observed state is rendered as HA state."""

    _attr_should_poll = False

    def __init__(self, coordinator: SimulatorCoordinator, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self.device_id = device["id"]
        self._attr_unique_id = f"ha_operator_sim_{self.device_id}"
        self._attr_name = f"Sim {device['name']}"
        self.entity_id = f"{device['kind']}.sim_{self.device_id}"

    @property
    def observation(self) -> dict[str, Any]:
        return self.coordinator.data.get(self.device_id, {}).get("observable", {})

    @property
    def available(self) -> bool:
        return super().available and bool(self.observation.get("available", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sim_observed_at": self.observation.get("observed_at")}
