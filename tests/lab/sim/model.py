"""Deterministic physical device model used by the isolated acceptance lab.

Nothing in this package imports Home Assistant or ha_operator. Commands set physical
objectives; time advances motion and eventually publishes independent telemetry.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ACTIONS = {
    "cover": {"open", "close", "set_position", "stop"},
    "switch": {"turn_on", "turn_off"},
    "fan": {"turn_on", "turn_off", "set_percentage", "set_direction"},
    "binary_sensor": set(),
    "sensor": set(),
}


class CommandError(ValueError):
    """A malformed command or conflicting physical output."""


@dataclass
class Device:
    """Simulator-owned state; only ``descriptor`` crosses the device API boundary."""

    id: str
    kind: str
    name: str
    physical: dict[str, Any]
    controls: dict[str, Any]
    supports_stop: bool = True
    speed_count: int = 3
    exclusive_group: str | None = None
    airflow_role: str | None = None
    airflow_requires_any: tuple[str, ...] = ()
    observable: dict[str, Any] = field(default_factory=dict)
    pending: deque[tuple[float, dict[str, Any]]] = field(default_factory=deque)
    sampled: dict[str, Any] = field(default_factory=dict)
    retained_on: dict[str, Any] | None = None

    def descriptor(self) -> dict[str, Any]:
        """Expose raw feedback without scenario controls, targets, or hidden rain."""
        result = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "observable": deepcopy(self.observable),
        }
        if self.kind == "cover":
            result["supports_stop"] = self.supports_stop
        if self.kind == "fan":
            result["speed_count"] = self.speed_count
            result["supports_direction"] = True
        return result


def default_devices() -> list[dict[str, Any]]:
    """Sanitized topology with no address or identifier from the real household."""
    return [
        {"id": "skylight", "kind": "cover", "position": 0, "airflow_role": "inlet"},
        {"id": "inlet", "kind": "cover", "position": 0, "airflow_role": "inlet"},
        {"id": "stopped_unsupported", "kind": "cover", "supports_stop": False},
        {"id": "exhaust", "kind": "fan", "airflow_role": "extractor"},
        {"id": "low_relay", "kind": "switch", "exclusive_group": "speed"},
        {"id": "high_relay", "kind": "switch", "exclusive_group": "speed"},
        {
            "id": "inward_relay",
            "kind": "switch",
            "exclusive_group": "direction",
            "airflow_role": "inlet",
            "airflow_requires_any": ["low_relay", "high_relay"],
        },
        {
            "id": "outward_relay",
            "kind": "switch",
            "exclusive_group": "direction",
            "airflow_role": "extractor",
            "airflow_requires_any": ["low_relay", "high_relay"],
        },
        {"id": "cellar_fan", "kind": "fan", "airflow_role": "inlet"},
        {"id": "cellar_on", "kind": "binary_sensor", "derived": "on", "source": "cellar_fan"},
        {"id": "cellar_inlet", "kind": "cover", "airflow_role": "inlet"},
        {"id": "cellar_demand", "kind": "binary_sensor"},
        {"id": "cellar_policy", "kind": "binary_sensor"},
        {"id": "cellar_target", "kind": "sensor", "value": 100},
        {"id": "cellar_low_relay", "kind": "switch", "exclusive_group": "cellar_speed"},
        {"id": "cellar_high_relay", "kind": "switch", "exclusive_group": "cellar_speed"},
        {
            "id": "cellar_inward_relay",
            "kind": "switch",
            "exclusive_group": "cellar_direction",
            "airflow_role": "inlet",
            "airflow_requires_any": ["cellar_low_relay", "cellar_high_relay"],
        },
        {
            "id": "cellar_outward_relay",
            "kind": "switch",
            "exclusive_group": "cellar_direction",
            "airflow_role": "extractor",
            "airflow_requires_any": ["cellar_low_relay", "cellar_high_relay"],
        },
        {"id": "passive_window", "kind": "binary_sensor", "airflow_role": "inlet"},
        {"id": "fireplace", "kind": "binary_sensor"},
        {"id": "extraction", "kind": "binary_sensor", "airflow_role": "extractor"},
        {"id": "demand", "kind": "binary_sensor"},
        {"id": "airflow", "kind": "sensor", "derived": "airflow"},
    ]


class Simulator:
    """Physical dynamics, observation delay, and independent sequenced evidence."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        journal_path: Path | None = None,
    ) -> None:
        self.clock = monotonic
        self.wall_time = wall_time
        self.instance_id = str(uuid.uuid4())
        self.journal_path = journal_path
        self.events: list[dict[str, Any]] = []
        self.sequence = 0
        if journal_path is not None:
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            if journal_path.exists():
                for line in journal_path.read_text().splitlines():
                    item = json.loads(line)
                    self.events.append(item)
                    self.sequence = max(self.sequence, item["seq"])
        self.devices: dict[str, Device] = {}
        self.last_tick = self.clock()
        self.reset()

    def record(self, kind: str, device_id: str | None, **data: Any) -> dict[str, Any]:
        self.sequence += 1
        event = {
            "seq": self.sequence,
            "instance_id": self.instance_id,
            "time": self.wall_time(),
            "monotonic": self.clock(),
            "kind": kind,
            "device_id": device_id,
            "data": data,
        }
        if self.journal_path is not None:
            with self.journal_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")
                stream.flush()
        self.events.append(event)
        return event

    def reset(self, definitions: list[dict[str, Any]] | None = None) -> None:
        """Reset physical fixtures without losing instance identity or journal order."""
        candidates: dict[str, Device] = {}
        for spec in default_devices() if definitions is None else definitions:
            device_id = spec["id"]
            if not isinstance(device_id, str) or not device_id.replace("_", "").isalnum():
                raise ValueError("Device IDs must contain only letters, numbers and underscores")
            if device_id in candidates:
                raise ValueError(f"Duplicate device: {device_id}")
            kind = spec["kind"]
            if kind not in {"cover", "switch", "fan", "binary_sensor", "sensor"}:
                raise ValueError(f"Unknown device kind: {kind}")
            position = self._percentage(spec.get("position", 0))
            percentage = self._percentage(spec.get("percentage", 0))
            physical = {
                "position": position,
                "target": None,
                "moving": False,
                "on": bool(spec.get("on", percentage > 0)),
                "percentage": percentage,
                "direction": spec.get("direction", "forward"),
                "value": spec.get("value", False if kind == "binary_sensor" else 0),
            }
            controls = {
                "hidden_rain": bool(spec.get("hidden_rain", False)),
                "rain_autoclose": bool(spec.get("rain_autoclose", True)),
                "available": bool(spec.get("available", True)),
                "telemetry_delay": max(0.0, float(spec.get("telemetry_delay", 0))),
                "quantization": max(0.0, float(spec.get("quantization", 1))),
                "speed": max(0.01, float(spec.get("speed", 25))),
                "stuck": bool(spec.get("stuck", False)),
                "refuse_actions": self._refuse_actions(spec.get("refuse_actions", []), kind),
                "suppress_off_feedback": self._boolean(spec.get("suppress_off_feedback", False)),
                "unknown_direction": self._boolean(spec.get("unknown_direction", False)),
                "derived": spec.get("derived"),
                "source": spec.get("source"),
            }
            dependencies = spec.get("airflow_requires_any", [])
            if (
                not isinstance(dependencies, list)
                or any(not isinstance(item, str) for item in dependencies)
                or len(set(dependencies)) != len(dependencies)
            ):
                raise ValueError("airflow_requires_any must be a list of unique device IDs")
            device = Device(
                id=device_id,
                kind=kind,
                name=spec.get("name", device_id.replace("_", " ")),
                physical=physical,
                controls=controls,
                supports_stop=bool(spec.get("supports_stop", True)),
                speed_count=max(1, int(spec.get("speed_count", 3))),
                exclusive_group=spec.get("exclusive_group"),
                airflow_role=spec.get("airflow_role"),
                airflow_requires_any=tuple(dependencies),
            )
            if controls["hidden_rain"] and controls["rain_autoclose"] and position > 0:
                physical.update(target=0.0, moving=True)
            candidates[device_id] = device
        for device in candidates.values():
            if any(
                source == device.id
                or source not in candidates
                or candidates[source].kind != "switch"
                for source in device.airflow_requires_any
            ):
                raise ValueError("airflow_requires_any must reference other switch devices")
            derived, source = device.controls["derived"], device.controls["source"]
            if derived == "on" and (
                device.kind != "binary_sensor"
                or source not in candidates
                or candidates[source].kind not in {"fan", "switch"}
            ):
                raise ValueError("derived on requires a binary sensor and a fan or switch source")
            if derived not in {None, "on", "airflow"} or (source is not None and derived != "on"):
                raise ValueError("Unsupported derived observation or source")
        self.devices = candidates
        self.last_tick = self.clock()
        self.record("reset", None, devices=list(candidates))
        self._derive_signals()
        for device in self.devices.values():
            self._sample(device, immediate=True)

    @staticmethod
    def _percentage(value: Any) -> float:
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise CommandError("Percentage/position must be finite and between 0 and 100")
        return value

    @staticmethod
    def _boolean(value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("Fault flags must be true or false")
        return value

    @staticmethod
    def _refuse_actions(value: Any, kind: str) -> list[str]:
        if (
            not isinstance(value, list)
            or any(not isinstance(action, str) or action not in _ACTIONS[kind] for action in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("refuse_actions must list unique supported device actions")
        return list(value)

    def _sample(self, device: Device, *, immediate: bool = False) -> None:
        state: dict[str, Any] = {"available": device.controls["available"]}
        if device.kind == "cover":
            position = device.physical["position"]
            quantum = device.controls["quantization"]
            if quantum:
                position = min(100.0, max(0.0, round(position / quantum) * quantum))
            state.update(position=position, moving=device.physical["moving"])
            # Movement direction is physical telemetry, not the requested endpoint.
            if device.physical["moving"]:
                state["motion"] = (
                    "opening"
                    if device.physical["target"] > device.physical["position"]
                    else "closing"
                )
        elif device.kind == "switch":
            state["on"] = device.physical["on"]
        elif device.kind == "fan":
            state.update({key: device.physical[key] for key in ("on", "percentage", "direction")})
        else:
            state["value"] = device.physical["value"]
        signal = "on" if device.kind in {"fan", "switch"} else "value"
        if device.kind in {"fan", "switch", "binary_sensor"}:
            if state[signal] is True:
                device.retained_on = deepcopy(state)
            elif not device.controls["suppress_off_feedback"]:
                device.retained_on = None
            elif device.retained_on is not None:
                state[signal] = True
                if device.kind == "fan":
                    state["percentage"] = device.retained_on["percentage"]
        if device.kind == "fan" and device.controls["unknown_direction"]:
            state["direction"] = None
        if not immediate and state == device.sampled:
            return
        device.sampled = deepcopy(state)
        state["observed_at"] = self.wall_time()
        if immediate:
            self._publish(device, state)
        else:
            device.pending.append((self.clock() + device.controls["telemetry_delay"], state))

    def _publish(self, device: Device, state: dict[str, Any]) -> None:
        device.observable = state
        self.record("feedback", device.id, observation=deepcopy(state))

    def tick(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self.last_tick)
        self.last_tick = now
        for device in self.devices.values():
            physical, controls = device.physical, device.controls
            if device.kind == "cover" and physical["moving"] and not controls["stuck"]:
                old = physical["position"]
                target = physical["target"]
                delta = controls["speed"] * elapsed
                position = min(old + delta, target) if target > old else max(old - delta, target)
                physical["position"] = position
                if position == target:
                    physical["target"] = None
                    physical["moving"] = False
                if position != old:
                    self.record(
                        "effect",
                        device.id,
                        action="position",
                        position=position,
                        moving=physical["moving"],
                    )
        self._derive_signals()
        for device in self.devices.values():
            self._sample(device)
            while device.pending and device.pending[0][0] <= now:
                _, state = device.pending.popleft()
                self._publish(device, state)

    def _derive_signals(self) -> None:
        for device in self.devices.values():
            if device.controls["derived"] != "on":
                continue
            value = self.devices[device.controls["source"]].physical["on"]
            if device.physical["value"] != value:
                device.physical["value"] = value
                self.record("effect", device.id, action="on", value=value)
        self._derive_airflow()

    def _derive_airflow(self) -> None:
        """A separate physical fact from openness, requests, or service success."""
        inlet_capacity = 0.0
        extraction = 0.0
        for device in self.devices.values():
            if device.airflow_role is None:
                continue
            state = device.physical
            if device.kind == "cover":
                strength = state["position"] / 100
            elif device.kind == "binary_sensor":
                strength = float(bool(state["value"]))
            elif device.kind == "fan":
                strength = state["percentage"] / 100 if state["on"] else 0.0
            else:
                strength = float(state["on"])
            if device.airflow_requires_any and not any(
                self.devices[source].physical["on"] for source in device.airflow_requires_any
            ):
                strength = 0.0
            role = device.airflow_role
            if device.kind == "fan" and state["direction"] == "reverse":
                role = "inlet" if role == "extractor" else "extractor"
            if role == "inlet":
                inlet_capacity += strength
            else:
                extraction += strength
        airflow = round(min(inlet_capacity, extraction) * 100.0, 3)
        for device in self.devices.values():
            if device.controls["derived"] == "airflow" and device.physical["value"] != airflow:
                device.physical["value"] = airflow
                self.record("effect", device.id, action="airflow", value=airflow)

    def public(self) -> dict[str, Any]:
        self.tick()
        return {
            "instance_id": self.instance_id,
            "devices": [device.descriptor() for device in self.devices.values()],
        }

    def inspect(self) -> dict[str, Any]:
        self.tick()
        return {
            "instance_id": self.instance_id,
            "journal_seq": self.sequence,
            "devices": [
                {
                    **device.descriptor(),
                    "physical": deepcopy(device.physical),
                    "controls": deepcopy(device.controls),
                    "airflow_requires_any": list(device.airflow_requires_any),
                }
                for device in self.devices.values()
            ],
        }

    def control(self, device_id: str, patch: dict[str, Any]) -> None:
        """Scenario API: never exposed through a raw HA entity."""
        self.tick()
        device = self.devices[device_id]
        control_keys = {
            "hidden_rain",
            "rain_autoclose",
            "available",
            "telemetry_delay",
            "quantization",
            "speed",
            "stuck",
            "refuse_actions",
            "suppress_off_feedback",
            "unknown_direction",
        }
        physical_keys = {"position", "on", "percentage", "direction", "value"}
        unexpected = patch.keys() - control_keys - physical_keys
        if unexpected:
            raise ValueError(f"Unknown scenario controls: {sorted(unexpected)}")
        patch = deepcopy(patch)
        if "refuse_actions" in patch:
            patch["refuse_actions"] = self._refuse_actions(patch["refuse_actions"], device.kind)
        for key in {"suppress_off_feedback", "unknown_direction"} & patch.keys():
            patch[key] = self._boolean(patch[key])
        for key in {"position", "percentage"} & patch.keys():
            patch[key] = self._percentage(patch[key])
        for key in {"telemetry_delay", "quantization", "speed"} & patch.keys():
            patch[key] = float(patch[key])
            if not math.isfinite(patch[key]) or patch[key] < 0:
                raise ValueError(f"Invalid {key}")
        for key in control_keys & patch.keys():
            device.controls[key] = patch[key]
        for key in physical_keys & patch.keys():
            device.physical[key] = patch[key]
        if "position" in patch:
            device.physical.update(target=None, moving=False)
        if (
            device.kind == "cover"
            and device.controls["hidden_rain"]
            and device.controls["rain_autoclose"]
            and device.physical["position"] > 0
        ):
            device.physical.update(target=0.0, moving=True)
            self.record("effect", device_id, action="autonomous_close", cause="hidden_rain")
        self.record("admin", device_id, changes=patch)
        self.tick()

    def command(self, device_id: str, command: dict[str, Any]) -> dict[str, Any]:
        self.tick()
        device = self.devices[device_id]
        action = command.get("action")
        command_event = self.record("command", device_id, **deepcopy(command))
        if not device.controls["available"]:
            self.record("refusal", device_id, action=action, reason="unavailable")
            raise CommandError("Device unavailable")
        self._validate_command(device, command)
        if action in device.controls["refuse_actions"]:
            self.record("refusal", device_id, action=action, reason="configured_refusal")
        elif device.kind == "cover":
            self._cover_command(device, action, command)
        elif device.kind == "switch":
            self._switch_command(device, action)
        elif device.kind == "fan":
            self._fan_command(device, action, command)
        else:
            raise CommandError("Read-only signal")
        self.tick()
        # Receipt only: callers must acquire telemetry to know physical outcomes.
        return {
            "accepted": True,
            "command_seq": command_event["seq"],
            "instance_id": self.instance_id,
        }

    def _validate_command(self, device: Device, command: dict[str, Any]) -> None:
        action = command.get("action")
        if action not in _ACTIONS[device.kind]:
            raise CommandError("Unsupported device action")
        if action == "set_direction" and command.get("direction") not in {"forward", "reverse"}:
            raise CommandError("Unsupported direction")
        if action == "set_position":
            self._percentage(command.get("position"))
        if device.kind == "fan" and action in {"turn_on", "set_percentage"}:
            self._percentage(command.get("percentage", 100))
        if action == "stop" and not device.supports_stop:
            raise CommandError("STOP not supported")

    def _cover_command(self, device: Device, action: str, command: dict[str, Any]) -> None:
        if action == "stop":
            if not device.supports_stop:
                raise CommandError("STOP not supported")
            device.physical.update(target=None, moving=False)
            self.record("effect", device.id, action="stop", position=device.physical["position"])
            return
        if action not in {"open", "close", "set_position"}:
            raise CommandError("Unsupported cover action")
        target = {"open": 100.0, "close": 0.0}.get(action)
        if target is None:
            target = self._percentage(command["position"])
        if device.controls["hidden_rain"] and target > device.physical["position"]:
            self.record("refusal", device.id, action=action, reason="hidden_rain", target=target)
            return
        if device.controls["stuck"]:
            self.record("refusal", device.id, action=action, reason="stuck", target=target)
            return
        device.physical["target"] = target if target != device.physical["position"] else None
        device.physical["moving"] = target != device.physical["position"]
        self.record(
            "effect",
            device.id,
            action="motion_started",
            target=target,
            position=device.physical["position"],
        )

    def _switch_command(self, device: Device, action: str) -> None:
        if action not in {"turn_on", "turn_off"}:
            raise CommandError("Unsupported switch action")
        on = action == "turn_on"
        if on and device.exclusive_group:
            conflicts = [
                other.id
                for other in self.devices.values()
                if other.id != device.id
                and other.exclusive_group == device.exclusive_group
                and other.physical["on"]
            ]
            if conflicts:
                self.record("unsafe_command", device.id, action=action, conflicts=conflicts)
                raise CommandError("Conflicting relay already energized")
        device.physical["on"] = on
        self.record("effect", device.id, action=action, on=on)

    def _fan_command(self, device: Device, action: str, command: dict[str, Any]) -> None:
        if action == "set_direction":
            if command.get("direction") not in {"forward", "reverse"}:
                raise CommandError("Unsupported direction")
            device.physical["direction"] = command["direction"]
        elif action in {"turn_on", "set_percentage"}:
            percentage = self._percentage(command.get("percentage", 100))
            if percentage:
                percentage = math.ceil(percentage / (100 / device.speed_count)) * (
                    100 / device.speed_count
                )
            device.physical.update(percentage=percentage, on=percentage > 0)
        elif action == "turn_off":
            device.physical.update(percentage=0.0, on=False)
        else:
            raise CommandError("Unsupported fan action")
        self.record(
            "effect",
            device.id,
            action=action,
            **{key: device.physical[key] for key in ("on", "percentage", "direction")},
        )
