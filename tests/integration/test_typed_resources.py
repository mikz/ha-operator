"""Native number/select actors through real platforms, workers and manual leases."""

# ruff: noqa: F811 - imported pytest fixtures are injected by name

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.ha_operator.const import DOMAIN
from custom_components.ha_operator.core import Target
from tests.integration.helpers import async_setup_operator
from tests.integration.test_config_flow import _start
from tests.integration.test_runtime_reconciliation import advance, runtime_factory  # noqa: F401


def number(hass, value=0, unit="°C", **metadata):
    hass.states.async_set(
        "number.actor",
        str(value),
        {"min": -20, "max": 40, "step": 1, "unit_of_measurement": unit, **metadata},
    )


def select(hass, value="quiet", options=None):
    hass.states.async_set(
        "select.actor", value, {"options": options if options is not None else ["quiet", "boost"]}
    )


def number_config(**extra):
    return {
        "name": "Setpoint",
        "kind": "number",
        "entity_id": "number.actor",
        "unit_of_measurement": "°C",
        "tolerance": 0.01,
        **extra,
    }


def select_config(**extra):
    return {"name": "Operating mode", "kind": "select", "entity_id": "select.actor", **extra}


@pytest.mark.parametrize("value", [-5.5, 0, 2.5])
async def test_native_number_accepts_negative_zero_and_non_grid_values(
    hass, runtime_factory, value
):
    number(hass, 10)
    sent = []
    hass.services.async_register("number", "set_value", lambda call: sent.append(dict(call.data)))
    factory, _ = runtime_factory
    runtime = await factory(resources={"n": number_config()})
    await runtime.async_set_mode("n", "live")
    await runtime.async_request("n", target={"value": value}, indefinite=True)
    await hass.async_block_till_done()
    assert sent == [{"entity_id": "number.actor", "value": value}]
    assert runtime.decisions["n"].status != "satisfied"  # A service return is not feedback.
    number(hass, value)
    await hass.async_block_till_done()
    assert runtime.decisions["n"].status == "satisfied"


async def test_select_dynamic_options_state_fallback_but_manual_keeps_ownership(
    hass, runtime_factory
):
    select(hass)
    sent = []
    hass.services.async_register(
        "select", "select_option", lambda call: sent.append(call.data["option"])
    )
    factory, _ = runtime_factory
    runtime = await factory(
        resources={"s": select_config()},
        policies={
            "high": {
                "name": "Boost",
                "resource_id": "s",
                "kind": "state",
                "target": {"option": "boost"},
                "priority": 20,
            },
            "low": {
                "name": "Quiet",
                "resource_id": "s",
                "kind": "state",
                "target": {"option": "quiet"},
                "priority": 10,
            },
        },
    )
    await runtime.async_set_mode("s", "live")
    assert runtime.decisions["s"].target == Target(option="boost")
    receipt = await runtime.async_request("s", target={"option": "boost"}, indefinite=True)
    select(hass, options=["quiet"])
    await hass.async_block_till_done()
    assert runtime.manual("s").request_id == receipt["request_id"]
    assert runtime.decisions["s"].target == Target(option="boost")
    assert runtime.decisions["s"].reason == "invalid_target"
    await runtime.async_release("s")
    await hass.async_block_till_done()
    assert runtime.decisions["s"].target == Target(option="quiet")
    assert runtime.decisions["s"].source == "policy:low"
    rejected = next(
        item for item in runtime.decisions["s"].candidates if item.source == "policy:high"
    )
    assert not rejected.eligible


async def test_accepted_number_occurrence_becomes_invalid_without_retargeting(
    hass, runtime_factory
):
    number(hass)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={"n": number_config()},
        policies={
            "p": {
                "name": "Setpoint",
                "resource_id": "n",
                "kind": "occurrence",
                "target": {"value": 20},
            }
        },
    )
    await runtime.async_set_mode("n", "live")
    from homeassistant.util import dt as dt_util

    expiry = dt_util.utcnow().timestamp() + 60
    accepted = await runtime.async_submit_occurrence("p", "external", expiry)
    number(hass, max=10)
    await hass.async_block_till_done()
    assert runtime.decisions["n"].target == Target(value=20)
    assert runtime.decisions["n"].reason == "invalid_target"
    assert accepted["expires_at"] == expiry
    assert not next(iter(runtime._state["occurrences"].values())).get("skipped")


