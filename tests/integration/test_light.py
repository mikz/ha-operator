"""Native light commands, independent feedback, and adaptive source ownership."""

# ruff: noqa: F811

import pytest
from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import ColorMode, LightEntityFeature
from homeassistant.setup import async_setup_component
from homeassistant.util import color as color_util
from homeassistant.util import dt as dt_util

from custom_components.ha_operator.adapters import LightAdapter
from custom_components.ha_operator.core import Target, TargetValidationError, matches
from custom_components.ha_operator.diagnostics import async_get_config_entry_diagnostics
from tests.integration.helpers import async_setup_operator
from tests.integration.test_runtime_reconciliation import advance, runtime_factory  # noqa: F401


def light_config(**extra):
    return {"name": "Studio", "kind": "light", "entity_id": "light.physical", **extra}


class PhysicalLight(LightEntity):
    _attr_name = "Physical"
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_color_mode = ColorMode.HS
    _attr_supported_features = LightEntityFeature.EFFECT | LightEntityFeature.TRANSITION
    _attr_effect_list = ["steady", "pulse"]
    _attr_effect = "steady"
    _attr_brightness = 120
    _attr_hs_color = (10, 20)
    _attr_min_color_temp_kelvin = 2200
    _attr_max_color_temp_kelvin = 6500
    _attr_is_on = False

    def __init__(self):
        self.entity_id = "light.physical"
        self.commands = []

    async def async_turn_on(self, **kwargs):
        self.commands.append((True, kwargs))

    async def async_turn_off(self, **kwargs):
        self.commands.append((False, kwargs))


async def add_physical(hass):
    assert await async_setup_component(hass, "light", {})
    raw = PhysicalLight()
    await hass.data["light"].async_add_entities([raw])
    return raw


