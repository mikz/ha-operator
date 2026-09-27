"""Stream original hourly shadow-export pages; preserve gaps and recorder health.

Index: {schema:1,pages:[{file,sha256,request_after,capture_at}]}. Relative files are
resolved beside the private index. No paths or credentials enter the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, OrderedDict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.shadow_report import (  # noqa: E402
    _conditions,
    _feedback,
    _local_file,
    _number,
    _utc,
    validate_bindings,
)
from tests.lab.shadow_trace import validate_trace  # noqa: E402

MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 100_000
MAX_RECORDS = 2_000_000
MAX_ENTITIES = 1024
RETRY_CACHE = 4096
INTERVAL_LIMIT = 200
HEARTBEAT_GAP = 90.0
MAX_COVERS = 128
TARGET_SETTLE_SECONDS = 1.0


class ArchiveError(ValueError):
    """Invalid archive or explicit capacity boundary; never a partial success."""


def require(condition, message):
    if not condition:
        raise ArchiveError(message)


def _read(path, maximum):
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "Capacity exceeded: input bytes")
    return raw


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ArchiveError("Nonfinite JSON value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _expected_entities(config):
    entities = set(config["trace_entities"])
    for resource in config["resources"].values():
        entities.update(resource.get("outputs", []))
        entities.update(
            resource.get(key) for key in ("entity_id", "restriction_entity", "fault_entity")
        )
    for policy in config["policies"].values():
        entities.update(policy.get(key) for key in ("eligibility_entity", "target_entity"))
    for requirement in config["requirements"].values():
        entities.update(requirement.get("activation_entities", []))
        for provider in requirement.get("providers", []):
            entities.update(item["entity_id"] for item in provider["evidence"])
    entities.discard(None)
    require(len(entities) <= MAX_ENTITIES, "Capacity exceeded: configured entities")
    return entities


def _position(data, attribute="current_position", *, raw=False):
    if not data or data["state"] in (None, "unknown", "unavailable"):
        return None
    if raw and _feedback(data) is None or data["attributes"].get("restored"):
        return None
    value = _number(data["attributes"].get(attribute) if attribute else data["state"])
    return value if value is not None and 0 <= value <= 100 else None


class CoverComparison:
    """Bounded timed feedback comparisons; never infer a cause or prior target age."""

    def __init__(self, resource, policies):
        self.signature = self.configuration_signature(resource, policies)
        self.raw = resource.get("entity_id")
        self.policy = policies[0][1] if len(policies) == 1 else {}
        self.policy_id = policies[0][0] if len(policies) == 1 else None
        self.reason = (
            "multiple_policies" if len(policies) > 1 else "missing_policy" if not policies else None
        )
        if self.reason is None and (
            self.policy.get("kind", "state") != "state"
            or not self.policy.get("target_entity")
            or self.policy.get("target_field", "position") != "position"
            or not self.raw
        ):
            self.reason = "unsupported_policy_or_feedback"
        self.timeout = _number(resource.get("movement_timeout", 120))
        self.tolerance = _number(resource.get("tolerance", 2))
        if (
            self.timeout is None
            or self.timeout <= 0
            or self.tolerance is None
            or self.tolerance < 0
        ):
            self.reason = "invalid_comparison_settings"
        self.seconds, self.counts = Counter(), Counter()
        self.intervals = deque(maxlen=INTERVAL_LIMIT)
        self.total = self.known = self.raw_known = self.effective_known = 0.0
        self.target_changes = self.generation = self.interval_count = 0
        self.reset()

    @staticmethod
    def configuration_signature(resource, policies):
        return json.dumps([resource, sorted(policies)], sort_keys=True, separators=(",", ":"))

    def reset(self):
        self.requested = self.since = self.effective = self.last_command = None
        self.generation += 1

    def input(self, data, at):
        if self.reason or data["entity_id"] != self.policy["target_entity"]:
            return
        value = _position(data, self.policy.get("target_attribute"))
        if value != self.requested or self.since is None:
            self.target_changes += int(self.requested is not None and value is not None)
            self.requested, self.since = value, at if value is not None else None
            self.generation += 1

    def decision(self, resource_id, data, at):
        local_id = data.get("engine_aliases", {}).get(resource_id, resource_id)
        decision = data.get("engine_result", {}).get("decisions", {}).get(local_id)
        self.effective = (
            {key: decision.get(key) for key in ("target", "status", "source")}
            | {"frame_at_utc": _utc(at)}
            if decision is not None
            else None
        )
        self.last_command = data.get("resources", {}).get(resource_id, {}).get("last_command")

    def account(self, start, end, states, valid, epoch):
        if end <= start:
            return
        raw = states.get(self.raw)
        observed = _position(raw, raw=True)
        known = valid and observed is not None and self.requested is not None
        duration = end - start
        self.total += duration
        self.raw_known += duration if valid and observed is not None else 0
        self.known += duration if known and not self.reason else 0
        self.effective_known += duration if valid and self.effective is not None else 0
        points = {start, end}
        if known and not self.reason:
            points.update(
                boundary
                for boundary in (self.since + TARGET_SETTLE_SECONDS, self.since + self.timeout)
                if start < boundary < end
            )
        points = sorted(points)
        for left, right in zip(points, points[1:], strict=False):
            status = "not_compared" if self.reason else "unknown"
            if known and not self.reason:
                age = left - self.since
                moving = raw["state"] in {"opening", "closing"}
                status = (
                    "target_settling"
                    if age < TARGET_SETTLE_SECONDS
                    else "matched"
                    if abs(observed - self.requested) <= self.tolerance and not moving
                    else "pending_movement"
                    if age < self.timeout
                    else "sustained_divergence"
                )
            self.seconds[status] += right - left
            recent = self.intervals[-1] if self.intervals else None
            if recent and (
                recent["end"],
                recent["status"],
                recent["epoch"],
                recent["generation"],
            ) == (left, status, epoch, self.generation):
                recent["end"] = right
            else:
                self.intervals.append(
                    {
                        "start": left,
                        "end": right,
                        "status": status,
                        "epoch": epoch,
                        "generation": self.generation,
                        "requested_position": self.requested,
                        "observed_position_at_start": observed,
                    }
                )
                self.counts[status] += 1
                self.interval_count += 1

    def report(self, states):
        return {
            "comparison_supported": self.reason is None,
            "not_compared_reason": self.reason,
            "policy": self.policy_id,
            "requested_entity": self.policy.get("target_entity"),
            "observed_entity": self.raw,
            "movement_timeout_seconds": self.timeout,
            "tolerance": self.tolerance,
            "interval_seconds": round(self.total, 6),
            "known_seconds": round(self.known, 6),
            "unknown_seconds": round(self.seconds["unknown"], 6),
            "known_ratio": self.known / self.total if self.total and not self.reason else None,
            "raw_known_seconds": round(self.raw_known, 6),
            "effective_frame_known_seconds": round(self.effective_known, 6),
            "seconds_by_status": {key: round(value, 6) for key, value in self.seconds.items()},
            "target_changes": self.target_changes,
            "latest": {
                "requested_position": self.requested,
                "target_observed_since_utc": _utc(self.since) if self.since is not None else None,
                "observed_position": _position(states.get(self.raw), raw=True),
                "effective": self.effective,
                "last_dispatched_command": self.last_command,
            },
            "episode_counts": dict(self.counts),
            "omitted_episodes": max(0, self.interval_count - INTERVAL_LIMIT),
            "episodes": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"start", "end", "epoch", "generation"}
                }
                | {
                    "start_utc": _utc(item["start"]),
                    "end_utc": _utc(item["end"]),
                    "seconds": round(item["end"] - item["start"], 6),
                }
                for item in self.intervals
            ],
        }


class Archive:
    """Bounded streaming state; old retry hashes must never be silently trusted."""

    def __init__(self, bindings):
        self.cellar = validate_bindings(bindings or {}).get("cellar", {})
        self.cache = OrderedDict()
        self.states, self.inputs, self.modes, self.sessions = {}, {}, {}, {}
        self.closed = set()
        self.covers = {}
        self.snapshot_valid = False
        self.completed_snapshots, self.expected = set(), set()
        self.initial, self.snapshot, self.lock = set(), False, None
        self.events, self.health, self.page_reasons, self.mode_counts = (
            Counter(),
            Counter(),
            Counter(),
            Counter(),
        )
        self.conditions, self.episodes, self.findings = Counter(), {}, Counter()
        self.gaps = deque(maxlen=INTERVAL_LIMIT)
        self.previous = self.first = self.identity = self.last_page = None
        self.pages = self.records = self.duplicates = self.highest = self.epoch = 0
        self.observed_seconds = self.unknown_seconds = 0.0
        self.page_chain = hashlib.sha256()

    def interval(self, flag, start, end):
        entry = self.episodes.setdefault(
            flag, {"count": 0, "seconds": 0.0, "intervals": deque(maxlen=INTERVAL_LIMIT)}
        )
        entry["seconds"] += end - start
        recent = entry["intervals"]
        if recent and recent[-1]["end"] == start and recent[-1]["epoch"] == self.epoch:
            recent[-1]["end"] = end
        else:
            recent.append({"start": start, "end": end, "epoch": self.epoch})
            entry["count"] += 1

    def consume_page(self, page, requested_after, digest):
        normalized = validate_trace(page)
        identity = tuple(
            page[key] for key in ("integration_version", "config_hash", "component_sha256")
        )
        require(self.identity in (None, identity), "Unexpected deployment fingerprint change")
        self.identity = identity
        require(
            type(requested_after) is int and requested_after >= 0 or requested_after is None,
            "Invalid requested cursor",
        )
        rows = normalized.records
        require(
            not rows or rows[0]["sequence"] > (requested_after or 0),
            "Page precedes requested cursor",
        )
        self.pages += 1
        self.records += len(rows)
        require(
            self.pages <= MAX_PAGES and self.records <= MAX_RECORDS,
            "Capacity exceeded: archive inputs",
        )
        self.page_chain.update(digest.encode())
        self.page_reasons.update(normalized.report.get("incomplete_reasons", []))
        health = page["health"]
        for key in ("dropped_records", "write_errors", "rotations", "queued_records"):
            self.health[key] = max(self.health[key], health[key])
        for key in ("unclean_previous", "history_gap"):
            self.health[key] |= bool(health[key])
        self.health["incomplete_pages"] += int(not health["complete"])
        self.health["disabled_pages"] += int(not health["enabled"])
        self.health["unhealthy_pages"] += int(not health["healthy"])
        self.health["declared_gap_pages"] += int(page["gap"])
        for row in rows:
            self.consume(row)
        self.last_page = page

    def consume(self, row):
        sequence = row["sequence"]
        digest = hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if sequence <= self.highest:
            require(
                sequence in self.cache,
                "Capacity exceeded: retry overlap exceeds verified hash cache",
            )
            require(self.cache[sequence] == digest, "Conflicting duplicate record")
            self.duplicates += 1
            return
        self.cache[sequence] = digest
        if len(self.cache) > RETRY_CACHE:
            self.cache.popitem(last=False)
        gap = sequence != self.highest + 1
        if gap:
            self.gaps.append({"first_missing": self.highest + 1, "last_missing": sequence - 1})
            self.findings["sequence_gaps"] += 1
        self.highest = sequence
        session, kind, at, data = row["session_id"], row["kind"], row["at"], row["data"]
        require(session not in self.closed, "Record follows a closed session")
        previous = self.previous
        new_session = previous is None or session != previous["session_id"]
        elapsed = max(0, at - previous["at"]) if previous else 0
        backwards = previous is not None and at < previous["at"]
        silent = elapsed > HEARTBEAT_GAP
        broken = (
            gap
            or backwards
            or silent
            or new_session
            or kind == "gap"
            or previous
            and previous["kind"] == "gap"
        )
        self.findings["clock_regressions"] += int(backwards)
        self.findings["heartbeat_gaps"] += int(silent)
        self.findings["recorded_gaps"] += int(kind == "gap")
        if previous:
            for comparison in self.covers.values():
                comparison.account(
                    previous["at"], at, self.states, not broken and self.snapshot_valid, self.epoch
                )
        if previous and not broken:
            self.observed_seconds += elapsed
            if elapsed:
                for condition in _conditions(self.states, self.cellar):
                    self.interval(condition, previous["at"], at)
        else:
            self.unknown_seconds += elapsed
        if broken:
            for comparison in self.covers.values():
                comparison.reset()
            self.states.clear()
            self.modes.clear()
            self.lock = None
            self.epoch += 1
        if new_session:
            if previous and previous["session_id"] not in self.closed:
                self.findings["unclean_session_tails"] += 1
            if kind != "session_start":
                self.findings["session_start_missing"] += 1
            self.initial, self.expected, self.snapshot = set(), set(), False
            self.snapshot_valid = False
        if kind == "session_start":
            require(session not in self.sessions, "Duplicate session start")
            require(len(self.sessions) < 4096, "Capacity exceeded: sessions")
            self.sessions[session] = {
                key: data[key] for key in ("ha_version", "timezone", "shadow_lock")
            }
            self.expected = _expected_entities(data["config"])
            configured_covers = {
                key
                for key, value in data["config"]["resources"].items()
                if value["kind"] == "cover"
            }
            for resource_id in self.covers.keys() - configured_covers:
                self.covers[resource_id].reason = "resource_removed"
            for resource_id, resource in data["config"]["resources"].items():
                if resource["kind"] != "cover":
                    continue
                policies = [
                    (key, policy)
                    for key, policy in data["config"]["policies"].items()
                    if policy["resource_id"] == resource_id
                ]
                if resource_id not in self.covers:
                    require(len(self.covers) < MAX_COVERS, "Capacity exceeded: cover comparisons")
                    self.covers[resource_id] = CoverComparison(resource, policies)
                elif self.covers[resource_id].signature != CoverComparison.configuration_signature(
                    resource, policies
                ):
                    self.covers[resource_id].reason = "configuration_changed"
            self.findings["unclean_previous_sessions"] += int(
                data["previous_session_closed"] is False
            )
            self.lock = data["shadow_lock"]
            self.modes = {
                key: "observe" if self.lock else data["intent"]["modes"].get(key, "observe")
                for key in data["config"]["resources"]
            }
            self.mode_counts.update(self.modes.values())
        elif kind == "session_end":
            self.closed.add(session)
        elif kind == "snapshot_end":
            require(not self.snapshot, "Duplicate initial snapshot")
            self.findings["incomplete_initial_snapshot"] += int(
                len(self.initial) != data["entities"] or self.initial != self.expected
            )
            self.snapshot_valid = (
                len(self.initial) == data["entities"] and self.initial == self.expected
            )
            self.snapshot = True
            self.completed_snapshots.add(session)
        elif kind == "input":
            entity = data["entity_id"]
            require(
                entity in self.inputs or len(self.inputs) < MAX_ENTITIES,
                "Capacity exceeded: input entities",
            )
            item = self.inputs.setdefault(entity, Counter())
            old = self.states.get(entity)
            item["records"] += 1
            item["unknown_records"] += int(_feedback(data) is None)
            if old:
                item["changes"] += int(
                    any(old[key] != data[key] for key in ("state", "attributes"))
                )
            if data["event_type"] == "initial":
                require(
                    not self.snapshot and entity not in self.initial,
                    "Invalid initial snapshot input",
                )
                self.initial.add(entity)
            self.states[entity] = data
            for comparison in self.covers.values():
                comparison.input(data, at)
            self.conditions.update(_conditions(self.states, self.cellar))
        elif kind == "admission" and data["action"] == "set_mode":
            mode = "observe" if self.lock else data["mode"]
            self.modes[data["resource_id"]] = mode
            self.mode_counts[mode] += 1
        elif kind == "decision":
            for resource_id, comparison in self.covers.items():
                comparison.decision(resource_id, data, at)
            self.lock = data.get("shadow_locked", self.lock)
            if "engine" in data:
                self.modes = {
                    key: value["mode"] for key, value in data["engine"]["resources"].items()
                }
                self.mode_counts.update(self.modes.values())
        elif kind == "dispatch":
            self.events["dispatch_" + data["status"]] += 1
        self.events[kind] += 1
        self.events["unlocked_observations"] += int(self.lock is False)
        self.first = self.first or row
        self.previous = row

    def report(self):
        require(self.first is not None, "Archive contains no records")
        self.findings["missing_initial_snapshots"] = len(
            self.sessions.keys() - self.completed_snapshots
        )
        terminal = not self.last_page["more"] and self.highest == self.last_page["through_sequence"]
        uncertain = any(self.findings.values()) or any(
            self.health[key]
            for key in (
                "dropped_records",
                "write_errors",
                "unclean_previous",
                "history_gap",
                "declared_gap_pages",
                "disabled_pages",
                "unhealthy_pages",
            )
        )
        complete = (
            terminal and not uncertain and not self.unknown_seconds and self.first["sequence"] == 1
        )
        attempts = self.events["dispatch_started"]
        episodes = {}
        for key, item in self.episodes.items():
            episodes[key] = {
                "count": item["count"],
                "seconds": round(item["seconds"], 6),
                "omitted_intervals": max(0, item["count"] - INTERVAL_LIMIT),
                "intervals": [
                    {
                        "start_utc": _utc(v["start"]),
                        "end_utc": _utc(v["end"]),
                        "seconds": round(v["end"] - v["start"], 6),
                    }
                    for v in item["intervals"]
                ],
            }
        return {
            "schema": 1,
            "source": "immutable_export_archive",
            "page_hash_chain": self.page_chain.hexdigest(),
            "pages": self.pages,
            "unique_records": sum(
                self.events[k]
                for k in (
                    "session_start",
                    "session_end",
                    "snapshot_end",
                    "heartbeat",
                    "input",
                    "decision",
                    "admission",
                    "dispatch",
                    "external_command",
                    "gap",
                )
            ),
            "duplicate_records": self.duplicates,
            "capture": {
                "start_utc": _utc(self.first["at"]),
                "end_utc": _utc(self.previous["at"]),
                "first_sequence": self.first["sequence"],
                "last_sequence": self.highest,
                "observed_seconds": round(self.observed_seconds, 6),
                "unknown_seconds": round(self.unknown_seconds, 6),
                "complete_observed_interval": complete,
                "terminal_export": terminal,
                "heartbeat_gap_threshold_seconds": HEARTBEAT_GAP,
                "findings": dict(self.findings),
                "sequence_gaps": list(self.gaps),
                "omitted_sequence_gaps": max(0, self.findings["sequence_gaps"] - INTERVAL_LIMIT),
            },
            "recorder_health": dict(self.health),
            "per_page_validation_reasons": dict(self.page_reasons),
            "rotation_interpretation": "Rotation/completeness guards do not establish loss. "
            "Original flags remain above.",
            "operator": {
                "dispatch_attempts": attempts,
                "recorded_events": dict(self.events),
                "last_modes": self.modes,
                "mode_observations": dict(self.mode_counts),
                "last_shadow_lock": self.lock,
                "attempt_evidence": "attempts_recorded"
                if attempts
                else "zero_in_complete_observed_interval"
                if complete
                else "zero_recorded; coverage_uncertain",
            },
            "sessions": self.sessions,
            "inputs": {key: dict(value) for key, value in self.inputs.items()},
            "cover_comparisons": {
                "target_settle_seconds": TARGET_SETTLE_SECONDS,
                "resources": {key: value.report(self.states) for key, value in self.covers.items()},
                "interpretation": "Sustained divergence is timed telemetry requiring review, "
                "not an operator or device failure. Legacy synchronization timing may explain it. "
                "Effective targets are recorded decisions; no actuator causation is inferred.",
            },
            "cellar": {
                "configured": bool(self.cellar),
                "bindings": self.cellar,
                "condition_observations": dict(self.conditions),
                "episodes": episodes,
            },
            "not_exercised": [
                "native historical timing",
                "physical motor effects",
                "measured airflow",
                "manual actor attribution",
                "pure-engine replay",
            ],
            "limitations": [
                "Relay feedback and virtual requests do not prove motor operation or ownership.",
                "Intervals carry observations forward only across continuous records "
                "within the heartbeat threshold.",
                "A complete observed interval is not a claim about "
                "the entire requested seven days.",
            ],
        }


def analyze_archive(index_path, bindings=None):
    path = _local_file(str(index_path))
    index = _decode(_read(path, MAX_INDEX_BYTES))
    require(
        type(index) is dict and set(index) == {"schema", "pages"} and index["schema"] == 1,
        "Invalid archive index",
    )
    require(
        type(index["pages"]) is list and 0 < len(index["pages"]) <= MAX_PAGES,
        "Capacity exceeded or empty page index",
    )
    archive = Archive(bindings)
    capture_at = -math.inf
    for item in index["pages"]:
        require(
            type(item) is dict and set(item) == {"file", "sha256", "request_after", "capture_at"},
            "Invalid index entry",
        )
        stamp = item["capture_at"]
        require(
            type(stamp) in (int, float) and math.isfinite(stamp) and stamp >= capture_at,
            "Out-of-order capture time",
        )
        capture_at = stamp
        source = _local_file(item["file"])
        raw = _read(source if source.is_absolute() else path.parent / source, MAX_PAGE_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        require(digest == item["sha256"], "Export page hash mismatch")
        archive.consume_page(_decode(raw), item["request_after"], digest)
    return archive.report()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index")
    parser.add_argument("--bindings")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        bindings = _decode(_read(_local_file(args.bindings), 256 * 1024)) if args.bindings else None
        result = analyze_archive(args.index, bindings)
        with _local_file(args.output).open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        message = (
            str(error)
            if isinstance(error, ArchiveError)
            else "Invalid local archive or unavailable output"
        )
        parser.exit(2, message + "\n")


if __name__ == "__main__":
    main()