async def test_number_unit_drift_keeps_intent_and_inhibits_only_affected_resource(
    hass, runtime_factory
):
    number(hass, 20)
    select(hass)
    factory, _ = runtime_factory
    runtime = await factory(
        resources={"n": number_config(), "s": select_config()},
        policies={
            "p": {"name": "Setpoint", "resource_id": "n", "kind": "state", "target": {"value": 20}}
        },
    )
    await runtime.async_set_mode("n", "live")
    assert runtime.decisions["n"].status == "satisfied"
    number(hass, 20, "°F")
    await hass.async_block_till_done()
    assert runtime.decisions["n"].target == Target(value=20)
    assert runtime.decisions["n"].status == "unavailable"
    assert not runtime.observations["n"].available
    assert runtime.observations["s"].available
    with pytest.raises(ServiceValidationError):
        await runtime.async_request("n", target={"value": 20})
    assert not runtime.fault


@pytest.mark.parametrize(
    "source_unit,attribute,eligible",
    [("°C", None, True), ("°F", None, False), (None, None, False), ("°F", "desired", True)],
)
async def test_dynamic_number_source_unit_is_explicit(
    hass, runtime_factory, source_unit, attribute, eligible
):
    number(hass)
    hass.states.async_set(
        "sensor.desired", "20", {"unit_of_measurement": source_unit, "desired": 20}
    )
    factory, _ = runtime_factory
    policy = {
        "name": "Requested",
        "kind": "state",
        "resource_id": "n",
        "target_entity": "sensor.desired",
        "target_field": "value",
    }
    if attribute:
        policy["target_attribute"] = attribute
    runtime = await factory(resources={"n": number_config()}, policies={"p": policy})
    assert (runtime.decisions["n"].target is not None) is eligible


async def test_real_native_managed_platforms_dynamic_metadata_and_unit_drift(hass, tmp_path):
    number(hass, 20)
    select(hass)
    entry = await async_setup_operator(
        hass,
        tmp_path,
        resources={"n": number_config(), "s": select_config()},
        policies={
            "fixed": {
                "name": "Setpoint",
                "kind": "state",
                "resource_id": "n",
                "target": {"value": 20},
            }
        },
    )
    runtime = entry.runtime_data
    await runtime.async_set_mode("n", "live")
    await runtime.async_set_mode("s", "live")
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    managed_number = registry.async_get_entity_id("number", DOMAIN, "n_managed")
    managed_select = registry.async_get_entity_id("select", DOMAIN, "s_managed")
    assert hass.states.get(managed_number).state == "20.0"
    assert hass.states.get(managed_select).state == "quiet"
    assert registry.async_get_entity_id("select", DOMAIN, "s_mode") is not None
    desired = registry.async_get_entity_id("sensor", DOMAIN, "n_desired")
    observed = registry.async_get_entity_id("sensor", DOMAIN, "n_observed")
    reason = registry.async_get_entity_id("sensor", DOMAIN, "n_reason")
    assert hass.states.get(desired).attributes["unit_of_measurement"] == "°C"
    assert hass.states.get(observed).attributes["unit_of_measurement"] == "°C"
    number(hass, 20, "°F", min=-4, max=104, step=0.5)
    await hass.async_block_till_done()
    state = hass.states.get(managed_number)
    assert state.state == "unavailable"
    assert state.attributes["unit_of_measurement"] == "°F"
    assert state.attributes["min"] == -4
    assert hass.states.get(desired).attributes["unit_of_measurement"] == "°C"
    assert hass.states.get(desired).state == "20.0"
    assert hass.states.get(observed).attributes["unit_of_measurement"] == "°F"
    assert hass.states.get(observed).state == "unavailable"
    assert hass.states.get(reason).attributes["target_unit"] == "°C"
    assert hass.states.get(reason).attributes["observed_unit"] == "°F"
    before = dict(runtime._state["requests"])
    await hass.services.async_call(
        "number", "set_value", {"entity_id": managed_number, "value": 25}, blocking=True
    )
    assert runtime.manual("n") is None
    assert runtime._state["requests"] == before
    await hass.config_entries.async_unload(entry.entry_id)


