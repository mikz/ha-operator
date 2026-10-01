"""Domain actions registered independently of a loaded configuration entry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol

from . import OperatorConfigEntry

if TYPE_CHECKING:
    from .runtime import OperatorRuntime
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


def _trace_integer(value: object) -> int:
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
    def runtime_for(call: ServiceCall) -> OperatorRuntime:
        entry_id = call.data.get("config_entry_id")
        if not entry_id:
            entries = hass.config_entries.async_entries(DOMAIN)
            if len(entries) != 1:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="configure_first"
                )
            entry_id = entries[0].entry_id
        # Native lookup checks the domain and loaded entry before exposing runtime data.
        entry = cast(OperatorConfigEntry, service.async_get_config_entry(hass, DOMAIN, entry_id))
        return entry.runtime_data

    def resource_for(
        call: ServiceCall, runtime: OperatorRuntime, *, optional: bool = False
    ) -> str | None:
        key = call.data.get("resource_id")
        entity_ids = call.data.get("entity_id", [])
        if key and entity_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="ambiguous_resource"
            )
        if entity_ids:
            if len(entity_ids) != 1:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="one_managed_entity"
                )
            entity = er.async_get(hass).async_get(entity_ids[0])
            if (
                entity is None
                or entity.platform != DOMAIN
                or entity.config_entry_id != runtime.entry.entry_id
            ):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="not_managed_entity"
                )
            key = entity.config_subentry_id
        if key is None and not optional:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="resource_required"
            )
        if key is not None and key not in runtime.resources:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_resource"
            )
        return key

    async def handle(call: ServiceCall) -> dict[str, Any] | None:
        # HA encodes native heterogeneous response values (including tuples and
        # observed attributes); its decoded-JSON alias is narrower than this boundary.
        runtime = runtime_for(call)
        if call.service == "seed_intents":
            await runtime.async_seed_intents(call.data["values"])
            return None
        if call.service == "export_trace":
            return await runtime.async_export_trace(
                after=call.data.get("after"), limit=call.data["limit"]
            )
        if call.service == "submit_occurrence":
            occurrence = await runtime.async_submit_occurrence(
                call.data["policy_id"],
                call.data["occurrence_id"],
                call.data["expires_at"],
                context=call.context,
            )
            return dict(occurrence) if call.return_response else None
        if call.service == "skip_occurrence":
            await runtime.async_skip_occurrence(
                call.data["policy_id"], call.data["occurrence_id"], call.data["expires_at"]
            )
            return None
        key = resource_for(call, runtime, optional=call.service in {"explain", "reconcile"})
        if call.service == "explain":
            return runtime.explain(key)
        if call.service == "reconcile":
            await runtime.async_reconcile(key)
            return None
        # resource_for requires a resource for the remaining registered actions.
        resource_id = cast(str, key)
        if call.service == "request":
            # Native schema-normalized ServiceCall data is the dynamic external boundary.
            result = await runtime.async_request(
                resource_id,
                mode=call.data["mode"],
                target=call.data.get("target"),
                duration=call.data.get("duration"),
                expires_at=call.data.get("expires_at"),
                indefinite=call.data["indefinite"],
                request_id=call.data.get("request_id"),
                context=call.context,
            )
            return dict(result) if call.return_response else None
        await runtime.async_release(resource_id)
        return None

    service.async_register_admin_service(
        hass,
        DOMAIN,
        "seed_intents",
        handle,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("values"): dict,
            }
        ),
    )
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
