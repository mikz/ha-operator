"""Test-only raw devices; never included in the production integration archive."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

DOMAIN = "ha_operator_sim"
_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.COVER, Platform.SWITCH, Platform.FAN, Platform.BINARY_SENSOR, Platform.SENSOR]
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("url"): cv.url,
                vol.Optional("poll_interval", default=0.25): vol.All(
                    vol.Coerce(float), vol.Range(min=0.05)
                ),
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


class SimulatorCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Poll only device feedback; scenario controls never enter Home Assistant."""

    def __init__(self, hass: HomeAssistant, url: str, poll_interval: float) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
            always_update=False,
        )
        self.url = url.rstrip("/")
        self.session = async_get_clientsession(hass)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            async with self.session.get(
                f"{self.url}/devices",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                response.raise_for_status()
                result = await response.json()
                return {device["id"]: device for device in result["devices"]}
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:
            raise UpdateFailed(f"Simulator feedback unavailable: {err}") from err

    async def command(self, device_id: str, action: str, **values: Any) -> None:
        try:
            async with self.session.post(
                f"{self.url}/devices/{device_id}/command",
                json={"action": action, **values},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                response.raise_for_status()
                await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise HomeAssistantError(f"Simulator command failed: {err}") from err
        # No optimistic assignment: a successful receipt does not confirm an effect.
        await self.async_request_refresh()


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    settings = config[DOMAIN]
    coordinator = SimulatorCoordinator(hass, settings["url"], settings["poll_interval"])
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        return False
    hass.data[DOMAIN] = coordinator

    async def stop(_event) -> None:
        await coordinator.async_shutdown()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    await asyncio.gather(
        *(
            discovery.async_load_platform(hass, platform, DOMAIN, {}, config)
            for platform in PLATFORMS
        )
    )
    return True
