"""Domain actions registered independently of a loaded configuration entry."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service

from .const import DOMAIN

_BASE = {
    vol.Optional("config_entry_id"): cv.string,
    vol.Optional("resource_id"): cv.string,
    vol.Optional("entity_id"): cv.entity_ids,
}
_REQUEST = vol.Schema(
    {
        **_BASE,
        vol.Optional("mode", default="target"): vol.In(("target", "hands_off")),
        vol.Optional("target"): dict,
        vol.Optional("duration"): vol.Any(int, float),
        vol.Optional("expires_at"): vol.Any(int, float),
        vol.Optional("indefinite", default=False): cv.boolean,
        vol.Optional("request_id"): cv.string,
    }
)
_OCCURRENCE = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Required("policy_id"): cv.string,
        vol.Required("occurrence_id"): cv.string,
        vol.Required("expires_at"): vol.Any(int, float),
    }
)


def _trace_integer(value):
    if type(value) is not int:
        raise vol.Invalid("Expected an integer")
    return value


_TRACE = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("after"): vol.All(_trace_integer, vol.Range(min=0)),
        vol.Optional("limit", default=100): vol.All(_trace_integer, vol.Range(min=1, max=1000)),
    }
)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    def runtime_for(call):
        entry_id = call.data.get("config_entry_id")
        if not entry_id:
            entries = hass.config_entries.async_entries(DOMAIN)
            if len(entries) != 1:
                raise ServiceValidationError("Configure HA Operator before calling actions")
            entry_id = entries[0].entry_id
        return service.async_get_config_entry(hass, DOMAIN, entry_id).runtime_data

    def resource_for(call, runtime, *, optional=False):
        key = call.data.get("resource_id")
        entity_ids = call.data.get("entity_id", [])
        if key and entity_ids:
            raise ServiceValidationError("Choose resource_id or entity_id")
        if entity_ids:
            if len(entity_ids) != 1:
                raise ServiceValidationError("Target exactly one managed entity")
            entity = er.async_get(hass).async_get(entity_ids[0])
            if (
                entity is None
                or entity.platform != DOMAIN
                or entity.config_entry_id != runtime.entry.entry_id
            ):
                raise ServiceValidationError("Target is not a managed HA Operator entity")
            key = entity.config_subentry_id
        if key is None and not optional:
            raise ServiceValidationError("Target a resource_id or managed entity_id")
        if key is not None and key not in runtime.resources:
            raise ServiceValidationError("Unknown resource")
        return key

    async def handle(call: ServiceCall):
        runtime = runtime_for(call)
        if call.service == "export_trace":
            return await runtime.async_export_trace(
                after=call.data.get("after"), limit=call.data["limit"]
            )
        if call.service in {"submit_occurrence", "skip_occurrence"}:
            result = await getattr(runtime, f"async_{call.service}")(
                call.data["policy_id"], call.data["occurrence_id"], call.data["expires_at"]
            )
            return result if call.return_response else None
        key = resource_for(call, runtime, optional=call.service in {"explain", "reconcile"})
        if call.service == "explain":
            return runtime.explain(key)
        if call.service == "request":
            arguments = {
                name: value for name, value in call.data.items() if name not in _TARGET_KEYS
            }
            result = await runtime.async_request(key, **arguments)
            return result if call.return_response else None
        await getattr(runtime, f"async_{call.service}")(key)
        return None

    service.async_register_admin_service(
        hass,
        DOMAIN,
        "export_trace",
        handle,
        schema=_TRACE,
        supports_response=SupportsResponse.ONLY,
    )
    for name in (
        "request",
        "release",
        "submit_occurrence",
        "skip_occurrence",
        "reconcile",
        "explain",
    ):
        hass.services.async_register(
            DOMAIN,
            name,
            handle,
            schema=_REQUEST
            if name == "request"
            else (_OCCURRENCE if "occurrence" in name else vol.Schema(_BASE)),
            supports_response=SupportsResponse.ONLY
            if name == "explain"
            else (
                SupportsResponse.OPTIONAL
                if name in {"request", "submit_occurrence"}
                else SupportsResponse.NONE
            ),
        )


_TARGET_KEYS = {"config_entry_id", "resource_id", "entity_id"}
