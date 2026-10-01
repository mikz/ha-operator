"""Physical simulator regressions; assertions do not use the operator engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.lab.sim.model import CommandError, Simulator

LAB_COMPONENTS = Path(__file__).resolve().parents[1] / "lab" / "custom_components"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def sim(tmp_path):
    clock = Clock()
    simulator = Simulator(
        monotonic=clock,
        wall_time=lambda: 1700000000 + clock.now,
        journal_path=tmp_path / "journal.jsonl",
    )
    return simulator, clock


def observed(simulator, device_id):
    return next(d["observable"] for d in simulator.public()["devices"] if d["id"] == device_id)


def test_hidden_rain_refuses_without_exposing_reason(sim):
    simulator, clock = sim
    simulator.control("skylight", {"hidden_rain": True})
    receipt = simulator.command("skylight", {"action": "set_position", "position": 80})
    assert receipt["accepted"] is True
    clock.advance(30)
    assert observed(simulator, "skylight")["position"] == 0
    assert "rain" not in json.dumps(simulator.public())
    assert any(event["kind"] == "refusal" for event in simulator.events)
    # Clearing rain emits no HA signal and cannot replay the ignored opening.
    simulator.control("skylight", {"hidden_rain": False})
    clock.advance(30)
    assert observed(simulator, "skylight")["position"] == 0
    simulator.command("skylight", {"action": "set_position", "position": 80})
    clock.advance(1)
    assert observed(simulator, "skylight")["position"] == 25
    clock.advance(3)
    assert observed(simulator, "skylight")["position"] == 80


def test_gradual_movement_stop_and_delayed_telemetry(sim):
    simulator, clock = sim
    simulator.control("skylight", {"telemetry_delay": 2, "quantization": 10})
    simulator.command("skylight", {"action": "open"})
    clock.advance(0.5)
    simulator.tick()
    assert simulator.devices["skylight"].physical["position"] == 12.5
    assert observed(simulator, "skylight")["position"] == 0
    simulator.command("skylight", {"action": "stop"})
    clock.advance(3)
    state = observed(simulator, "skylight")
    assert state["position"] == 10
    assert state["moving"] is False
    assert simulator.devices["skylight"].physical["position"] == 12.5


def test_hidden_rain_autonomously_closes_moving_window(sim):
    simulator, clock = sim
    simulator.command("skylight", {"action": "open"})
    clock.advance(2)
    assert observed(simulator, "skylight")["position"] == 50
    simulator.control("skylight", {"hidden_rain": True})
    assert observed(simulator, "skylight")["motion"] == "closing"
    clock.advance(2)
    assert observed(simulator, "skylight")["position"] == 0
    assert any(event["data"].get("action") == "autonomous_close" for event in simulator.events)


def test_stop_capability_and_unavailable_signals(sim):
    simulator, _ = sim
    device = simulator.devices["stopped_unsupported"]
    assert device.descriptor()["supports_stop"] is False
    with pytest.raises(CommandError, match="STOP not supported"):
        simulator.command(device.id, {"action": "stop"})
    simulator.control("skylight", {"available": False})
    assert observed(simulator, "skylight")["available"] is False
    with pytest.raises(CommandError, match="unavailable"):
        simulator.command("skylight", {"action": "open"})


def test_relay_conflicts_are_journalled_before_rejection(sim):
    simulator, _ = sim
    simulator.command("inward_relay", {"action": "turn_on"})
    with pytest.raises(CommandError, match="Conflicting relay"):
        simulator.command("outward_relay", {"action": "turn_on"})
    assert observed(simulator, "outward_relay")["on"] is False
    violations = [event for event in simulator.events if event["kind"] == "unsafe_command"]
    assert violations[0]["data"]["conflicts"] == ["inward_relay"]
    simulator.command("inward_relay", {"action": "turn_off"})
    simulator.command("outward_relay", {"action": "turn_on"})
    effects = [
        (event["device_id"], event["data"].get("on"))
        for event in simulator.events
        if event["kind"] == "effect" and "on" in event["data"]
    ]
    assert effects == [("inward_relay", True), ("inward_relay", False), ("outward_relay", True)]


def test_airflow_depends_on_physical_capacity_and_extraction(sim):
    simulator, clock = sim
    simulator.control("extraction", {"value": True})
    simulator.command("skylight", {"action": "open"})
    assert observed(simulator, "airflow")["value"] == 0
    clock.advance(2)
    assert observed(simulator, "airflow")["value"] == 50
    simulator.control("skylight", {"hidden_rain": True})
    clock.advance(2)
    assert observed(simulator, "airflow")["value"] == 0
    simulator.control("passive_window", {"value": True})
    assert observed(simulator, "airflow")["value"] == 100
    simulator.control("extraction", {"value": False})
    assert observed(simulator, "airflow")["value"] == 0


def test_fan_direction_while_off_and_discrete_speed(sim):
    simulator, _ = sim
    simulator.command("exhaust", {"action": "set_direction", "direction": "reverse"})
    assert observed(simulator, "exhaust")["on"] is False
    simulator.command("exhaust", {"action": "set_percentage", "percentage": 1})
    assert observed(simulator, "exhaust")["percentage"] == pytest.approx(100 / 3)
    simulator.command("exhaust", {"action": "turn_off"})
    assert observed(simulator, "exhaust")["direction"] == "reverse"


@pytest.mark.parametrize("device_id", ["cellar_fan", "cellar_low_relay"])
def test_configured_refusal_accepts_receipt_without_effect_or_replay(sim, device_id):
    simulator, clock = sim
    simulator.control(device_id, {"refuse_actions": ["turn_on"]})
    before = simulator.sequence
    receipt = simulator.command(device_id, {"action": "turn_on"})
    assert receipt["accepted"] is True
    assert simulator.devices[device_id].physical["on"] is False
    assert observed(simulator, device_id)["on"] is False
    events = [event for event in simulator.events if event["seq"] > before]
    assert [event["kind"] for event in events] == ["command", "refusal"]
    assert events[0]["seq"] == receipt["command_seq"]
    assert events[1]["data"] == {"action": "turn_on", "reason": "configured_refusal"}
    simulator.control(device_id, {"refuse_actions": []})
    clock.advance(5)
    assert observed(simulator, device_id)["on"] is False
    simulator.command(device_id, {"action": "turn_on"})
    assert observed(simulator, device_id)["on"] is True


def test_refused_direction_preserves_physical_truth_and_still_validates_commands(sim):
    simulator, _ = sim
    simulator.command("cellar_fan", {"action": "turn_on", "percentage": 100})
    simulator.control("cellar_fan", {"refuse_actions": ["set_direction", "set_percentage"]})
    receipt = simulator.command("cellar_fan", {"action": "set_direction", "direction": "reverse"})
    assert receipt["accepted"] is True
    assert simulator.devices["cellar_fan"].physical["direction"] == "forward"
    assert observed(simulator, "cellar_fan")["direction"] == "forward"
    with pytest.raises(CommandError, match="direction"):
        simulator.command("cellar_fan", {"action": "set_direction", "direction": "sideways"})
    with pytest.raises(CommandError, match="finite"):
        simulator.command("cellar_fan", {"action": "set_percentage", "percentage": 101})
    simulator.control("cellar_fan", {"available": False})
    with pytest.raises(CommandError, match="unavailable"):
        simulator.command("cellar_fan", {"action": "set_direction", "direction": "reverse"})


@pytest.mark.parametrize("device_id", ["cellar_fan", "cellar_low_relay"])
def test_suppressed_off_feedback_retains_on_until_fault_clears_with_delay(sim, device_id):
    simulator, clock = sim
    simulator.command(device_id, {"action": "turn_on"})
    assert observed(simulator, device_id)["on"] is True
    simulator.control(device_id, {"suppress_off_feedback": True, "telemetry_delay": 2})
    before = simulator.sequence
    simulator.command(device_id, {"action": "turn_off"})
    assert simulator.devices[device_id].physical["on"] is False
    clock.advance(3)
    assert observed(simulator, device_id)["on"] is True
    if device_id == "cellar_fan":
        assert simulator.devices[device_id].physical["percentage"] == 0
        assert observed(simulator, device_id)["percentage"] == 100
    events = [
        event
        for event in simulator.events
        if event["seq"] > before and event["device_id"] == device_id
    ]
    assert any(event["kind"] == "effect" and event["data"].get("on") is False for event in events)
    assert not any(event["kind"] == "feedback" for event in events)
    simulator.control(device_id, {"suppress_off_feedback": False})
    clock.advance(1.9)
    assert observed(simulator, device_id)["on"] is True
    clock.advance(0.1)
    assert observed(simulator, device_id)["on"] is False
    feedback = [
        event
        for event in simulator.events
        if event["kind"] == "feedback" and event["device_id"] == device_id
    ]
    assert feedback[-1]["data"]["observation"]["on"] is False


@pytest.mark.parametrize("on", [False, True])
def test_unknown_direction_changes_observation_only(sim, on):
    simulator, _ = sim
    simulator.control("extraction", {"value": True})
    simulator.command("cellar_fan", {"action": "set_direction", "direction": "forward"})
    if on:
        simulator.command("cellar_fan", {"action": "turn_on", "percentage": 100})
    simulator.control("cellar_fan", {"unknown_direction": True})
    state = observed(simulator, "cellar_fan")
    assert state["direction"] is None
    assert state["on"] is on
    assert state["available"] is True
    assert simulator.devices["cellar_fan"].physical["direction"] == "forward"
    assert observed(simulator, "airflow")["value"] == (100 if on else 0)
    simulator.control("cellar_fan", {"unknown_direction": False})
    assert observed(simulator, "cellar_fan")["direction"] == "forward"


def test_derived_virtual_on_is_physical_and_can_have_independent_feedback_fault(sim):
    simulator, _ = sim
    simulator.command("cellar_fan", {"action": "turn_on"})
    assert observed(simulator, "cellar_on")["value"] is True
    simulator.control("cellar_fan", {"suppress_off_feedback": True})
    simulator.command("cellar_fan", {"action": "turn_off"})
    assert observed(simulator, "cellar_fan")["on"] is True
    assert observed(simulator, "cellar_on")["value"] is False
    simulator.command("cellar_fan", {"action": "turn_on"})
    simulator.control("cellar_on", {"suppress_off_feedback": True})
    simulator.command("cellar_fan", {"action": "turn_off"})
    assert simulator.devices["cellar_on"].physical["value"] is False
    assert observed(simulator, "cellar_on")["value"] is True
    simulator.control("cellar_on", {"suppress_off_feedback": False})
    assert observed(simulator, "cellar_on")["value"] is False


@pytest.mark.parametrize("prefix", ["", "cellar_"])
def test_direction_relay_requires_actual_power_for_airflow(sim, prefix):
    simulator, _ = sim
    simulator.control("extraction", {"value": True})
    direction, power = f"{prefix}inward_relay", f"{prefix}low_relay"
    simulator.command(direction, {"action": "turn_on"})
    assert observed(simulator, direction)["on"] is True
    assert observed(simulator, "airflow")["value"] == 0
    simulator.command(power, {"action": "turn_on"})
    assert observed(simulator, "airflow")["value"] == 100
    simulator.control(power, {"suppress_off_feedback": True})
    simulator.command(power, {"action": "turn_off"})
    assert observed(simulator, power)["on"] is True
    assert simulator.devices[power].physical["on"] is False
    assert observed(simulator, "airflow")["value"] == 0


def test_cellar_relays_have_independent_interlock_groups(sim):
    simulator, _ = sim
    for device_id in ("low_relay", "inward_relay", "cellar_low_relay", "cellar_inward_relay"):
        simulator.command(device_id, {"action": "turn_on"})
    assert not any(event["kind"] == "unsafe_command" for event in simulator.events)
    for device_id in ("cellar_high_relay", "cellar_outward_relay"):
        with pytest.raises(CommandError, match="Conflicting relay"):
            simulator.command(device_id, {"action": "turn_on"})


def test_feedback_journal_records_publication_after_physical_effect(sim):
    simulator, clock = sim
    simulator.control("cellar_fan", {"telemetry_delay": 2})
    before = simulator.sequence
    simulator.command("cellar_fan", {"action": "turn_on"})
    assert simulator.devices["cellar_fan"].physical["on"] is True
    assert observed(simulator, "cellar_fan")["on"] is False
    clock.advance(2)
    assert observed(simulator, "cellar_fan")["on"] is True
    events = [
        event
        for event in simulator.events
        if event["seq"] > before and event["device_id"] == "cellar_fan"
    ]
    assert [event["kind"] for event in events] == ["command", "effect", "feedback"]
    assert events[2]["monotonic"] - events[1]["monotonic"] == 2
    assert events[2]["data"]["observation"]["on"] is True
    simulator.command("cellar_fan", {"action": "turn_off"})
    clock.advance(2)
    observed(simulator, "cellar_fan")
    assert events[2]["data"]["observation"]["on"] is True, "Earlier journal rows were mutated"


@pytest.mark.parametrize(
    "patch",
    [
        {"refuse_actions": "turn_on"},
        {"refuse_actions": ["turn_on", "turn_on"]},
        {"refuse_actions": ["open"]},
        {"suppress_off_feedback": "false"},
        {"unknown_direction": 1},
    ],
)
def test_reject_invalid_fault_controls_atomically(sim, patch):
    simulator, _ = sim
    previous = dict(simulator.devices["cellar_fan"].controls)
    with pytest.raises(ValueError):
        simulator.control("cellar_fan", {"available": False, **patch})
    assert simulator.devices["cellar_fan"].controls == previous


def test_reject_invalid_physical_dependencies_without_replacing_fixture(sim):
    simulator, _ = sim
    with pytest.raises(ValueError, match="airflow_requires_any"):
        simulator.reset(
            [{"id": "direction", "kind": "switch", "airflow_requires_any": ["missing"]}]
        )
    with pytest.raises(ValueError, match="derived on"):
        simulator.reset(
            [{"id": "virtual", "kind": "binary_sensor", "derived": "on", "source": "missing"}]
        )
    assert "cellar_fan" in simulator.devices


def test_reset_preserves_instance_and_append_only_journal(sim):
    simulator, clock = sim
    instance = simulator.instance_id
    simulator.command("skylight", {"action": "open"})
    previous = list(simulator.events)
    clock.advance(1)
    simulator.reset()
    assert simulator.instance_id == instance
    assert simulator.events[: len(previous)] == previous
    journal = [json.loads(line) for line in simulator.journal_path.read_text().splitlines()]
    assert journal == simulator.events
    assert [event["seq"] for event in journal] == list(range(1, len(journal) + 1))
    assert observed(simulator, "skylight")["position"] == 0
    # Process replacement is distinguishable and never rewrites previous evidence.
    replacement = Simulator(journal_path=simulator.journal_path)
    assert replacement.instance_id != instance
    assert replacement.events[: len(journal)] == journal


def test_reject_malformed_commands_and_fixture_ids(sim):
    simulator, _ = sim
    with pytest.raises(CommandError, match="finite"):
        simulator.command("skylight", {"action": "set_position", "position": float("nan")})
    with pytest.raises(ValueError, match="Duplicate"):
        simulator.reset([{"id": "same", "kind": "cover"}, {"id": "same", "kind": "cover"}])
    with pytest.raises(ValueError, match="Device IDs"):
        simulator.reset([{"id": "../bad", "kind": "cover"}])
    assert "skylight" in simulator.devices


async def test_http_boundary_hides_controls_and_records_refusal(
    sim, aiohttp_client, socket_enabled
):
    from tests.lab.sim.server import create_app

    simulator, clock = sim
    client = await aiohttp_client(create_app(simulator))
    health = await (await client.get("/health")).json()
    assert health["instance_id"] == simulator.instance_id
    await client.post("/admin/devices/skylight", json={"hidden_rain": True})
    response = await client.post(
        "/devices/skylight/command", json={"action": "set_position", "position": 70}
    )
    assert response.status == 200
    clock.advance(5)
    descriptor = await (await client.get("/devices/skylight")).json()
    assert descriptor["observable"]["position"] == 0
    assert "hidden_rain" not in json.dumps(descriptor)
    evidence = await (await client.get("/admin/journal?after=0")).json()
    assert any(event["kind"] == "refusal" for event in evidence["events"])
    response = await client.post("/devices/missing/command", json={"action": "open"})
    assert response.status == 404
    response = await client.post(
        "/devices/skylight/command", json={"action": "set_position", "position": 120}
    )
    assert response.status == 409


async def test_http_fault_contract_keeps_controls_private(sim, aiohttp_client, socket_enabled):
    from tests.lab.sim.server import create_app

    simulator, _ = sim
    client = await aiohttp_client(create_app(simulator))
    response = await client.patch(
        "/admin/devices/cellar_fan",
        json={
            "refuse_actions": ["turn_on"],
            "suppress_off_feedback": True,
            "unknown_direction": True,
        },
    )
    assert response.status == 200
    response = await client.post("/devices/cellar_fan/command", json={"action": "turn_on"})
    assert response.status == 200
    assert (await response.json())["accepted"] is True
    descriptor = await (await client.get("/devices/cellar_fan")).json()
    assert descriptor["observable"]["on"] is False
    assert descriptor["observable"]["direction"] is None
    public = await (await client.get("/devices")).json()
    for field in (
        "controls",
        "refuse_actions",
        "suppress_off_feedback",
        "unknown_direction",
        "source",
        "derived",
        "airflow_requires_any",
    ):
        assert f'"{field}"' not in json.dumps(public)
    private = await (await client.get("/admin/state")).json()
    cellar = next(device for device in private["devices"] if device["id"] == "cellar_fan")
    assert cellar["physical"]["direction"] == "forward"
    assert cellar["controls"]["refuse_actions"] == ["turn_on"]
    journal = await (await client.get("/admin/journal")).json()
    assert any(
        event["kind"] == "refusal" and event["device_id"] == "cellar_fan"
        for event in journal["events"]
    )
    assert any(
        event["kind"] == "feedback" and event["device_id"] == "cellar_fan"
        for event in journal["events"]
    )


def test_native_ha_bridge_poll_interval_defaults_and_rejects_subsecond():
    import voluptuous as vol

    from tests.lab.custom_components.ha_operator_sim import CONFIG_SCHEMA

    settings = {"url": "http://simulator:8099"}
    assert CONFIG_SCHEMA({"ha_operator_sim": settings})["ha_operator_sim"]["poll_interval"] == 1
    for interval in (0.05, 0.25, 0.999):
        with pytest.raises(vol.Invalid):
            CONFIG_SCHEMA({"ha_operator_sim": {**settings, "poll_interval": interval}})
    assert (
        CONFIG_SCHEMA({"ha_operator_sim": {**settings, "poll_interval": 1}})["ha_operator_sim"][
            "poll_interval"
        ]
        == 1
    )


async def test_native_ha_bridge_reports_feedback_not_receipts(
    sim, aiohttp_client, hass, monkeypatch, socket_enabled
):
    from homeassistant.setup import async_setup_component

    import custom_components
    from tests.lab.sim.server import create_app

    simulator, clock = sim
    client = await aiohttp_client(create_app(simulator))
    monkeypatch.setattr(
        custom_components, "__path__", [*custom_components.__path__, str(LAB_COMPONENTS)]
    )
    url = str(client.make_url("/"))
    assert await async_setup_component(hass, "ha_operator_sim", {"ha_operator_sim": {"url": url}})
    await hass.async_block_till_done()
    coordinator = hass.data["ha_operator_sim"]
    try:
        assert hass.states.get("cover.sim_skylight").attributes["current_position"] == 0
        assert hass.states.get("fan.sim_exhaust").state == "off"
        assert hass.states.get("fan.sim_cellar_fan").state == "off"
        assert hass.states.get("binary_sensor.sim_cellar_on").state == "off"
        assert hass.states.get("binary_sensor.sim_passive_window").state == "off"
        simulator.control("skylight", {"hidden_rain": True})
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": "cover.sim_skylight", "position": 80},
            blocking=True,
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("cover.sim_skylight").attributes["current_position"] == 0
        simulator.control("skylight", {"hidden_rain": False})
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": "cover.sim_skylight", "position": 80},
            blocking=True,
        )
        clock.advance(1)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("cover.sim_skylight").attributes["current_position"] == 25
        assert hass.states.get("cover.sim_skylight").state == "opening"
        await hass.services.async_call(
            "cover",
            "stop_cover",
            {"entity_id": "cover.sim_skylight"},
            blocking=True,
        )
        clock.advance(4)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("cover.sim_skylight").attributes["current_position"] == 25
        assert hass.states.get("cover.sim_skylight").state == "open"
        simulator.control("skylight", {"available": False})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("cover.sim_skylight").state == "unavailable"
        simulator.control("cellar_fan", {"unknown_direction": True})
        await hass.services.async_call(
            "fan",
            "turn_on",
            {"entity_id": "fan.sim_cellar_fan", "percentage": 100},
            blocking=True,
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("fan.sim_cellar_fan").state == "on"
        assert hass.states.get("fan.sim_cellar_fan").attributes.get("direction") is None
        assert hass.states.get("binary_sensor.sim_cellar_on").state == "on"
        simulator.control("cellar_fan", {"suppress_off_feedback": True})
        await hass.services.async_call(
            "fan",
            "turn_off",
            {"entity_id": "fan.sim_cellar_fan"},
            blocking=True,
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert simulator.devices["cellar_fan"].physical["on"] is False
        assert hass.states.get("fan.sim_cellar_fan").state == "on"
        assert hass.states.get("binary_sensor.sim_cellar_on").state == "off"
    finally:
        await coordinator.async_shutdown()


def test_optional_sensor_unit_metadata_is_independent_of_values_and_controls(sim):
    simulator, clock = sim
    device = next(
        item for item in simulator.public()["devices"] if item["id"] == "window_temperature"
    )
    assert device["unit"] == "°C"
    assert device["observable"]["value"] == 20
    assert "unit" not in simulator.devices["window_temperature"].physical
    simulator.control("window_temperature", {"value": 15})
    clock.advance(0.25)
    simulator.tick()
    device = next(
        item for item in simulator.public()["devices"] if item["id"] == "window_temperature"
    )
    assert device["unit"] == "°C"
    assert device["observable"]["value"] == 15
    assert any(
        event["kind"] == "feedback"
        and event["device_id"] == "window_temperature"
        and event["data"]["observation"]["value"] == 15
        for event in simulator.events
    )
    with pytest.raises(ValueError, match="Unknown scenario controls"):
        simulator.control("window_temperature", {"unit": "K"})


@pytest.mark.parametrize(
    "spec",
    [
        {"id": "bad", "kind": "cover", "unit": "°C"},
        {"id": "bad", "kind": "sensor", "unit": ""},
        {"id": "bad", "kind": "sensor", "unit": 42},
    ],
)
def test_invalid_sensor_metadata_is_rejected_before_reset(sim, spec):
    simulator, _ = sim
    with pytest.raises(ValueError, match="unit"):
        simulator.reset([spec])
    assert "window_temperature" in simulator.devices
