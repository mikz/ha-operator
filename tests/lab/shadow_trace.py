"""Offline ingestion of sanitized observation traces, never a physical-effect oracle.

An accepted document is structurally safe to inspect. ``replay_complete`` is a
separate evidence-quality finding; it never means conformance or physical success.
No Home Assistant, Docker, network, or integration imports are required.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_BYTES = 128 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_DEPTH = 32
SETTLE_SECONDS = 1.0
_HEX = r"[0-9a-f]{20}"
_ENTITY = re.compile(rf"[a-z_]+\.shadow_{_HEX}\Z")
_ALIAS = re.compile(
    rf"(?:r_|p_|q_|v_|profile_|attribute_|value_|request_id_|occurrence_id_|"
    rf"context_id_|context_|source_){_HEX}\Z"
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[a-zA-Z0-9.+-]*)\Z")
_UNSAFE_KEY = re.compile(
    r"(?:password|credential|authorization|cookie|token|secret|private_key|LTSK|LTPK|"
    r"api_key|(?:^|_)(?:url|uri|path|hostname|ip_address)(?:$|_))",
    re.IGNORECASE,
)
_ATTRIBUTES = {
    "supported_features",
    "assumed_state",
    "restored",
    "optimistic",
    "current_position",
    "percentage",
    "direction",
    "percentage_step",
    "speed_count",
    "duration",
    "remaining",
    "finishes_at",
    "timestamp",
    "has_date",
    "has_time",
    "next_update",
}
_METADATA_ATTRIBUTES = {"supported_features", "assumed_state", "restored", "optimistic"}
# Dynamic policy fields admitted by configuration.validate_policy.
_POLICY_TARGET_FIELDS = {"position", "on", "percentage", "direction"}
_SAFE_VALUES = {
    "on",
    "off",
    "open",
    "closed",
    "opening",
    "closing",
    "unknown",
    "unavailable",
    "forward",
    "reverse",
    "observe",
    "live",
    "target",
    "hands_off",
    "state",
    "occurrence",
    "cover",
    "fan",
    "relay_fan",
    "switch",
    "position",
    "contact",
    "relay",
    "airflow",
    "eq",
    "gte",
    "lte",
    "pending",
    "applying",
    "waiting",
    "satisfied",
    "idle",
    "fault",
    "restricted",
    "inactive",
    "acquiring",
    "unmet",
    "initializing",
    "service",
    "entity",
    "stop",
    "active",
    "paused",
    "direction",
    "percentage",
    "profile",
    "homekit",
    "bridge_request",
}
_HEALTH = {
    "enabled",
    "healthy",
    "session_id",
    "last_sequence",
    "durable_sequence",
    "queued_records",
    "dropped_records",
    "write_errors",
    "rotations",
    "last_heartbeat",
    "last_write_at",
    "complete",
    "queue_limit",
    "record_bytes_limit",
    "disk_bytes_limit",
    "unclean_previous",
    "history_gap",
}
_EXPORT = {
    "schema",
    "integration_version",
    "config_hash",
    "component_sha256",
    "records",
    "next_after",
    "more",
    "gap",
    "health",
    "through_sequence",
}
_SESSION = {
    "config",
    "intent",
    "timezone",
    "ha_version",
    "integration_version",
    "config_hash",
    "shadow_lock",
    "previous_session_closed",
    "gap_since_previous",
    "component_sha256",
}
_INPUT = {
    "entity_id",
    "event_type",
    "state",
    "attributes",
    "last_changed",
    "last_updated",
    "last_reported",
    "event_context",
    "event_parent_context",
    "old_last_reported",
}
_RESOURCE_CONFIG = {
    "name",
    "kind",
    "entity_id",
    "outputs",
    "profiles",
    "reversal_dead_time",
    "restriction_entity",
    "fault_entity",
    "default_target",
    "retry_interval",
    "command_interval",
    "movement_timeout",
    "tolerance",
    "manual_duration",
}
_POLICY_CONFIG = {
    "name",
    "resource_id",
    "kind",
    "priority",
    "target",
    "eligibility_entity",
    "eligibility_state",
    "target_entity",
    "target_attribute",
    "target_field",
}
_REQUIREMENT_CONFIG = {"name", "activation_entities", "providers", "acquisition_timeout"}
_ENGINE = {
    "now",
    "resources",
    "observations",
    "manuals",
    "policies",
    "occurrences",
    "requirements",
    "requirement_memory",
}
_DECISION = {
    "revision",
    "fault",
    "resources",
    "requirements",
    "engine",
    "engine_result",
    "engine_aliases",
    "shadow_locked",
    "trace",
}
_ADMISSION = {
    "action",
    "resource_id",
    "policy_id",
    "manual",
    "occurrence",
    "mode",
    "enabled",
    "revision",
    "replayed",
}
_DISPATCH = {
    "resource_id",
    "data",
    "status",
    "error_type",
    "domain",
    "service",
    "cancellation_scope",
    "context_id",
    "at",
}
_EXTERNAL = {"source", "entity_id", "service", "value", "context_id", "evidence", "at"}
_FIELDS = (
    _HEALTH
    | _SESSION
    | _INPUT
    | _RESOURCE_CONFIG
    | _POLICY_CONFIG
    | _REQUIREMENT_CONFIG
    | _ENGINE
    | _DECISION
    | _ADMISSION
    | _DISPATCH
    | _EXTERNAL
    | _ATTRIBUTES
    | {
        "selection",
        "source_kind",
        "source_id",
        "source_name",
        "selection_reason",
        "related_entities",
        "active_inputs",
        "position",
        "on",
        "profile",
        "resource_id",
        "policy_id",
        "id",
        "expires_at",
        "request_id",
        "fan_settings",
        "occurrence_id",
        "context_id",
        "source",
        "skipped",
        "modes",
        "policy_enabled",
        "selected_provider",
        "acquiring_provider",
        "acquisition_started",
        "failed_until",
        "available",
        "moving",
        "restriction",
        "reported_at",
        "observation_after",
        "confirmed",
        "eligible",
        "active",
        "expected",
        "observed",
        "value",
        "operator",
        "attribute",
        "evidence",
        "decision",
        "observation",
        "last_command",
        "next_attempt",
        "attempts",
        "reason",
        "candidates",
        "memory",
        "activation_values",
        "decisions",
        "next_evaluation",
        "trace_entities",
        "from",
        "to",
        "lost_records",
        "health",
        "entity_count",
        "entities",
        "at",
    }
)
_SYMBOL_FIELDS = {
    "status",
    "reason",
    "action",
    "kind",
    "mode",
    "service",
    "domain",
    "error_type",
    "cancellation_scope",
    "event_type",
    "fault",
    "restriction",
}


class TraceValidationError(ValueError):
    """Unsafe, malformed, unsupported, or internally contradictory trace."""


@dataclass(frozen=True, slots=True)
class NormalizedTrace:
    records: tuple[dict[str, Any], ...]
    config: dict[str, Any]
    report: dict[str, Any]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TraceValidationError(message)


def _object(value: Any, allowed: set[str], *, required: set[str] | None = None) -> dict:
    _require(type(value) is dict, "Expected an object")
    _require(all(type(key) is str for key in value), "Object keys must be strings")
    _require(set(value) <= allowed, "Unknown trace fields")
    _require((allowed if required is None else required) <= set(value), "Missing trace fields")
    return value


def _integer(value: Any, minimum: int = 0) -> None:
    _require(
        type(value) is int and minimum <= value <= 2**53 - 1,
        "Expected a bounded nonnegative integer",
    )


def _time(value: Any, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    _require(
        type(value) in (int, float) and abs(value) <= 1e20 and math.isfinite(value),
        "Expected a finite epoch",
    )


def _uuid(value: Any) -> None:
    try:
        valid = type(value) is str and str(UUID(value)) == value
    except ValueError, AttributeError:
        valid = False
    _require(valid, "Invalid session UUID")


def _alias(value: Any, prefix: str) -> None:
    _require(
        type(value) is str and re.fullmatch(re.escape(prefix) + _HEX, value) is not None,
        "Invalid sanitized identifier",
    )


def _entity(value: Any) -> None:
    _require(
        type(value) is str and _ENTITY.fullmatch(value) is not None,
        "Entity identifier is not sanitized",
    )


def _json_types(value: Any, depth: int = 0) -> None:
    """Check before serialization, which otherwise coerces integer object keys."""
    _require(depth <= MAX_DEPTH, "Trace nesting limit exceeded")
    if type(value) is dict:
        _require(all(type(key) is str for key in value), "Object keys must be strings")
        for child in value.values():
            _json_types(child, depth + 1)
    elif type(value) is list:
        for child in value:
            _json_types(child, depth + 1)
    elif type(value) in (int, float):
        _require(abs(value) <= 1e20 and math.isfinite(value), "Nonfinite or excessive JSON number")
    else:
        _require(value is None or type(value) in (str, bool), "Non-JSON value")


def _safe_tree(value: Any, key: str = "", depth: int = 0, *, engine: bool = False) -> None:
    _require(depth <= MAX_DEPTH, "Trace nesting limit exceeded")
    if type(value) is dict:
        for field, child in value.items():
            _require(type(field) is str and not _UNSAFE_KEY.search(field), "Private payload key")
            _require(
                field in _FIELDS
                or _ALIAS.fullmatch(field) is not None
                or _ENTITY.fullmatch(field) is not None,
                "Unknown or unsanitized payload key",
            )
            _safe_tree(
                child, field, depth + 1, engine=engine or field in {"engine", "engine_result"}
            )
    elif type(value) is list:
        for child in value:
            _safe_tree(child, key, depth + 1, engine=engine)
    elif value is None or type(value) is bool:
        return
    elif type(value) in (int, float):
        _require(abs(value) <= 1e20 and math.isfinite(value), "Nonfinite or excessive JSON number")
    elif type(value) is str:
        _require(len(value) <= 512, "Trace string limit exceeded")
        if key == "timezone":
            _require(re.fullmatch(r"[A-Za-z0-9_+/-]+", value) is not None, "Invalid timezone")
            try:
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError) as error:
                raise TraceValidationError("Invalid timezone") from error
            return
        _require(
            not any(part in value for part in ("/", "\\", "@", "://", "Bearer ")),
            "URL, path, or credential payload",
        )
        if key == "session_id":
            _uuid(value)
            return
        if key in {"ha_version", "integration_version"}:
            _require(_VERSION.fullmatch(value) is not None, "Invalid version")
            return
        if key in {"config_hash", "component_sha256"}:
            _require(_HASH.fullmatch(value) is not None, "Invalid configuration hash")
            return
        if _ALIAS.fullmatch(value) or _ENTITY.fullmatch(value) or value in _SAFE_VALUES:
            return
        if key in _SYMBOL_FIELDS:
            _require(
                re.fullmatch(r"[A-Za-z][A-Za-z0-9 _;:-]{0,127}", value) is not None,
                "Invalid integration-generated symbol",
            )
            return
        if key == "source":
            policy_prefix, requirement_prefix = ("[pr]", "[qr]") if engine else ("p", "q")
            _require(
                re.fullmatch(
                    rf"(?:policy:(?:{policy_prefix}_{_HEX}|unknown)(?::occurrence_id_{_HEX})?|"
                    rf"requirement:(?:{requirement_prefix}_{_HEX}|unknown):(?:[vr]_{_HEX}|unknown)|"
                    rf"manual:(?:service|entity|stop|source_{_HEX}))",
                    value,
                )
                is not None,
                "Invalid source alias",
            )
            return
        if key in {"attribute", "target_attribute"} and value in _ATTRIBUTES:
            return
        if re.fullmatch(r"\d{1,3}:\d{2}:\d{2}(?:\.\d+)?", value) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
        ):
            return
        try:
            numeric = len(value) <= 64 and math.isfinite(float(value))
        except ValueError:
            numeric = False
        _require(numeric, "Unsanitized string payload")
    else:
        raise TraceValidationError("Non-JSON value")


def _health(value: Any) -> None:
    health = _object(value, _HEALTH)
    _uuid(health["session_id"])
    for key in ("enabled", "healthy", "complete", "unclean_previous", "history_gap"):
        _require(type(health[key]) is bool, "Health flag must be boolean")
    for key in _HEALTH - {
        "enabled",
        "healthy",
        "complete",
        "unclean_previous",
        "history_gap",
        "session_id",
        "last_heartbeat",
        "last_write_at",
    }:
        _integer(health[key])
    for key in ("last_heartbeat", "last_write_at"):
        _time(health[key], nullable=True)
    _require(health["durable_sequence"] <= health["last_sequence"], "Durability exceeds sequence")
    if health["complete"]:
        _require(
            health["enabled"]
            and health["healthy"]
            and not health["unclean_previous"]
            and not health["history_gap"]
            and not any(
                health[key] for key in ("dropped_records", "write_errors", "queued_records")
            )
            and health["last_sequence"] == health["durable_sequence"],
            "Contradictory complete health",
        )


def _configuration(value: Any) -> dict:
    config = _object(value, {"resources", "policies", "requirements", "trace_entities"})
    for group, prefix, fields in (
        ("resources", "r_", _RESOURCE_CONFIG),
        ("policies", "p_", _POLICY_CONFIG),
        ("requirements", "q_", _REQUIREMENT_CONFIG),
    ):
        _require(type(config[group]) is dict, "Configuration collection must be an object")
        for identifier, item in config[group].items():
            _alias(identifier, prefix)
            _object(item, fields, required={"name"})
            _require(item["name"] == identifier, "Configuration name is not its alias")
            for field in (
                "entity_id",
                "restriction_entity",
                "fault_entity",
                "eligibility_entity",
                "target_entity",
            ):
                if item.get(field) is not None:
                    _entity(item[field])
            if group == "resources":
                _require(
                    type(item.get("kind")) is str
                    and item["kind"] in {"cover", "fan", "switch", "relay_fan"},
                    "Invalid resource kind",
                )
            if group == "policies":
                _alias(item.get("resource_id"), "r_")
                _require(item["resource_id"] in config["resources"], "Unknown policy resource")
                if "target_field" in item:
                    _require(
                        type(item["target_field"]) is str
                        and item["target_field"] in _POLICY_TARGET_FIELDS,
                        "Invalid policy target field",
                    )
            for field in ("outputs", "activation_entities"):
                if field in item:
                    _require(type(item[field]) is list, "Expected entity list")
                    for entity in item[field]:
                        _entity(entity)
            if "profiles" in item:
                _require(type(item["profiles"]) is dict, "Expected profile map")
                for profile, data in item["profiles"].items():
                    _alias(profile, "profile_")
                    _object(data, {"outputs", "percentage", "direction"}, required={"outputs"})
                    _require(type(data["outputs"]) is dict, "Expected profile output map")
                    for entity, state in data["outputs"].items():
                        _entity(entity)
                        _require(type(state) is bool, "Relay output must be boolean")
            if "providers" in item:
                _require(type(item["providers"]) is list, "Expected provider list")
                for provider in item["providers"]:
                    _object(
                        provider,
                        {"id", "resource_id", "target", "evidence"},
                        required={"id", "evidence"},
                    )
                    _require(
                        type(provider["id"]) is str
                        and re.fullmatch(rf"[vr]_{_HEX}", provider["id"]) is not None,
                        "Invalid provider alias",
                    )
                    _require(type(provider["evidence"]) is list, "Expected evidence list")
                    for predicate in provider["evidence"]:
                        _object(
                            predicate,
                            {"entity_id", "attribute", "operator", "value", "kind"},
                            required={"entity_id", "operator", "value", "kind"},
                        )
                        _entity(predicate["entity_id"])
    _require(type(config["trace_entities"]) is list, "Expected trace entity list")
    for entity in config["trace_entities"]:
        _entity(entity)
    return config


_MODELS = {
    "target": {"position", "on", "percentage", "direction", "profile"},
    "resource": {"id", "kind", "mode", "tolerance", "fault", "observation_after"},
    "observation": {"target", "available", "moving", "restriction", "reported_at"},
    "manual": {"resource_id", "mode", "target", "expires_at", "request_id", "source"},
    "policy": {"id", "resource_id", "target", "priority", "enabled", "eligible", "kind"},
    "occurrence": {"policy_id", "occurrence_id", "expires_at", "skipped", "target"},
    "requirement": {"id", "active", "providers", "acquisition_timeout", "retry_interval"},
    "provider": {"id", "resource_id", "target", "confirmed", "eligible"},
    "memory": {"selected_provider", "acquiring_provider", "acquisition_started", "failed_until"},
    "decision": {"resource_id", "target", "status", "source", "reason", "candidates"},
    "candidate": {"source", "target", "priority", "eligible", "reason"},
    "requirement_result": {
        "id",
        "status",
        "selected_provider",
        "acquiring_provider",
        "reason",
        "memory",
    },
}


def _model(value: Any, kind: str, *, engine: bool = False) -> None:
    fields = _MODELS[kind] | ({"fan_settings"} if kind == "manual" and not engine else set())
    _object(value, fields, required=_MODELS[kind] if engine and kind != "target" else set())
    for key, child in value.items():
        if key == "fan_settings":
            _object(child, {"direction", "percentage"}, required=set())
            if "direction" in child:
                _require(
                    type(child["direction"]) is str
                    and child["direction"] in {"forward", "reverse"},
                    "Invalid fan direction selection",
                )
            if "percentage" in child:
                _require(
                    type(child["percentage"]) in (int, float) and 0 <= child["percentage"] <= 100,
                    "Invalid fan speed selection",
                )
        elif key == "target" and child is not None:
            _model(child, "target", engine=engine)
        elif key == "memory":
            _model(child, "memory", engine=engine)
        elif key in {"providers", "candidates"}:
            _require(type(child) is list, "Expected model list")
            for item in child:
                _model(item, "provider" if key == "providers" else "candidate", engine=engine)
        elif key == "resource_id" and child is not None:
            _alias(child, "r_")
        elif key == "policy_id":
            _alias(child, "r_" if engine else "p_")
        elif key == "id":
            if kind == "provider":
                _require(
                    type(child) is str and re.fullmatch(rf"[vr]_{_HEX}", child) is not None,
                    "Invalid provider alias",
                )
            else:
                _alias(
                    child,
                    "r_"
                    if engine
                    else {
                        "resource": "r_",
                        "policy": "p_",
                        "requirement": "q_",
                        "requirement_result": "q_",
                    }[kind],
                )
        elif key in {"request_id", "occurrence_id"} and child is not None:
            _alias(child, key + "_")
        elif key in {"expires_at", "reported_at", "observation_after", "acquisition_started"}:
            _time(child, nullable=key != "reported_at")
        elif key in {"available", "moving", "enabled", "eligible", "skipped", "on"}:
            _require(type(child) is bool or key == "on" and child is None, "Invalid model flag")
        elif key in {"active", "confirmed"}:
            _require(child is None or type(child) is bool, "Invalid evidence flag")


def _engine_snapshot(value: Any) -> None:
    _object(value, _ENGINE)
    _time(value["now"])
    for key, kind, prefix in (
        ("resources", "resource", "r_"),
        ("observations", "observation", "r_"),
        ("requirement_memory", "memory", "r_"),
    ):
        _require(type(value[key]) is dict, "Expected engine model map")
        for identifier, item in value[key].items():
            _alias(identifier, prefix)
            _model(item, kind, engine=True)
            if kind == "resource":
                _require(item.get("id") == identifier, "Engine resource identity mismatch")
    for key, kind in (
        ("manuals", "manual"),
        ("policies", "policy"),
        ("occurrences", "occurrence"),
        ("requirements", "requirement"),
    ):
        _require(type(value[key]) is list, "Expected engine model list")
        for item in value[key]:
            _model(item, kind, engine=True)


def _engine_result(value: Any) -> None:
    _object(value, {"decisions", "requirements", "next_evaluation"})
    _time(value["next_evaluation"], nullable=True)
    for key, kind, prefix in (
        ("decisions", "decision", "r_"),
        ("requirements", "requirement_result", "r_"),
    ):
        _require(type(value[key]) is dict, "Expected engine result map")
        for identifier, item in value[key].items():
            _alias(identifier, prefix)
            _model(item, kind, engine=True)


def _intent(value: Any) -> None:
    _object(value, {"manuals", "occurrences", "modes", "policy_enabled"})
    for key, prefix in (("manuals", "r_"), ("modes", "r_"), ("policy_enabled", "p_")):
        _require(type(value[key]) is dict, "Expected intent map")
        for identifier, item in value[key].items():
            _alias(identifier, prefix)
            if key == "manuals":
                _model(item, "manual")
            elif key == "modes":
                _require(type(item) is str and item in {"live", "observe"}, "Invalid resource mode")
            else:
                _require(type(item) is bool, "Invalid policy enabled flag")
    _require(type(value["occurrences"]) is list, "Expected intent occurrence list")
    for item in value["occurrences"]:
        _model(item, "occurrence")


def _explanation(data: dict) -> None:
    for key, prefix in (("resources", "r_"), ("requirements", "q_")):
        _require(type(data[key]) is dict, "Expected explanation map")
        for identifier in data[key]:
            _alias(identifier, prefix)
    for item in data["resources"].values():
        _object(
            item,
            {
                "mode",
                "decision",
                "observation",
                "manual",
                "last_command",
                "next_attempt",
                "attempts",
                "selection",
            },
            required={
                "mode",
                "decision",
                "observation",
                "manual",
                "last_command",
                "next_attempt",
                "attempts",
            },
        )
        for key, kind in (
            ("decision", "decision"),
            ("observation", "observation"),
            ("manual", "manual"),
        ):
            if item[key] is not None:
                _model(item[key], kind)
        if item["last_command"] is not None:
            _object(item["last_command"], {"target", "at"})
            _model(item["last_command"]["target"], "target")
            _time(item["last_command"]["at"])
        _time(item["next_attempt"], nullable=True)
        _integer(item["attempts"])
    for item in data["requirements"].values():
        _object(item, _MODELS["requirement_result"] | {"activation_values", "providers"})
        _model({key: item[key] for key in _MODELS["requirement_result"]}, "requirement_result")
        _require(type(item["activation_values"]) is dict, "Expected activation map")
        for entity in item["activation_values"]:
            _entity(entity)
        _require(type(item["providers"]) is dict, "Expected provider explanation map")
        for identifier, provider in item["providers"].items():
            _require(
                re.fullmatch(rf"[vr]_{_HEX}", identifier) is not None, "Invalid provider alias"
            )
            _object(provider, {"resource_id", "target", "confirmed", "evidence"})
            if provider["resource_id"] is not None:
                _alias(provider["resource_id"], "r_")
            if provider["target"] is not None:
                _model(provider["target"], "target")
            _require(type(provider["evidence"]) is list, "Expected evidence explanation list")
            for evidence in provider["evidence"]:
                _object(
                    evidence,
                    {
                        "kind",
                        "entity_id",
                        "attribute",
                        "operator",
                        "expected",
                        "observed",
                        "confirmed",
                    },
                )
                _entity(evidence["entity_id"])


def _record_data(kind: str, data: Any) -> None:
    _safe_tree(data)
    if kind == "session_start":
        _object(data, _SESSION)
        for key in (
            "timezone",
            "ha_version",
            "integration_version",
            "config_hash",
            "component_sha256",
        ):
            _require(type(data[key]) is str, "Missing session provenance")
        config = _configuration(data["config"])
        actual = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        _require(actual == data["config_hash"], "Configuration hash mismatch")
        _intent(data["intent"])
        _require(type(data["shadow_lock"]) is bool, "Invalid shadow lock flag")
        _require(
            data["previous_session_closed"] is None
            or type(data["previous_session_closed"]) is bool,
            "Invalid prior-session flag",
        )
        if data["gap_since_previous"] is not None:
            _object(data["gap_since_previous"], {"from", "to"})
            for value in data["gap_since_previous"].values():
                _time(value)
    elif kind == "input":
        _object(
            data,
            _INPUT,
            required=_INPUT - {"event_context", "event_parent_context", "old_last_reported"},
        )
        _entity(data["entity_id"])
        _require(
            type(data["event_type"]) is str
            and data["event_type"] in {"initial", "state_changed", "state_reported"},
            "Invalid input event",
        )
        _require(data["state"] is None or type(data["state"]) is str, "Invalid input state")
        attrs = data["attributes"]
        _require(type(attrs) is dict, "Input attributes must be an object")
        for key, value in attrs.items():
            _require(type(value) not in (dict, list) or not value, "Nested input attribute payload")
            _require(
                key in _ATTRIBUTES or re.fullmatch("attribute_" + _HEX, key) is not None,
                "Unsanitized input attribute",
            )
        for field in ("event_context", "event_parent_context"):
            if data.get(field) is not None:
                _alias(data[field], "context_id_")
        for key in ("last_changed", "last_updated", "last_reported", "old_last_reported"):
            _time(data.get(key), nullable=True)
    elif kind == "snapshot_end":
        _object(data, {"entities"})
        _integer(data["entities"])
    elif kind == "decision":
        _object(data, _DECISION, required={"resources", "requirements"})
        _explanation(data)
        if "trace" in data:
            _health(data["trace"])
        if "engine" in data:
            _engine_snapshot(data["engine"])
        if "engine_result" in data:
            _engine_result(data["engine_result"])
        if "engine_aliases" in data:
            _require(type(data["engine_aliases"]) is dict, "Expected engine alias map")
            values = set()
            for canonical, local in data["engine_aliases"].items():
                _require(
                    _ALIAS.fullmatch(canonical) is not None
                    and type(local) is str
                    and _ALIAS.fullmatch(local) is not None,
                    "Invalid engine alias",
                )
                _require(
                    local.startswith("r_")
                    or canonical.rsplit("_", 1)[0] == local.rsplit("_", 1)[0],
                    "Engine alias category mismatch",
                )
                _require(local not in values, "Duplicate engine alias")
                values.add(local)
        if "shadow_locked" in data:
            _require(type(data["shadow_locked"]) is bool, "Invalid shadow lock flag")
    elif kind == "admission":
        _object(data, _ADMISSION, required={"action", "revision"})
        _integer(data["revision"])
        _require(
            type(data["action"]) is str
            and data["action"]
            in {
                "request",
                "release",
                "stop",
                "set_mode",
                "set_policy_enabled",
                "submit_occurrence",
                "skip_occurrence",
            },
            "Unknown admission action",
        )
        if data.get("manual") is not None:
            _model(data["manual"], "manual")
        if "occurrence" in data:
            _model(data["occurrence"], "occurrence")
    elif kind == "dispatch":
        _object(data, _DISPATCH, required=_DISPATCH - {"error_type", "cancellation_scope"})
        _alias(data["resource_id"], "r_")
        _object(data["data"], {"entity_id", "position", "percentage", "direction"}, required=set())
        if "entity_id" in data["data"]:
            _entity(data["data"]["entity_id"])
        _alias(data["context_id"], "context_id_")
        _time(data["at"])
    elif kind == "external_command":
        _object(data, _EXTERNAL)
        _require(
            data["source"] == "homekit" and data["evidence"] == "bridge_request",
            "Invalid external command provenance",
        )
        _entity(data["entity_id"])
        _alias(data["context_id"], "context_id_")
        _time(data["at"])
    elif kind in {"heartbeat", "session_end"}:
        _health(data)
    elif kind == "gap":
        _object(data, {"lost_records", "health"})
        _integer(data["lost_records"], 1)
        _health(data["health"])
    else:
        raise TraceValidationError("Unknown record kind")


def _settling(records: list[dict]) -> dict:
    entities: dict[str, dict] = {}
    current_session = None
    end = None
    previous_sequence = 0
    for record in records:
        if (
            record["session_id"] != current_session
            or record["kind"] == "gap"
            or record["sequence"] != previous_sequence + 1
            or end is not None
            and record["at"] < end
        ):
            entities.clear()
            current_session = record["session_id"]
        end = record["at"]
        previous_sequence = record["sequence"]
        if record["kind"] != "input":
            continue
        data = record["data"]
        target = {
            key: value
            for key, value in data["attributes"].items()
            if key not in _METADATA_ATTRIBUTES
        }
        entity = data["entity_id"]
        previous = entities.get(entity)
        if (
            previous is None
            or previous["state"] != data["state"]
            or previous["target_attributes"] != target
        ):
            entities[entity] = {
                "state": data["state"],
                "target_attributes": target,
                "stable_since": end,
                "last_input_at": end,
            }
        else:
            previous["last_input_at"] = end
        entities[entity]["metadata_flags"] = {
            key: data["attributes"].get(key, False)
            for key in ("assumed_state", "restored", "optimistic")
        }
    for entity, item in entities.items():
        duration = max(0.0, end - item["stable_since"])
        moving = entity.startswith("cover.") and item["state"] in {"opening", "closing"}
        item.update(
            stable_seconds=duration,
            observed_until=end,
            moving=moving,
            settled=duration >= SETTLE_SECONDS
            and not moving
            and item["state"] not in {None, "unknown", "unavailable"},
            manual_intent="not_inferred",
        )
    return {
        "minimum_seconds": SETTLE_SECONDS,
        "entities": entities,
        "basis": "recorded_state_and_target_attributes",
        "manual_intent": "not_inferred",
    }


def validate_trace(payload: dict) -> NormalizedTrace:
    """Validate one export or ``{schema: 1, pages: [exports]}`` without side effects."""
    _json_types(payload)
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise TraceValidationError("Invalid finite JSON payload") from error
    _require(len(encoded) <= MAX_BYTES, "Trace byte limit exceeded")
    # Own the normalized data; subsequent caller mutation must not change evidence.
    payload = json.loads(encoded)
    _require(type(payload) is dict, "Trace root must be an object")
    if "pages" in payload:
        _object(payload, {"schema", "pages"})
        _require(type(payload["schema"]) is int and payload["schema"] == 1, "Unknown trace schema")
        pages = payload["pages"]
        _require(type(pages) is list and bool(pages), "Expected nonempty export pages")
    else:
        pages = [payload]
    records: list[dict] = []
    reasons: set[str] = set()
    sessions: dict[str, dict] = {}
    config: dict = {}
    previous_sequence = 0
    previous_at = None
    previous_page = None
    active_session = None
    snapshot_sessions: set[str] = set()
    initial_entities: dict[str, set[str]] = {}
    closed_sessions: set[str] = set()
    observed_intervals: list[dict] = []
    engine_decisions = 0
    metadata = None
    for page in pages:
        _object(page, _EXPORT)
        _require(type(page["schema"]) is int and page["schema"] == 1, "Unknown export schema")
        _safe_tree(
            {key: page[key] for key in ("integration_version", "config_hash", "component_sha256")}
        )
        _require(type(page["integration_version"]) is str, "Missing integration version")
        for key in ("config_hash", "component_sha256"):
            _require(
                type(page[key]) is str and _HASH.fullmatch(page[key]) is not None,
                "Missing or invalid provenance hash",
            )
        _health(page["health"])
        _integer(page["through_sequence"])
        _require(
            page["through_sequence"] <= page["health"]["last_sequence"],
            "Export bound exceeds recorded sequence",
        )
        _require(
            type(page["more"]) is bool and type(page["gap"]) is bool, "Invalid pagination flags"
        )
        _require(type(page["records"]) is list, "Records must be a list")
        _require(len(records) + len(page["records"]) <= MAX_RECORDS, "Trace record limit exceeded")
        identity = (page["integration_version"], page["config_hash"], page["component_sha256"])
        _require(metadata is None or metadata == identity, "Export metadata changed between pages")
        metadata = identity
        if previous_page is not None:
            _require(previous_page["more"], "Page follows a terminal export page")
            _require(
                page["through_sequence"] >= previous_page["through_sequence"],
                "Export sequence bound moved backwards",
            )
        if page["next_after"] is not None:
            _integer(page["next_after"])
        if page["gap"]:
            reasons.add("declared_gap")
        for record in page["records"]:
            _object(record, {"schema", "sequence", "session_id", "at", "kind", "data"})
            _require(
                type(record["schema"]) is int and record["schema"] == 1, "Unknown record schema"
            )
            _integer(record["sequence"], 1)
            _uuid(record["session_id"])
            _time(record["at"])
            _require(type(record["kind"]) is str, "Invalid record kind")
            _record_data(record["kind"], record["data"])
            sequence, session = record["sequence"], record["session_id"]
            _require(sequence > previous_sequence, "Duplicate or reversed record sequence")
            if sequence != previous_sequence + 1:
                reasons.add("sequence_gap" if previous_sequence else "prefix_missing")
            if previous_at is not None and record["at"] < previous_at:
                reasons.add("clock_regression")
            previous_sequence, previous_at = sequence, record["at"]
            if record["kind"] == "session_start":
                _require(session not in sessions, "Duplicate session start")
                data = record["data"]
                if active_session is not None and active_session not in closed_sessions:
                    reasons.add("session_end_missing")
                config = data["config"]
                sessions[session] = {
                    key: data[key]
                    for key in (
                        "config_hash",
                        "integration_version",
                        "ha_version",
                        "timezone",
                        "shadow_lock",
                        "component_sha256",
                    )
                }
                _require(
                    data["component_sha256"] == page["component_sha256"],
                    "Session component fingerprint mismatch",
                )
                if data["previous_session_closed"] is False:
                    reasons.add("unclean_previous_session")
                if data["gap_since_previous"] is not None:
                    observed_intervals.append({"session_id": session, **data["gap_since_previous"]})
                    if data["previous_session_closed"] is not True:
                        reasons.add("between_session_gap")
                if data["integration_version"] != page["integration_version"]:
                    reasons.add("integration_version_changed")
            elif session != active_session:
                reasons.add("session_start_missing")
            _require(session not in closed_sessions, "Record follows a closed session")
            active_session = session
            if record["kind"] == "input" and record["data"]["event_type"] == "initial":
                _require(session not in snapshot_sessions, "Initial input follows snapshot end")
                entities = initial_entities.setdefault(session, set())
                _require(record["data"]["entity_id"] not in entities, "Duplicate initial entity")
                entities.add(record["data"]["entity_id"])
            if record["kind"] == "snapshot_end":
                _require(session not in snapshot_sessions, "Duplicate snapshot end")
                if len(initial_entities.get(session, set())) != record["data"]["entities"]:
                    reasons.add("initial_inputs_missing")
                snapshot_sessions.add(session)
            if record["kind"] == "session_end":
                closed_sessions.add(session)
            if record["kind"] == "gap":
                reasons.add("recorded_gap")
            if record["kind"] == "decision" and not {"engine", "engine_result"} <= set(
                record["data"]
            ):
                reasons.add("engine_snapshot_missing")
            elif record["kind"] == "decision":
                engine_decisions += 1
            if record["kind"] in {"heartbeat", "session_end"}:
                _require(record["data"]["session_id"] == session, "Record health session mismatch")
            records.append(record)
        if page["records"]:
            _require(page["next_after"] == previous_sequence, "Invalid pagination cursor")
            _require(
                previous_sequence <= page["health"]["durable_sequence"],
                "Records exceed durable sequence",
            )
            _require(previous_sequence <= page["through_sequence"], "Records exceed export bound")
        else:
            _require(not page["more"], "Empty page cannot advertise more records")
            if previous_page is not None:
                _require(
                    page["next_after"] == previous_page["next_after"], "Empty page cursor changed"
                )
        previous_page = page
    final = pages[-1]
    health = final["health"]
    if final["more"]:
        reasons.add("pagination_incomplete")
    if not records:
        reasons.add("empty_trace")
    if set(sessions) - snapshot_sessions:
        reasons.add("initial_snapshot_missing")
    if not sessions:
        reasons.add("session_start_missing")
    if previous_sequence != final["through_sequence"]:
        reasons.add("tail_missing")
    # Opening another retained segment is not loss. Eviction is recorded as
    # history_gap; missing sequences and prefixes are checked independently.
    for key in ("dropped_records", "write_errors", "queued_records"):
        if health[key]:
            reasons.add(key)
    if not health["enabled"] or not health["healthy"] or not health["complete"]:
        reasons.add("incomplete_health")
    for key in ("history_gap", "unclean_previous"):
        if health[key]:
            reasons.add(key)
    if not engine_decisions:
        reasons.add("engine_decisions_missing")
    if records and health["session_id"] != records[-1]["session_id"]:
        reasons.add("current_session_missing")
    if sessions and sessions[next(reversed(sessions))]["config_hash"] != final["config_hash"]:
        reasons.add("current_configuration_missing")
    report = {
        "schema": 1,
        "source": "recorded_feedback",
        "physical_effects": "not_observed",
        "conformance": "not_evaluated",
        "replay_complete": not reasons,
        "reasons": sorted(reasons),
        "incomplete_reasons": sorted(reasons),
        "record_count": len(records),
        "page_count": len(pages),
        "engine_decision_count": engine_decisions,
        "integration_version": final["integration_version"],
        "config_hash": final["config_hash"],
        "component_sha256": final["component_sha256"],
        "through_sequence": final["through_sequence"],
        "sessions": sessions,
        "health": health,
        "settling": _settling(records),
        "between_session_intervals": observed_intervals,
        "synthetic_scenarios": [],
    }
    return NormalizedTrace(tuple(records), config, report)


def load_trace(path: str | Path) -> NormalizedTrace:
    """Read a bounded UTF-8 JSON document, rejecting duplicates and NaN/Infinity."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    _require(len(raw) <= MAX_BYTES, "Trace byte limit exceeded")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise TraceValidationError("Nonfinite JSON constant")

    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=nonfinite
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise TraceValidationError("Invalid UTF-8 JSON trace") from error
    return validate_trace(payload)