async def test_native_light_preserves_capabilities_requests_and_actual_feedback(hass, tmp_path):
    raw = await add_physical(hass)
    entry = await async_setup_operator(hass, tmp_path, resources={"studio": light_config()})
    runtime = entry.runtime_data
    await runtime.async_set_mode("studio", "live")
    managed = next(
        state for state in hass.states.async_all("light") if state.entity_id != raw.entity_id
    )
    assert set(managed.attributes["supported_color_modes"]) == {"hs", "color_temp"}
    assert managed.attributes["supported_features"] == LightEntityFeature.EFFECT
    assert managed.attributes["min_color_temp_kelvin"] == 2200
    await hass.services.async_call(
        "light",
        "turn_on",
        {
            "entity_id": managed.entity_id,
            "brightness": 200,
            "hs_color": [45, 60],
            "effect": "pulse",
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    lease = runtime.manual("studio")
    assert lease.target == Target(on=True, brightness=200, hs_color=(45, 60), effect="pulse")
    assert raw.commands == [(True, {"brightness": 200, "hs_color": (45, 60), "effect": "pulse"})]
    assert hass.states.get(managed.entity_id).state == "off"
    raw._attr_is_on = True
    raw._attr_brightness = 200
    raw._attr_hs_color = (45, 60)
    raw._attr_effect = "pulse"
    raw.async_write_ha_state()
    await hass.async_block_till_done()
    assert runtime.decisions["studio"].status == "satisfied"
    assert hass.states.get(managed.entity_id).attributes["brightness"] == 200
    assert runtime.manual("studio").expires_at == lease.expires_at
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    exported = next(iter(diagnostics["resources"].values()))
    assert exported["kind"] == "light"
    assert exported["desired"]["brightness"] == 200
    assert exported["desired"]["hs_color"] == [45, 60]
    assert exported["desired"]["effect"] != "pulse"
    assert "light.physical" not in str(diagnostics)


async def test_bare_on_keeps_adaptive_source_settings_unconstrained(hass, runtime_factory):
    raw = await add_physical(hass)
    factory, _ = runtime_factory
    runtime = await factory(resources={"studio": light_config()})
    await runtime.async_set_mode("studio", "live")
    await runtime.async_request("studio", target={"on": True}, duration=120)
    await hass.async_block_till_done()
    lease = runtime.manual("studio")
    assert lease.target == Target(on=True)
    assert raw.commands == [(True, {})]
    # Native Adaptive Lighting owns source brightness/color; observations never renew leases.
    raw._attr_is_on = True
    raw._attr_brightness = 25
    raw._attr_hs_color = (70, 40)
    raw.async_write_ha_state()
    await hass.async_block_till_done()
    raw._attr_brightness = 30
    raw.async_write_ha_state()
    await hass.async_block_till_done()
    assert runtime.manual("studio") == lease
    assert runtime.decisions["studio"].status == "satisfied"
    assert raw.commands == [(True, {})]


@pytest.mark.parametrize(
    "target",
    [
        {"brightness": 256},
        {"hs_color": [1]},
        {"xy_color": [0, float("nan")]},
        {"rgb_color": [255, 0, -1]},
        {"on": False, "brightness": 20},
        {"hs_color": [0, 0], "color_temp_kelvin": 3000},
        {"color_temp_kelvin": 1000},
        {"rgbw_color": [0, 0, 0, 20]},
        {"effect": "missing"},
    ],
)
async def test_light_rejects_invalid_or_unsupported_targets(hass, target):
    await add_physical(hass)
    adapter = LightAdapter(hass, "studio", light_config())
    with pytest.raises(TargetValidationError):
        adapter.normalize(Target.from_dict(target))


def test_native_colors_roundtrip_and_compare():
    target = Target.from_dict({"on": True, "xy_color": [0.2, 0.4]})
    assert Target.from_dict(target.to_dict()) == target
    assert matches(target, Target(on=True, xy_color=(0.21, 0.4)))
    assert not matches(target, Target(on=True, xy_color=(0.5, 0.4)))


async def test_managed_off_dimming_and_temperature_use_timed_manual_boundary(
    hass, tmp_path, freezer
):
    raw = await add_physical(hass)
    entry = await async_setup_operator(
        hass, tmp_path, resources={"studio": light_config(manual_duration=90)}
    )
    runtime = entry.runtime_data
    await runtime.async_set_mode("studio", "live")
    managed = next(
        state for state in hass.states.async_all("light") if state.entity_id != raw.entity_id
    )
    for service, attributes, expected in (
        ("turn_on", {"brightness": 45}, Target(on=True, brightness=45)),
        (
            "turn_on",
            {"color_temp_kelvin": 3000},
            Target(on=True, brightness=45, color_temp_kelvin=3000),
        ),
        ("turn_off", {}, Target(on=False)),
    ):
        await hass.services.async_call(
            "light", service, {"entity_id": managed.entity_id, **attributes}, blocking=True
        )
        await hass.async_block_till_done()
        lease = runtime.manual("studio")
        assert lease.target == expected
        assert 89 <= lease.expires_at - dt_util.utcnow().timestamp() <= 90
        await advance(hass, freezer, 31)
    assert raw.commands == [
        (True, {"brightness": 45}),
        (True, {"brightness": 45, "color_temp_kelvin": 3000}),
    ]
    # OFF was observed already; a manual OFF lease still owns selection without fabricated dispatch.
    assert runtime.manual("studio").target == Target(on=False)


def test_kelvin_matches_native_whole_mired_quantization():
    assert matches(Target(color_temp_kelvin=5000), Target(color_temp_kelvin=5025))
    assert not matches(Target(color_temp_kelvin=5000), Target(color_temp_kelvin=5100))
    assert Target(color_temp_kelvin=200000).color_temp_kelvin == 200000
    with pytest.raises(TargetValidationError):
        Target(color_temp_kelvin=0)


async def test_partial_manual_light_settings_compose_without_reviving_after_off(
    hass, runtime_factory
):
    await add_physical(hass)
    factory, _ = runtime_factory
    runtime = await factory(resources={"studio": light_config()})
    await runtime.async_set_mode("studio", "live")
    await runtime.async_request(
        "studio", target={"on": True, "hs_color": [45, 60], "brightness": 200}
    )
    await runtime.async_request("studio", target={"brightness": 100})
    assert runtime.manual("studio").target == Target(on=True, hs_color=(45, 60), brightness=100)
    await runtime.async_request("studio", target={"color_temp_kelvin": 3000})
    assert runtime.manual("studio").target == Target(
        on=True, color_temp_kelvin=3000, brightness=100
    )
    await runtime.async_request("studio", target={"on": True})
    assert runtime.manual("studio").target == Target(on=True)
    await runtime.async_request("studio", target={"on": False})
    await runtime.async_request("studio", target={"brightness": 30})
    assert runtime.manual("studio").target == Target(on=True, brightness=30)


async def test_light_missing_at_startup_recovers_native_capabilities(hass, tmp_path, caplog):
    entry = await async_setup_operator(hass, tmp_path, resources={"studio": light_config()})
    managed = next(state for state in hass.states.async_all("light"))
    assert managed.state == "unavailable"
    assert "supported_color_modes" not in managed.attributes
    await add_physical(hass)
    await hass.async_block_till_done()
    recovered = hass.states.get(managed.entity_id)
    assert recovered.state == "off"
    assert set(recovered.attributes["supported_color_modes"]) == {"hs", "color_temp"}
    assert not any(record.levelname == "ERROR" for record in caplog.records)
    assert entry.runtime_data.observations["studio"].available


async def test_native_white_mode_keeps_white_commands_and_partial_dimming(hass, runtime_factory):
    hass.states.async_set(
        "light.physical",
        "on",
        {
            "supported_color_modes": ["rgb", "white"],
            "color_mode": "white",
            "brightness": 60,
        },
    )
    adapter = LightAdapter(hass, "studio", light_config())
    target = adapter.normalize(Target(on=True, white=60))
    assert matches(target, adapter.read_observation(0))
    assert adapter.normalize(Target(on=True, white=60, brightness=90)) == Target(on=True, white=90)
    sent = []
    hass.services.async_register("light", "turn_on", lambda call: sent.append(dict(call.data)))
    await adapter.async_apply(target, lambda: True)
    assert sent == [{"entity_id": "light.physical", "white": 60}]
    factory, _ = runtime_factory
    runtime = await factory(resources={"studio": light_config()})
    await runtime.async_set_mode("studio", "live")
    await runtime.async_request("studio", target={"white": 60})
    await runtime.async_request("studio", target={"brightness": 90})
    assert runtime.manual("studio").target == Target(on=True, white=90)


@pytest.mark.parametrize("color", [(360, 100), (359.9, 100), (60, 0)])
async def test_native_hs_roundtrip_does_not_retry_equivalent_color(
    hass, runtime_factory, freezer, color
):
    raw = await add_physical(hass)
    factory, _ = runtime_factory
    runtime = await factory(resources={"studio": light_config()})
    await runtime.async_set_mode("studio", "live")
    await runtime.async_request("studio", target={"on": True, "hs_color": color})
    await hass.async_block_till_done()
    assert len(raw.commands) == 1
    raw._attr_is_on = True
    raw._attr_hs_color = color_util.color_RGB_to_hs(*color_util.color_hs_to_RGB(*color))
    raw.async_write_ha_state()
    await hass.async_block_till_done()
    assert runtime.decisions["studio"].status == "satisfied"
    await advance(hass, freezer, 301)
    assert len(raw.commands) == 1
    assert not matches(Target(hs_color=color), Target(hs_color=(180, 90)))


def test_achromatic_hue_does_not_hide_saturation_or_brightness_error():
    assert matches(Target(hs_color=(60, 0)), Target(hs_color=(0, 0)))
    assert not matches(Target(hs_color=(60, 0)), Target(hs_color=(0, 10)))
    assert not matches(
        Target(hs_color=(60, 0), brightness=100), Target(hs_color=(0, 0), brightness=50)
    )


@pytest.mark.parametrize("white,brightness,on", [(0, 90, True), (90, 0, False)])
async def test_public_request_white_power_uses_native_brightness_precedence(
    hass, tmp_path, white, brightness, on
):
    assert await async_setup_component(hass, "light", {})
    raw = PhysicalLight()
    raw._attr_supported_color_modes = {ColorMode.RGB, ColorMode.WHITE}
    raw._attr_color_mode = ColorMode.WHITE
    raw._attr_is_on = not on
    await hass.data["light"].async_add_entities([raw])
    entry = await async_setup_operator(hass, tmp_path, resources={"studio": light_config()})
    runtime = entry.runtime_data
    await runtime.async_set_mode("studio", "live")
    await hass.services.async_call(
        "ha_operator",
        "request",
        {"resource_id": "studio", "target": {"on": True, "white": white, "brightness": brightness}},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert runtime.manual("studio").target == (
        Target(on=True, white=90) if on else Target(on=False)
    )
    assert raw.commands == [(on, {"white": 90} if on else {})]


@pytest.mark.parametrize(
    "mode,field,value",
    [
        (ColorMode.COLOR_TEMP, "color_temp_kelvin", 3100),
        (ColorMode.XY, "xy_color", (0.3, 0.4)),
        (ColorMode.RGB, "rgb_color", (64, 128, 192)),
        (ColorMode.RGBW, "rgbw_color", (64, 128, 192, 32)),
        (ColorMode.RGBWW, "rgbww_color", (64, 128, 192, 32, 16)),
    ],
)
async def test_managed_native_color_modes_publish_source_feedback_after_command(
    hass, tmp_path, mode, field, value
):
    assert await async_setup_component(hass, "light", {})
    raw = PhysicalLight()
    raw._attr_supported_color_modes = {mode}
    raw._attr_color_mode = mode
    setattr(raw, f"_attr_{field}", value)
    await hass.data["light"].async_add_entities([raw])
    entry = await async_setup_operator(hass, tmp_path, resources={"studio": light_config()})
    runtime = entry.runtime_data
    await runtime.async_set_mode("studio", "live")
    managed = next(
        state for state in hass.states.async_all("light") if state.entity_id != raw.entity_id
    )
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": managed.entity_id, field: value}, blocking=True
    )
    await hass.async_block_till_done()
    assert raw.commands == [(True, {field: value})]
    assert hass.states.get(managed.entity_id).state == "off"
    raw._attr_is_on = True
    raw.async_write_ha_state()
    await hass.async_block_till_done()
    feedback = hass.states.get(managed.entity_id)
    assert feedback.state == "on"
    assert feedback.attributes["color_mode"] == mode
    assert feedback.attributes[field] == value
    assert runtime.decisions["studio"].status == "satisfied"
