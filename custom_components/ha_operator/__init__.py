"""HA Operator: native observable device reconciliation."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .configuration import ConfigurationError
from .const import DOMAIN, PLATFORMS

if TYPE_CHECKING:
    from .runtime import OperatorRuntime

type OperatorConfigEntry = ConfigEntry[OperatorRuntime]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    from .services import async_register_services

    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OperatorConfigEntry) -> bool:
    from .runtime import OperatorRuntime

    try:
        runtime = OperatorRuntime(hass, entry)
    except ConfigurationError as err:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"configuration_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=err.translation_key,
            translation_placeholders=err.translation_placeholders,
        )
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key=err.translation_key,
            translation_placeholders=err.translation_placeholders,
        ) from err
    ir.async_delete_issue(hass, DOMAIN, f"configuration_{entry.entry_id}")
    entry.runtime_data = runtime
    try:
        await runtime.async_start()
        # Preserve IDs/customizations for resources converted to source followers,
        # but remove their formerly writable controls from the active HA surface.
        registry = er.async_get(hass)
        for identifier, resource in runtime.resources.items():
            domain = "fan" if resource["kind"] == "relay_fan" else resource["kind"]
            for platform, suffix in (
                (domain, "managed"),
                ("binary_sensor", "manual"),
                ("sensor", "expiry"),
                ("button", "release"),
            ):
                entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{identifier}_{suffix}")
                if entity_id is None:
                    continue
                registered = registry.async_get(entity_id)
                if registered is None:
                    continue
                if not runtime.manual_control(identifier) and registered.disabled_by is None:
                    registry.async_update_entity(
                        entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                    )
                elif (
                    runtime.manual_control(identifier)
                    and registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                ):
                    registry.async_update_entity(entity_id, disabled_by=None)
                if not runtime.manual_control(identifier):
                    # HA retains an unavailable placeholder when unloading an entity;
                    # disabling a registry entry does not clear that restored state.
                    if (state := hass.states.get(entity_id)) and state.attributes.get("restored"):
                        hass.states.async_remove(entity_id)
        ir.async_delete_issue(hass, DOMAIN, f"storage_{entry.entry_id}")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        try:
            await runtime.async_close()
        except BaseException:
            _LOGGER.exception("Unable to close HA Operator after failed initialization")
        raise
    entry.async_on_unload(entry.add_update_listener(_async_updated))
    return True


async def _async_updated(hass: HomeAssistant, entry: OperatorConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OperatorConfigEntry) -> bool:
    interrupt = not hass.is_stopping
    if not await entry.runtime_data.async_close(interrupt_timer_inputs=interrupt):
        return False
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: OperatorConfigEntry) -> None:
    """Remove native runtime storage when the user removes the integration."""
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: OperatorConfigEntry) -> bool:
    """Retain configuration and discard obsolete runtime files without loading them.

    Back up the deployment before upgrading to 0.2.0. Runtime state starts from
    configured defaults through native Store; subentries and registry IDs remain.
    """
    if entry.version not in (1, 2, 3):
        return False
    if entry.version == 3:
        return True

    def remove_obsolete_files() -> None:
        Path(hass.config.path(f"ha_operator.{entry.entry_id}.json")).unlink(missing_ok=True)
        trace = Path(hass.config.path(f"ha_operator_trace.{entry.entry_id}"))
        if trace.is_symlink():
            trace.unlink()
        elif trace.exists():
            shutil.rmtree(trace)

    await hass.async_add_executor_job(remove_obsolete_files)
    hass.config_entries.async_update_entry(
        entry,
        data={key: value for key, value in entry.data.items() if key != "initialized"},
        options={
            key: value
            for key, value in entry.options.items()
            if key not in {"trace_enabled", "trace_entities"}
        },
        version=3,
        minor_version=1,
    )
    _LOGGER.info("Removed obsolete HA Operator runtime files for entry %s", entry.entry_id)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: OperatorConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Resources are removed with their subentry, not by deleting live devices."""
    return config_entry.entry_id != device_entry.config_entry_id
