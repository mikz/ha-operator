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
    finally:
        await coordinator.async_shutdown()