async def test_native_number_form_captures_unit_once_and_blocks_outstanding_rebind(hass):
    number(hass)
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    data = {"name": "Setpoint", "kind": "number", "entity_id": "number.actor", "tolerance": 0.1}
    result = await hass.config_entries.subentries.async_configure(form["flow_id"], data)
    assert result["type"] == "create_entry"
    resource = next(iter(entry.subentries.values()))
    assert resource.data["unit_of_measurement"] == "°C"
    number(hass, unit="°F")
    edit = await entry.start_subentry_reconfigure_flow(hass, resource.subentry_id)
    changed = await hass.config_entries.subentries.async_configure(edit["flow_id"], data)
    assert changed["reason"] == "reconfigure_successful"
    assert entry.subentries[resource.subentry_id].data["unit_of_measurement"] == "°C"
    # Without a loaded runtime, saved numeric intent cannot be proven absent.
    edit = await entry.start_subentry_reconfigure_flow(hass, resource.subentry_id)
    changed = await hass.config_entries.subentries.async_configure(
        edit["flow_id"], {**data, "unit_of_measurement": "°F"}
    )
    assert changed["errors"]["base"] == "config_number_binding"
    assert entry.subentries[resource.subentry_id].data["unit_of_measurement"] == "°C"


async def test_number_explicit_rebind_rejects_outstanding_manual_value(hass, tmp_path):
    number(hass, 20)
    entry = await async_setup_operator(hass, tmp_path, resources={"n": number_config()})
    runtime = entry.runtime_data
    await runtime.async_set_mode("n", "live")
    receipt = await runtime.async_request("n", target={"value": 20}, indefinite=True)
    number(hass, 20, "°F")
    await hass.async_block_till_done()
    edit = await entry.start_subentry_reconfigure_flow(hass, "n")
    response = await hass.config_entries.subentries.async_configure(
        edit["flow_id"], {**number_config(), "unit_of_measurement": "°F"}
    )
    assert response["errors"]["base"] == "config_number_binding"
    assert entry.subentries["n"].data["unit_of_measurement"] == "°C"
    assert runtime.manual("n").request_id == receipt["request_id"]
    await runtime.async_release("n")
    changed = await hass.config_entries.subentries.async_configure(
        response["flow_id"], {**number_config(), "unit_of_measurement": "°F"}
    )
    assert changed["reason"] == "reconfigure_successful"
    assert entry.subentries["n"].data["unit_of_measurement"] == "°F"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["number", "select"])
async def test_typed_resource_without_manual_control_has_no_managed_entity(hass, tmp_path, kind):
    number(hass)
    select(hass)
    config = (
        number_config(manual_control=False)
        if kind == "number"
        else select_config(manual_control=False)
    )
    entry = await async_setup_operator(hass, tmp_path, resources={"r": config})
    assert er.async_get(hass).async_get_entity_id(kind, DOMAIN, "r_managed") is None
    assert er.async_get(hass).async_get_entity_id("select", DOMAIN, "r_mode") is not None
    await entry.runtime_data.async_set_mode("r", "live")
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.async_request(
            "r", target={"value": 0} if kind == "number" else {"option": "quiet"}
        )
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("option", ["", " ", " quiet "])
async def test_select_preserves_exact_native_option_strings(hass, runtime_factory, option):
    select(hass, value=option, options=[option])
    factory, _ = runtime_factory
    runtime = await factory(
        resources={"s": select_config()},
        policies={
            "p": {
                "name": "Native",
                "kind": "state",
                "resource_id": "s",
                "target": {"option": option},
            }
        },
    )
    await runtime.async_set_mode("s", "live")
    assert runtime.observations["s"].target.option == option
    assert runtime.decisions["s"].target.option == option
    assert runtime.decisions["s"].status == "satisfied"


