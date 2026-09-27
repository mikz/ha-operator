"""Build a sanitized offline report from a validated, paginated shadow export.

This program has no Home Assistant or network client. It reads recorded feedback;
virtual fan requests, relay bits and timer states do not prove airflow or ownership.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.lab.shadow_trace import NormalizedTrace, TraceValidationError, load_trace  # noqa: E402

BINDINGS_BYTES = 256 * 1024
EPISODE_LIMIT = 200
_ALIAS = re.compile(r"[a-z_]+\.shadow_[0-9a-f]{20}\Z")
_ROLES = {
    "virtual_fan",
    "power",
    "inward",
    "outward",
    "manual_flag",
    "manual_timer",
    "cellar_timer",
}


class ReportError(ValueError):
    """Report input or local output is invalid; messages never echo private values."""


def _utc(value: float) -> str:
    try:
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError) as error:
        raise ReportError("Trace time is outside the supported calendar range") from error


def validate_bindings(value: Any) -> dict:
    """Only explicitly named roles and already-sanitized aliases are accepted."""
    if type(value) is not dict or set(value) - {"cellar"}:
        raise ReportError("Bindings must contain only an optional cellar object")
    cellar = value.get("cellar", {})
    if type(cellar) is not dict or set(cellar) - (_ROLES | {"inward_direction"}):
        raise ReportError("Unknown cellar role")
    for key, entity in cellar.items():
        if key == "inward_direction":
            if entity not in {"forward", "reverse"}:
                raise ReportError("Inward direction must be forward or reverse")
        elif type(entity) is not str or not _ALIAS.fullmatch(entity):
            raise ReportError("Role bindings require sanitized entity aliases")
    if len({value for key, value in cellar.items() if key in _ROLES}) != sum(
        key in _ROLES for key in cellar
    ):
        raise ReportError("Each cellar role must bind a distinct observed entity")
    return {"cellar": dict(cellar)} if cellar else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except TypeError, ValueError:
        return None


def _feedback(state: dict | None) -> Any:
    if not state or any(
        state.get("attributes", {}).get(key)
        for key in (
            "restored",
            "optimistic",
            "assumed_state",
        )
    ):
        return None
    value = state.get("state")
    return None if value in (None, "unknown", "unavailable") else value


def _conditions(states: dict, cellar: dict) -> set[str]:
    """Classify only observed combinations, without inferring a physical cause."""
    roles = {key: states.get(value) for key, value in cellar.items() if key in _ROLES}
    values = {key: _feedback(value) for key, value in roles.items()}
    for role in values:
        allowed = {"idle", "active", "paused"} if role.endswith("timer") else {"on", "off"}
        if values[role] not in allowed:
            values[role] = None
    # A virtual entity is requested state even when explicitly marked optimistic.
    virtual = roles.get("virtual_fan") or {}
    requested = virtual.get("state")
    if requested not in {"on", "off"} or virtual.get("attributes", {}).get("restored"):
        requested = None
    flags = set()
    for role, value in values.items():
        if value is None and role != "virtual_fan":
            flags.add(f"unknown_{role}")
    if "virtual_fan" in roles and requested is None:
        flags.add("unknown_virtual_fan")
    power, inward, outward = (values.get(key) for key in ("power", "inward", "outward"))
    if power == "on":
        flags.add("raw_power_feedback_on")
    if inward == "on" and outward == "on":
        flags.add("both_direction_relays_on")
    if requested is not None and power in {"on", "off"} and requested != power:
        flags.add("requested_state_differs_from_power_feedback")
    if requested == "on" and power == "on" and {inward, outward} == {"on", "off"}:
        requested_direction = virtual.get("attributes", {}).get("direction")
        inward_direction = cellar.get("inward_direction", "reverse")
        if requested_direction in {"forward", "reverse"} and (
            (requested_direction == inward_direction) != (inward == "on")
        ):
            flags.add("requested_direction_differs_from_relay_feedback")
    timers = [values[role] for role in ("manual_timer", "cellar_timer") if role in roles]
    if values.get("manual_flag") == "on" and timers and all(value == "idle" for value in timers):
        flags.add("legacy_manual_flag_active_without_running_timer")
        if power == "on":
            flags.add("legacy_manual_flag_active_idle_timers_power_on")
    return flags


def _episode_summary(episodes: list[dict]) -> dict:
    return {
        "count": len(episodes),
        "seconds": round(sum(item["end"] - item["start"] for item in episodes), 6),
        "intervals": [
            {
                "start_utc": _utc(item["start"]),
                "end_utc": _utc(item["end"]),
                "seconds": round(item["end"] - item["start"], 6),
            }
            for item in episodes[-EPISODE_LIMIT:]
        ],
        "omitted_intervals": max(0, len(episodes) - EPISODE_LIMIT),
    }


def _cellar(records: tuple[dict, ...], bindings: dict) -> dict:
    cellar = bindings.get("cellar")
    if not cellar:
        return {"configured": False}
    states: dict[str, dict] = {}
    episodes: dict[str, list[dict]] = defaultdict(list)
    previous: dict | None = None
    condition_observations: Counter = Counter()
    gap_seconds = 0.0
    total_seconds = 0.0
    for record in records:
        contiguous = previous is not None and (
            record["sequence"] == previous["sequence"] + 1
            and record["session_id"] == previous["session_id"]
            and record["kind"] not in {"gap", "session_start"}
            and previous["kind"] != "gap"
            and record["at"] >= previous["at"]
        )
        if previous is not None:
            seconds = max(0.0, record["at"] - previous["at"])
            total_seconds += seconds
            if contiguous:
                for condition in _conditions(states, cellar):
                    values = episodes[condition]
                    start, end = previous["at"], record["at"]
                    if end > start:
                        if values and values[-1]["end"] == start:
                            values[-1]["end"] = end
                        else:
                            values.append({"start": start, "end": end})
            else:
                gap_seconds += seconds
        if not contiguous or record["kind"] in {"session_start", "gap"}:
            states.clear()
        if record["kind"] == "input":
            states[record["data"]["entity_id"]] = record["data"]
            condition_observations.update(_conditions(states, cellar))
        previous = record
    return {
        "configured": True,
        "bindings": cellar,
        "direction_mapping": {
            "inward": cellar.get("inward_direction", "reverse"),
            "outward": "forward"
            if cellar.get("inward_direction", "reverse") == "reverse"
            else "reverse",
            "basis": "caller_role_mapping; physical wiring is not verified by this report",
        },
        "record_span_seconds": round(total_seconds, 6),
        "unattributed_gap_seconds": round(gap_seconds, 6),
        "condition_observations": dict(condition_observations),
        "episodes": {
            key: _episode_summary(value) for key, value in sorted(episodes.items()) if value
        },
        "interpretation": [
            "Intervals carry recorded states forward only within contiguous session records.",
            "Relay state is electrical control feedback; "
            "motor operation and measured airflow are unobserved.",
            "Virtual state is requested state; disagreement does not prove a motor defect.",
            "An active legacy flag with idle timers leaves ownership ambiguous; "
            "no automatic-start cause is inferred.",
            "Unknown feedback is reported separately and cannot establish a contradiction.",
        ],
    }


def _roof_comparisons(trace: NormalizedTrace) -> list[dict]:
    settled = trace.report.get("settling", {}).get("entities", {})
    rows = []
    for policy_id, policy in trace.config.get("policies", {}).items():
        resource_id = policy.get("resource_id")
        resource = trace.config.get("resources", {}).get(resource_id, {})
        if (
            resource.get("kind") != "cover"
            or not policy.get("target_entity")
            or (policy.get("target_field", "position") != "position")
        ):
            continue
        target = settled.get(policy["target_entity"], {})
        observed = settled.get(resource.get("entity_id"), {})
        raw_input = next(
            (
                record["data"]
                for record in reversed(trace.records)
                if record["kind"] == "input"
                and record["data"]["entity_id"] == resource.get("entity_id")
            ),
            None,
        )
        attribute = policy.get("target_attribute")
        requested = _number(
            target.get("target_attributes", {}).get(attribute) if attribute else target.get("state")
        )
        position = _number(observed.get("target_attributes", {}).get("current_position"))
        if target.get("state") in (None, "unknown", "unavailable"):
            requested = None
        if (
            observed.get("state") in (None, "unknown", "unavailable")
            or _feedback(raw_input) is None
        ):
            position = None
        if requested is not None and not 0 <= requested <= 100:
            requested = None
        if position is not None and not 0 <= position <= 100:
            position = None
        status = "unknown"
        if requested is not None and position is not None:
            status = "unsettled"
            if target.get("settled") and observed.get("settled"):
                status = (
                    "matched"
                    if abs(requested - position) <= resource.get("tolerance", 2)
                    else "different"
                )
        rows.append(
            {
                "policy": policy_id,
                "resource": resource_id,
                "target_entity": policy["target_entity"],
                "observed_entity": resource.get("entity_id"),
                "requested_position": requested,
                "observed_position": position,
                "comparison": status,
                "target_settled": bool(target.get("settled")),
                "observed_settled": bool(observed.get("settled")),
                "interpretation": "Recorded target/feedback comparison; "
                "policy eligibility and actuator causation are not inferred.",
            }
        )
    return rows


def build_report(trace: NormalizedTrace, bindings: dict | None = None) -> dict:
    """Summarize validated records without changing their state or accessing HA."""
    bindings = validate_bindings(bindings or {})
    per_input: dict[str, dict] = {}
    seen: dict[str, dict] = {}
    modes: Counter = Counter()
    dispatches: Counter = Counter()
    days: dict[str, Counter] = defaultdict(Counter)
    previous = None
    mode_values = {}
    shadow_lock = False
    session_tails = {}
    for record in trace.records:
        day = _utc(record["at"])[:10]
        days[day]["records"] += 1
        data = record["data"]
        session_tails[record["session_id"]] = record["kind"]
        if (
            previous is None
            or record["session_id"] != previous["session_id"]
            or (record["sequence"] != previous["sequence"] + 1 or record["kind"] == "gap")
        ):
            seen.clear()
            mode_values.clear()
        if record["kind"] == "session_start":
            resources = data.get("config", {}).get("resources", {})
            stored = data.get("intent", {}).get("modes", {})
            shadow_lock = data.get("shadow_lock", False)
            mode_values = {
                key: "observe" if shadow_lock else stored.get(key, "observe") for key in resources
            }
            modes.update(mode_values.values())
        elif record["kind"] == "admission" and data.get("action") == "set_mode":
            mode = "observe" if shadow_lock else data["mode"]
            mode_values[data["resource_id"]] = mode
            modes[mode] += 1
        elif record["kind"] == "dispatch":
            dispatches[data.get("status", "unknown")] += 1
            if data.get("status") == "started":
                days[day]["dispatch_attempts"] += 1
        elif record["kind"] == "input":
            key = data["entity_id"]
            summary = per_input.setdefault(
                key,
                {
                    "records": 0,
                    "state_changes": 0,
                    "attribute_changes": 0,
                    "unknown_records": 0,
                    "initial_snapshots": 0,
                },
            )
            summary["records"] += 1
            summary["initial_snapshots"] += int(data.get("event_type") == "initial")
            summary["unknown_records"] += int(_feedback(data) is None)
            if key in seen:
                summary["state_changes"] += int(data.get("state") != seen[key].get("state"))
                summary["attribute_changes"] += int(
                    data.get("attributes") != seen[key].get("attributes")
                )
                days[day]["input_changes"] += int(
                    any(
                        data.get(field) != seen[key].get(field) for field in ("state", "attributes")
                    )
                )
            seen[key] = data
        previous = record
    complete = bool(trace.report.get("replay_complete"))
    attempts = dispatches["started"]
    claim = (
        "attempts_recorded"
        if attempts
        else (
            "zero_attempts_in_complete_captured_interval"
            if complete and trace.records
            else "zero_recorded_attempts; coverage_incomplete"
        )
    )
    first, last = (trace.records[0], trace.records[-1]) if trace.records else (None, None)
    return {
        "schema": 1,
        "source": "offline_validated_shadow_export",
        "capture": {
            "start_utc": _utc(first["at"]) if first else None,
            "end_utc": _utc(last["at"]) if last else None,
            "records": len(trace.records),
            "replay_complete": complete,
            "quality": trace.report,
            "session_tails": {
                key: "closed"
                if kind == "session_end"
                else ("open_at_export" if key == (last or {}).get("session_id") else "unclean")
                for key, kind in session_tails.items()
            },
            "scope": "Only exported records; no claim about an entire day or unexported tail.",
        },
        "utc_days": {key: dict(value) for key, value in sorted(days.items())},
        "operator": {
            "dispatch_attempts": attempts,
            "dispatch_records_by_status": dict(dispatches),
            "attempt_evidence": claim,
            "mode_observations": dict(modes),
            "last_modes": mode_values,
            "manual_actor": "not_inferred_from_telemetry",
            "manual_leases": "not_inferred_from_telemetry",
        },
        "inputs": per_input,
        "roof_target_comparisons": _roof_comparisons(trace),
        "cellar": _cellar(trace.records, bindings),
        "limitations": [
            "Recorded state is feedback evidence, not direct observation of physical effects.",
            "Incomplete capture, unclean sessions and missing pages "
            "weaken zero-attempt and duration claims.",
        ],
    }


def _local_file(value: str) -> Path:
    if "://" in value or "\x00" in value:
        raise ReportError("Only local file paths are supported")
    return Path(value)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace")
    parser.add_argument("--bindings")
    parser.add_argument(
        "--output", help="Create a new report file; never overwrite an existing file"
    )
    args = parser.parse_args(argv)
    try:
        trace = load_trace(_local_file(args.trace))
        bindings = {}
        if args.bindings:
            path = _local_file(args.bindings)
            if path.stat().st_size > BINDINGS_BYTES:
                raise ReportError("Bindings input exceeds size limit")
            with path.open("rb") as stream:
                contents = stream.read(BINDINGS_BYTES + 1)
            if len(contents) > BINDINGS_BYTES:
                raise ReportError("Bindings input exceeds size limit")
            bindings = validate_bindings(json.loads(contents))
        rendered = (
            json.dumps(build_report(trace, bindings), indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        )
        if args.output:
            with _local_file(args.output).open("x", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            print(rendered, end="")
    except (OSError, ValueError, TraceValidationError) as error:
        message = (
            str(error)
            if isinstance(error, ReportError)
            else "Invalid local input or unavailable output; no report written"
        )
        parser.exit(2, message + "\n")


if __name__ == "__main__":
    main()
