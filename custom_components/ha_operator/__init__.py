"""HA Operator: native observable device reconciliation."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .configuration import ConfigurationError
from .const import DOMAIN, PLATFORMS

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    from .services import async_register_services

    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
            translation_key="configuration_error",
            translation_placeholders={"detail": err.detail},
        )
        raise ConfigEntryError(
            err.detail,
            translation_domain=DOMAIN,
            translation_key="configuration_error",
            translation_placeholders={"detail": err.detail},
        ) from err
    ir.async_delete_issue(hass, DOMAIN, f"configuration_{entry.entry_id}")
    entry.runtime_data = runtime
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
    try:
        await runtime.async_start()
        if runtime.fault is None:
            ir.async_delete_issue(hass, DOMAIN, f"storage_{entry.entry_id}")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.async_close()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_updated))
    return True


async def _async_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await entry.runtime_data.async_close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return entry.version == 1


async def async_remove_config_entry_device(hass, config_entry, device_entry) -> bool:
    """Resources are removed with their subentry, not by deleting live devices."""
    return config_entry.entry_id not in device_entry.config_entries
