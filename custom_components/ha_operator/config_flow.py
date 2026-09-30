"""Native singleton and subentry configuration flows for HA Operator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .configuration import KINDS, RESOURCE_DEFAULTS, ConfigurationError, validate_configuration
from .const import DOMAIN, NAME


def _entity(*, domains: list[str] | None = None, multiple: bool = False) -> selector.EntitySelector:
    config: dict[str, Any] = {"multiple": multiple}
    if domains:
        config["filter"] = [{"domain": domains}]
    return selector.EntitySelector(config)


def _number(minimum: float = 0.001, maximum: float | None = None) -> selector.NumberSelector:
    config: dict[str, Any] = {"min": minimum, "step": "any", "mode": "box"}
    if maximum is not None:
        config["max"] = maximum
    return selector.NumberSelector(config)


def _options_schema(options: dict[str, Any] | None = None) -> vol.Schema:
    """Use explicit opt-in defaults for both tracing and the global observe lock."""
    current = options or {}
    return vol.Schema(
        {
            vol.Optional(
                "trace_enabled", default=current.get("trace_enabled", False)
            ): selector.BooleanSelector(),
            vol.Optional(
                "shadow_lock", default=current.get("shadow_lock", False)
            ): selector.BooleanSelector(),
            vol.Optional("trace_entities", default=current.get("trace_entities", [])): _entity(
                multiple=True
            ),
        }
    )


class OperatorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single integration; resources are native subentries."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OperatorOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {
            "resource": ResourceFlow,
            "policy": PolicyFlow,
            "requirement": RequirementFlow,
            "intent": IntentFlow,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=NAME, data={}, options=user_input)
        return self.async_show_form(step_id="user", data_schema=_options_schema())


class OperatorOptionsFlow(OptionsFlow):
    """Edit options through HA's existing single reload listener."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            options = {**self.config_entry.options, **user_input}
            if self.config_entry.options.get("shadow_lock", False) and not options["shadow_lock"]:
                runtime = getattr(self.config_entry, "runtime_data", None)
                if self.config_entry.state is not ConfigEntryState.LOADED or runtime is None:
                    errors["base"] = "unlock_unavailable"
                else:
                    try:
                        await runtime.async_prepare_unlock()
                    except HomeAssistantError:
                        errors["base"] = "unlock_persistence_failed"
                    else:
                        if (
                            self.config_entry.state is not ConfigEntryState.LOADED
                            or getattr(self.config_entry, "runtime_data", None) is not runtime
                        ):
                            errors["base"] = "unlock_unavailable"
            if not errors:
                return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(dict(self.config_entry.options)),
            errors=errors,
        )