async def test_number_requirement_fixed_target_prevents_rebind(hass, tmp_path):
    number(hass, 20)
    hass.states.async_set("binary_sensor.active", "off")
    hass.states.async_set("switch.evidence", "on")
    entry = await async_setup_operator(
        hass,
        tmp_path,
        resources={"n": number_config()},
        requirements={
            "q": {
                "name": "Demand",
                "activation_entities": ["binary_sensor.active"],
                "providers": [
                    {
                        "id": "p",
                        "resource_id": "n",
                        "target": {"value": 20},
                        "evidence": [
                            {
                                "entity_id": "switch.evidence",
                                "kind": "relay",
                                "operator": "eq",
                                "value": True,
                            }
                        ],
                    }
                ],
            }
        },
    )
    number(hass, 20, "°F")
    await hass.async_block_till_done()
    form = await entry.start_subentry_reconfigure_flow(hass, "n")
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {**number_config(), "unit_of_measurement": "°F"}
    )
    assert result["errors"]["base"] == "config_number_binding"
    assert entry.subentries["n"].data["unit_of_measurement"] == "°C"
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "metadata",
    [
        {"min": 10**1000},
        {"max": float("inf")},
        {"step": float("nan")},
        {"step": None},
        {"min": None},
    ],
)
async def test_malformed_number_metadata_inhibits_control_without_losing_observed_value(
    hass, tmp_path, metadata
):
    number(hass, 20, **metadata)
    entry = await async_setup_operator(hass, tmp_path, resources={"n": number_config()})
    runtime = entry.runtime_data
    await runtime.async_set_mode("n", "live")
    await hass.async_block_till_done()
    managed = er.async_get(hass).async_get_entity_id("number", DOMAIN, "n_managed")
    state = hass.states.get(managed)
    assert state.state == "unavailable"
    assert (
        "min" not in state.attributes
        and "max" not in state.attributes
        and "step" not in state.attributes
    )
    assert runtime.observations["n"].target == Target(value=20)
    with pytest.raises(ServiceValidationError):
        await runtime.async_request("n", target={"value": 20})
    assert not runtime.fault
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unitless_number_source_and_binding(hass, runtime_factory):
    number(hass, unit=None)
    hass.states.async_set("input_number.desired", "0")
    factory, _ = runtime_factory
    runtime = await factory(
        resources={"n": number_config(unit_of_measurement=None)},
        policies={
            "p": {
                "name": "Unitless",
                "kind": "state",
                "resource_id": "n",
                "target_entity": "input_number.desired",
                "target_field": "value",
            }
        },
    )
    await runtime.async_set_mode("n", "live")
    assert runtime.decisions["n"].target == Target(value=0)
    assert runtime.decisions["n"].status == "satisfied"


async def test_number_form_unitless_binding_suggestion_and_wrong_explicit_unit(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    number(hass, unit=None)
    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    form = await _start(hass, entry, "resource")
    data = {"name": "Unitless", "kind": "number", "entity_id": "number.actor", "tolerance": 0.1}
    wrong = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {**data, "unit_of_measurement": "°C"}
    )
    assert wrong["errors"] == {"base": "config_number_binding"}
    result = await hass.config_entries.subentries.async_configure(wrong["flow_id"], data)
    resource = next(iter(entry.subentries.values()))
    assert result["type"] == "create_entry"
    assert resource.data["unit_of_measurement"] is None
    edit = await entry.start_subentry_reconfigure_flow(hass, resource.subentry_id)
    key = next(key for key in edit["data_schema"].schema if key == "unit_of_measurement")
    assert key.description["suggested_value"] == ""