class OperatorSubentryFlow(ConfigSubentryFlow):
    """Share atomic validation and reconfiguration with a single reload listener."""

    kind: str

    def _schema(self) -> vol.Schema:
        raise NotImplementedError

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        return await self._async_step("user", user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        return await self._async_step("reconfigure", user_input)

    def _validated_data(self, user_input: dict[str, Any], current: Any) -> dict[str, Any]:
        entry = self._get_entry()
        candidate_id = current.subentry_id if current else "__new__"
        candidate = SimpleNamespace(
            subentry_type=self.kind, subentry_id=candidate_id, data=user_input
        )
        subentries = [
            item for item in entry.subentries.values() if item.subentry_id != candidate_id
        ]
        all_data = validate_configuration([*subentries, candidate], self.hass)
        bucket = {
            "resource": "resources",
            "policy": "policies",
            "requirement": "requirements",
            "intent": "intents",
        }[self.kind]
        return all_data[bucket][candidate_id]

    async def _async_step(self, step: str, user_input: dict[str, Any] | None):
        entry = self._get_entry()
        current = self._get_reconfigure_subentry() if step == "reconfigure" else None
        if self.kind == "policy" and not entry.get_subentries_of_type("resource"):
            return self.async_abort(reason="no_resources")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                data = self._validated_data(user_input, current)
            except ConfigurationError as err:
                errors["base"] = err.code
                placeholders["detail"] = err.detail
            else:
                if current is not None:
                    return self.async_update_and_abort(
                        entry, current, title=data["name"], data=data
                    )
                return self.async_create_entry(title=data["name"], data=data)
        values = user_input if user_input is not None else dict(current.data) if current else {}
        return self.async_show_form(
            step_id=step,
            data_schema=self.add_suggested_values_to_schema(self._schema(), values),
            errors=errors,
            description_placeholders=placeholders,
        )


class ResourceFlow(OperatorSubentryFlow):
    """Choose the physical output and adapter-specific settings."""

    kind = "resource"

    def _schema(self) -> vol.Schema:
        fields: dict[Any, Any] = {
            vol.Required("name"): selector.TextSelector(),
            vol.Required("kind"): selector.SelectSelector(
                {"options": list(KINDS), "mode": "dropdown"}
            ),
            vol.Optional("entity_id"): _entity(domains=["cover", "fan", "switch"]),
            vol.Optional("outputs"): _entity(domains=["switch"], multiple=True),
            vol.Optional("profiles"): selector.ObjectSelector(),
            vol.Optional("default_target"): selector.ObjectSelector(),
            vol.Optional("reversal_dead_time"): _number(),
            vol.Optional("restriction_entity"): _entity(),
            vol.Optional("fault_entity"): _entity(),
            vol.Optional("manual_control", default=True): selector.BooleanSelector(),
            vol.Optional("return_monitor"): selector.ObjectSelector(
                {
                    "fields": {
                        "target_at_most": {
                            "selector": {
                                "number": {"min": 0, "max": 100, "step": "any", "mode": "box"}
                            },
                            "required": True,
                        },
                        "warning_after_seconds": {
                            "selector": {"number": {"min": 0.001, "step": "any", "mode": "box"}},
                            "required": True,
                        },
                    },
                    "translation_key": "return_monitor",
                }
            ),
        }
        for key, default in RESOURCE_DEFAULTS.items():
            fields[vol.Optional(key, default=default)] = (
                _number(0, 100) if key == "tolerance" else _number()
            )
        return vol.Schema(fields)


class PolicyFlow(OperatorSubentryFlow):
    """Configure ordinary state-backed or explicitly submitted occurrence policies."""

    kind = "policy"

    async def _async_step(self, step: str, user_input: dict[str, Any] | None):
        if not self._get_entry().get_subentries_of_type("resource"):
            return self.async_abort(reason="no_resources")
        if user_input is None:
            result = await super()._async_step(step, None)
            if step == "reconfigure":
                current = self._get_reconfigure_subentry().data
                result["data_schema"] = self.add_suggested_values_to_schema(
                    result["data_schema"],
                    {"input_type": current.get("input", {}).get("type", "legacy")},
                )
            return result
        data = dict(user_input)
        input_type = data.pop("input_type", "legacy")
        if input_type == "legacy":
            return await super()._async_step(step, data)
        data["kind"] = "state" if input_type == "qualified_numeric" else "occurrence"
        current = self._get_reconfigure_subentry() if step == "reconfigure" else None
        try:
            if "eligibility_entity" in data or "eligibility_state" in data:
                raise ConfigurationError(
                    "input_eligibility_conflict",
                    "Remove helper eligibility fields for a native input",
                )
            self._validated_data(data, current)
        except ConfigurationError as err:
            return self.async_show_form(
                step_id=step,
                data_schema=self.add_suggested_values_to_schema(self._schema(), user_input),
                errors={"base": err.code},
                description_placeholders={"detail": err.detail},
            )
        self._policy_data = data
        self._policy_step = step
        self._input_type = input_type
        return await self._async_input_step(None)

    async def async_step_qualified_numeric(self, user_input: dict[str, Any] | None = None):
        return await self._async_input_step(user_input)

    async def async_step_timer_episode(self, user_input: dict[str, Any] | None = None):
        return await self._async_input_step(user_input)

    async def _async_input_step(self, user_input: dict[str, Any] | None):
        numeric = self._input_type == "qualified_numeric"
        fields = {
            vol.Required("entity_id"): _entity(domains=["sensor" if numeric else "timer"]),
            vol.Required("qualification_seconds"): _number(),
        }
        if numeric:
            fields.update(
                {
                    vol.Required("threshold"): selector.NumberSelector(
                        {"step": "any", "mode": "box"}
                    ),
                    vol.Required("unit"): selector.TextSelector(),
                }
            )
        else:
            fields[vol.Required("request_seconds")] = _number()
        errors, placeholders = {}, {}
        if user_input is not None:
            source = {**user_input, "type": self._input_type}
            if numeric:
                source["comparison"] = "below"
            result = await super()._async_step(
                self._policy_step, {**self._policy_data, "input": source}
            )
            if result["type"] != "form":
                return result
            errors, placeholders = result["errors"], result["description_placeholders"]
        current = (
            self._get_reconfigure_subentry().data.get("input", {})
            if self._policy_step == "reconfigure"
            else {}
        )
        values = current if user_input is None else user_input
        return self.async_show_form(
            step_id=self._input_type,
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), values),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _schema(self) -> vol.Schema:
        resources = self._get_entry().get_subentries_of_type("resource")
        return vol.Schema(
            {
                vol.Required("name"): selector.TextSelector(),
                vol.Required("resource_id"): selector.SelectSelector(
                    {
                        "options": [
                            {"value": item.subentry_id, "label": item.title} for item in resources
                        ],
                        "mode": "dropdown",
                    }
                ),
                vol.Required("kind"): selector.SelectSelector({"options": ["state", "occurrence"]}),
                vol.Optional("priority", default=0): _number(-1000000, 1000000),
                vol.Optional("input_type", default="legacy"): selector.SelectSelector(
                    {
                        "options": ["legacy", "qualified_numeric", "timer_episode"],
                        "translation_key": "policy_input_type",
                        "mode": "dropdown",
                    }
                ),
                vol.Optional("target"): selector.ObjectSelector(),
                vol.Optional("eligibility_entity"): _entity(),
                vol.Optional("eligibility_state"): selector.TextSelector(),
                vol.Optional("target_entity"): _entity(),
                vol.Optional("intent_id"): selector.SelectSelector(
                    {
                        "options": [
                            {"value": item.subentry_id, "label": item.title}
                            for item in self._get_entry().get_subentries_of_type("intent")
                        ],
                        "mode": "dropdown",
                    }
                ),
                vol.Optional("target_attribute"): selector.TextSelector(),
                vol.Optional("target_field"): selector.SelectSelector(
                    {"options": ["position", "on", "percentage", "direction"]}
                ),
            }
        )


class IntentFlow(OperatorSubentryFlow):
    """Configure one authoritative desired switch and optional ON-only attachment."""

    kind = "intent"

    def _schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name"): selector.TextSelector(),
                vol.Required("initial_value", default=False): selector.BooleanSelector(),
                vol.Optional("on_targets", default=[]): selector.SelectSelector(
                    {
                        "options": [
                            {"value": item.subentry_id, "label": item.title}
                            for item in self._get_entry().get_subentries_of_type("intent")
                        ],
                        "multiple": True,
                        "mode": "dropdown",
                    }
                ),
            }
        )


class RequirementFlow(OperatorSubentryFlow):
    """Configure explicit airflow activation and ordered evidence-backed providers."""

    kind = "requirement"

    def _schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name"): selector.TextSelector(),
                vol.Required("activation_entities"): _entity(multiple=True),
                vol.Required("providers"): selector.ObjectSelector(),
                vol.Optional("acquisition_timeout", default=120): _number(),
            }
        )
