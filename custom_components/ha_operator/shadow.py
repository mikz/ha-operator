"""Opt-in bounded, sanitized observation journal; never part of control admission.

Trace health describes missing/uncertain evidence explicitly. Capturing an event is
not a persistence receipt: only durable_sequence acknowledges a flushed batch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
from collections.abc import Mapping
from copy import copy
from pathlib import Path
from time import time
from typing import Any
from uuid import UUID, uuid4

from .async_utils import async_settle
from .const import VERSION

SCHEMA = 1
QUEUE_LIMIT = 512
RECORD_LIMIT = 64 * 1024
SEGMENT_BYTES = 2 * 1024 * 1024
SEGMENTS = 32
HEARTBEAT_SECONDS = 60
EXPORT_BYTES = 1024 * 1024
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
_BASE_ATTRS = {"supported_features", "assumed_state", "restored", "optimistic"}
_DOMAIN_ATTRS = {
    "cover": {"current_position"},
    "fan": {"percentage", "direction", "percentage_step", "speed_count"},
    "timer": {"duration", "remaining", "finishes_at"},
    "input_datetime": {"timestamp", "has_date", "has_time"},
    "binary_sensor": {"next_update"},
}
_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def alias(value: str, prefix: str) -> str:
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:20]


def entity_alias(value: str) -> str:
    return value.split(".", 1)[0] + "." + alias(value, "shadow_")


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def component_fingerprint(directory: Path) -> str:
    """Fingerprint package source bytes, independently of the release ZIP hash."""
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Component contains a symlink")
        if path.is_file() and path.suffix in {".py", ".json", ".yaml"}:
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Sanitizer:
    """One deterministic graph mapping shared by configuration and observations."""

    def __init__(self, config: dict, extra_entities: list[str]) -> None:
        self.ids: dict[str, str] = {}
        self.profiles: dict[str, str] = {}
        self.attrs: dict[str, set[str]] = {}
        self.attribute_aliases: dict[str, str] = {}
        self.occurrence_aliases: dict[str, str] = {}
        for collection, prefix in (("resources", "r_"), ("policies", "p_"), ("requirements", "q_")):
            for key in config[collection]:
                self.ids[key] = alias(key, prefix)
        for resource in config["resources"].values():
            for profile in resource.get("profiles", {}):
                self.profiles[profile] = alias(profile, "profile_")
            for entity in [resource.get("entity_id"), *resource.get("outputs", [])]:
                self.add_entity(entity)
            for key in ("restriction_entity", "fault_entity"):
                self.add_entity(resource.get(key))
        for policy in config["policies"].values():
            self.add_entity(policy.get("eligibility_entity"))
            self.add_entity(policy.get("target_entity"), policy.get("target_attribute"))
        for requirement in config["requirements"].values():
            for entity in requirement["activation_entities"]:
                self.add_entity(entity)
            for provider in requirement["providers"]:
                self.ids[provider["id"]] = self.ids.get(provider["id"], alias(provider["id"], "v_"))
                for predicate in provider["evidence"]:
                    self.add_entity(predicate["entity_id"], predicate.get("attribute"))
        for entity in extra_entities:
            self.add_entity(entity)
        self.config = self.configuration(config, extra_entities)
        self.config_hash = hashlib.sha256(
            json.dumps(self.config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def add_entity(self, entity: str | None, attribute: str | None = None) -> None:
        if not isinstance(entity, str) or not _ENTITY.fullmatch(entity):
            return
        self.attrs.setdefault(entity, _BASE_ATTRS | _DOMAIN_ATTRS.get(entity.split(".")[0], set()))
        if attribute:
            self.attrs[entity].add(attribute)
            self.attribute_aliases[attribute] = (
                attribute
                if attribute in _BASE_ATTRS | set().union(*_DOMAIN_ATTRS.values())
                else alias(attribute, "attribute_")
            )

    def value(self, value: Any) -> Any:
        if value is None or type(value) is bool or _finite(value):
            return value
        if isinstance(value, str):
            if len(value) > 512:
                return None
            if value in _SAFE_VALUES:
                return value
            if re.fullmatch(r"\d{1,3}:\d{2}:\d{2}(?:\.\d+)?", value) or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
            ):
                return value
            try:
                if len(value) <= 64 and math.isfinite(float(value)):
                    return value
            except ValueError:
                pass
            return alias(value, "value_")
        # Source attributes outside scalar predicate/adapter contracts are unusable.
        # Preserve their invalid type without copying arbitrary nested private data.
        if isinstance(value, list):
            return []
        if isinstance(value, Mapping):
            return {}
        return None

    def target(self, value: dict | None) -> dict | None:
        if value is None:
            return None
        return {
            key: self.profiles.get(item, alias(str(item), "profile_"))
            if key == "profile" and item is not None
            else self.value(item)
            for key, item in value.items()
            if key in {"position", "on", "percentage", "direction", "profile"}
        }

    def configuration(self, config: dict, extras: list[str]) -> dict:
        result = {"resources": {}, "policies": {}, "requirements": {}}
        for group, records in config.items():
            for key, data in records.items():
                item = self.fields(data)
                item["name"] = self.ids[key]
                result[group][self.ids[key]] = item
        result["trace_entities"] = [entity_alias(e) for e in extras if e in self.attrs]
        return result

    def fields(self, data: Any, key: str = "", depth: int = 0) -> Any:
        """Sanitize known integration-owned records; never whole HA attribute maps."""
        if depth > 12:
            return None
        if key == "name":
            return "shadow"
        if key in {"target", "default_target", "value"} and isinstance(data, dict):
            return self.target(data)
        if key in {
            "entity_id",
            "restriction_entity",
            "fault_entity",
            "eligibility_entity",
            "target_entity",
        }:
            return entity_alias(data) if isinstance(data, str) and data in self.attrs else None
        if key in {"attribute", "target_attribute"}:
            return self.attribute_aliases.get(data, data)
        if key in {"resource_id", "policy_id", "id", "selected_provider", "acquiring_provider"}:
            return self.ids.get(data) if data is not None else None
        if key in {"request_id", "occurrence_id", "context_id"}:
            if key == "occurrence_id" and data in self.occurrence_aliases:
                return self.occurrence_aliases[data]
            return alias(str(data), key + "_") if data is not None else None
        if key == "profile":
            return (
                self.profiles.get(data, alias(str(data), "profile_")) if data is not None else None
            )
        if key == "profiles" and isinstance(data, dict):
            return {
                self.profiles[name]: self.fields(value, depth=depth + 1)
                for name, value in data.items()
            }
        if key in {"outputs", "activation_entities"}:
            if isinstance(data, list):
                return [entity_alias(entity) for entity in data if entity in self.attrs]
            return {
                entity_alias(entity): value
                for entity, value in data.items()
                if entity in self.attrs
            }
        if key == "failed_until":
            return [[self.ids.get(item[0]), item[1]] for item in data]
        if key == "source" and isinstance(data, str):
            if ":" not in data:
                return (
                    data
                    if data in {"service", "entity", "stop", "homekit"}
                    else alias(data, "source_")
                )
            if data.startswith("policy:"):
                source_id = next(
                    (
                        old
                        for old in sorted(self.ids, key=len, reverse=True)
                        if data == "policy:" + old or data.startswith("policy:" + old + ":")
                    ),
                    None,
                )
                if source_id is None:
                    return "policy:unknown"
                suffix = data[len("policy:" + source_id) :]
                return (
                    "policy:"
                    + self.ids[source_id]
                    + (
                        ":"
                        + self.occurrence_aliases.get(
                            suffix[1:], alias(suffix[1:], "occurrence_id_")
                        )
                        if suffix
                        else ""
                    )
                )
            if data.startswith("requirement:"):
                parts = data.split(":", 2)
                return (
                    "requirement:"
                    + self.ids.get(parts[1], "unknown")
                    + ":"
                    + self.ids.get(parts[2], "unknown")
                )
            source = data.removeprefix("manual:")
            return "manual:" + (
                source if source in {"service", "entity", "stop"} else alias(source, "source_")
            )
        if isinstance(data, dict):
            return {
                (entity_alias(k) if k in self.attrs else self.ids.get(str(k), str(k))): self.fields(
                    v, str(k), depth + 1
                )
                for k, v in data.items()
            }
        if isinstance(data, (tuple, list)):
            return [self.fields(item, depth=depth + 1) for item in data[:512]]
        if key in {
            "status",
            "reason",
            "action",
            "kind",
            "mode",
            "service",
            "domain",
            "error_type",
            "cancellation_scope",
            "fault",
            "restriction",
        }:
            # These are integration/HA generated, never exception text or user labels.
            return str(data)[:128] if data is not None else None
        return self.value(data)

    def engine_frames(self, engine: dict, result: dict) -> tuple[dict, dict, dict]:
        """Preserve stable-ID tie ordering with snapshot-local opaque aliases.

        Configuration aliases remain stable across records. Snapshot aliases add
        only ordinal information, needed because arbitrary hashes reorder ties.
        """
        local = copy(self)
        local.ids = {
            key: f"r_{rank:08x}" + hashlib.sha256(key.encode()).hexdigest()[:12]
            for rank, key in enumerate(sorted(self.ids))
        }
        local.occurrence_aliases = {
            key: f"occurrence_id_{rank:08x}" + hashlib.sha256(key.encode()).hexdigest()[:12]
            for rank, key in enumerate(
                sorted({item["occurrence_id"] for item in engine["occurrences"]})
            )
        }
        aliases = {self.ids[key]: value for key, value in local.ids.items()}
        aliases.update(
            {alias(key, "occurrence_id_"): value for key, value in local.occurrence_aliases.items()}
        )
        return local.fields(engine), local.fields(result), aliases

    def intent(self, state: dict) -> dict:
        result = {}
        for key in ("manuals", "modes", "policy_enabled"):
            result[key] = {
                self.ids[k]: self.fields(v) for k, v in state[key].items() if k in self.ids
            }
        result["occurrences"] = [self.fields(value) for value in state["occurrences"].values()]
        return result

    def input(
        self,
        entity: str,
        state: Any,
        event_type: str,
        context: Any = None,
        old_reported: Any = None,
        reported: Any = None,
    ) -> dict:
        attrs = (
            {}
            if state is None
            else {
                self.attribute_aliases.get(key, key): self.value(state.attributes[key])
                for key in self.attrs[entity]
                if key in state.attributes
            }
        )
        return {
            "entity_id": entity_alias(entity),
            "event_type": event_type,
            "event_context": alias(context.id, "context_id_") if context is not None else None,
            "event_parent_context": alias(context.parent_id, "context_id_")
            if context is not None and context.parent_id is not None
            else None,
            "old_last_reported": old_reported.timestamp() if old_reported is not None else None,
            "state": None if state is None else self.value(state.state),
            "attributes": attrs,
            **{
                key: (
                    reported
                    if key == "last_reported" and reported is not None
                    else getattr(state, key)
                ).timestamp()
                if state is not None
                else None
                for key in ("last_changed", "last_updated", "last_reported")
            },
        }


class TraceDisk:
    """Blocking bounded journal operations, called only from executor jobs."""

    def __init__(
        self, path: Path, segment_bytes: int = SEGMENT_BYTES, segments: int = SEGMENTS
    ) -> None:
        self.path, self.segment_bytes, self.segments = path, segment_bytes, segments
        self.lock = threading.Lock()

    def _file(self, index: int) -> Path:
        return self.path / f"trace-{index}.jsonl"

    def _read(self, after: int | None = None, limit: int = 0, through: int | None = None) -> dict:
        records, invalid, maximum, first, previous, more, size = [], 0, 0, None, None, False, 0
        gap, history_gap = False, False
        expected = 1 if after is None else after + 1
        for index in reversed(range(self.segments)):
            try:
                with self._file(index).open("rb") as stream:
                    while line := stream.readline(RECORD_LIMIT + 1):
                        if len(line) > RECORD_LIMIT or not line.endswith(b"\n"):
                            invalid += 1
                            # Drain an oversized line without allocating the full payload.
                            while line and not line.endswith(b"\n"):
                                line = stream.readline(RECORD_LIMIT + 1)
                            continue
                        try:
                            record = json.loads(line)
                            if (
                                not isinstance(record, dict)
                                or set(record)
                                != {"schema", "sequence", "session_id", "at", "kind", "data"}
                                or type(record["schema"]) is not int
                                or record["schema"] != SCHEMA
                                or type(record["sequence"]) is not int
                                or record["sequence"] <= maximum
                                or not _finite(record["at"])
                                or record["kind"]
                                not in {
                                    "session_start",
                                    "input",
                                    "snapshot_end",
                                    "decision",
                                    "admission",
                                    "dispatch",
                                    "external_command",
                                    "heartbeat",
                                    "gap",
                                    "session_end",
                                }
                                or not isinstance(record["data"], dict)
                            ):
                                raise ValueError
                            UUID(record["session_id"])
                        except ValueError, KeyError, TypeError, AttributeError:
                            invalid += 1
                            continue
                        if first is None:
                            first = record["sequence"]
                        history_gap |= record["sequence"] != maximum + 1
                        history_gap |= record["kind"] == "gap"
                        if record["kind"] in {"session_end", "heartbeat"}:
                            data = record["data"]
                            history_gap |= bool(
                                data.get("history_gap")
                                or data.get("unclean_previous")
                                or data.get("dropped_records")
                                or data.get("write_errors")
                            )
                        maximum, previous = record["sequence"], record
                        if (
                            limit
                            and (after is None or maximum > after)
                            and (through is None or maximum <= through)
                        ):
                            if len(records) >= limit or (
                                records and size + len(line) > EXPORT_BYTES
                            ):
                                more = True
                                continue
                            gap |= maximum != expected
                            expected = maximum + 1
                            size += len(line)
                            records.append(record)
            except FileNotFoundError:
                continue
        return {
            "records": records,
            "invalid": invalid,
            "sequence": maximum,
            "previous": previous,
            "first_sequence": first,
            "more": more,
            "gap": gap or bool(invalid),
            "history_gap": history_gap or bool(invalid),
        }

    def load(self) -> dict:
        with self.lock:
            created = not self.path.exists()
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if created:
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            state = self._read()
            # Never append a valid row onto a previous process's torn final line.
            if self._file(0).exists():
                with self._file(0).open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    if stream.tell():
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            self._rotate()
            return state

    def _rotate(self) -> None:
        self._file(self.segments - 1).unlink(missing_ok=True)
        for index in reversed(range(self.segments - 1)):
            if self._file(index).exists():
                self._file(index).replace(self._file(index + 1))

    def append(self, records: list[dict]) -> dict:
        with self.lock:
            rotations, oversized, durable = 0, 0, 0
            directory_changed = False
            for record in records:
                encoded = (
                    json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
                ).encode()
                if len(encoded) > min(RECORD_LIMIT, self.segment_bytes):
                    oversized += 1
                    continue
                path = self._file(0)
                if path.exists() and path.stat().st_size + len(encoded) > self.segment_bytes:
                    self._rotate()
                    rotations += 1
                directory_changed |= not path.exists()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "ab") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                durable = record["sequence"]
            if rotations or directory_changed:
                fd = os.open(self.path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return {"rotations": rotations, "oversized": oversized, "durable": durable}

    def export(self, after: int | None, limit: int, through: int | None = None) -> dict:
        with self.lock:
            state = self._read(after, limit, through)
        page = state["records"]
        return {
            "records": page,
            "next_after": page[-1]["sequence"] if page else after,
            "more": state["more"],
            "gap": state["gap"],
        }


class ShadowTrace:
    """Synchronous event copying plus independent bounded asynchronous persistence."""

    def __init__(self, hass: Any, entry: Any, config: dict, extras: list[str]) -> None:
        self.hass, self.entry = hass, entry
        self.enabled = bool(entry.options.get("trace_enabled", False))
        self.sanitizer = Sanitizer(config, extras)
        self.disk = TraceDisk(Path(hass.config.path(f"ha_operator_trace.{entry.entry_id}")))
        self.session_id = str(uuid4())
        self.sequence = self.durable_sequence = 0
        self.dropped = self.write_errors = self.rotations = 0
        self.last_heartbeat = self.last_write = None
        self.queue: asyncio.Queue[dict] = asyncio.Queue(QUEUE_LIMIT)
        self._pending_gap = 0
        self._closing = False
        self._writer: asyncio.Task | None = None
        self._closed_task: asyncio.Task | None = None
        self._flush = asyncio.Condition()
        self._processed_sequence = 0
        self._inflight = False
        self._unclean_previous = False
        self._history_gap = False
        self.component_sha256 = None
        self._last_decision = None

    async def async_start(self, intent: dict, shadow_lock: bool) -> None:
        if not self.enabled:
            return
        previous = None
        try:
            state, cancelled = await async_settle(self.hass.async_add_executor_job(self.disk.load))
            if cancelled:
                raise asyncio.CancelledError
            self.sequence = self.durable_sequence = state["sequence"]
            previous = state["previous"]
            self._history_gap = state["history_gap"]
            self._unclean_previous = previous is not None and previous["kind"] != "session_end"
            self.component_sha256, cancelled = await async_settle(
                self.hass.async_add_executor_job(component_fingerprint, Path(__file__).parent)
            )
            if cancelled:
                raise asyncio.CancelledError
            self.write_errors += state["invalid"]
        except Exception:
            self.write_errors += 1
        self._processed_sequence = self.sequence
        from homeassistant.const import __version__ as ha_version

        self.record(
            "session_start",
            {
                "config": self.sanitizer.config,
                "intent": self.sanitizer.intent(intent),
                "timezone": self.hass.config.time_zone,
                "ha_version": ha_version,
                "integration_version": VERSION,
                "config_hash": self.sanitizer.config_hash,
                "component_sha256": self.component_sha256,
                "shadow_lock": shadow_lock,
                "previous_session_closed": previous["kind"] == "session_end" if previous else None,
                "gap_since_previous": None
                if previous is None
                else {"from": previous["at"], "to": time()},
            },
        )
        self._writer = self.entry.async_create_background_task(
            self.hass, self._run(), "ha_operator:shadow_trace"
        )

    def record(self, kind: str, data: dict, *, at: float | None = None) -> None:
        if not self.enabled or self._closing:
            return
        self.sequence += 1
        record = {
            "schema": SCHEMA,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "at": time() if at is None else at,
            "kind": kind,
            "data": data,
        }
        try:
            # Bound bytes before enqueue and detach all selected values now.
            encoded = json.dumps(record, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode()) + 1 > RECORD_LIMIT:
                raise ValueError("Trace record exceeds byte limit")
            self.queue.put_nowait(json.loads(encoded))
        except asyncio.QueueFull, ValueError, TypeError:
            self.dropped += 1
            self._pending_gap += 1

    def input(
        self,
        entity: str,
        state: Any,
        event_type: str,
        at: float | None = None,
        context: Any = None,
        old_reported: Any = None,
        reported: Any = None,
    ) -> None:
        if self.enabled and entity in self.sanitizer.attrs:
            self.record(
                "input",
                self.sanitizer.input(entity, state, event_type, context, old_reported, reported),
                at=at,
            )

    def event(self, kind: str, data: dict) -> None:
        if self.enabled:
            try:
                if kind == "decision":
                    engine, result, aliases = self.sanitizer.engine_frames(
                        data["engine"], data["engine_result"]
                    )
                    sanitized = self.sanitizer.fields(
                        {
                            key: value
                            for key, value in data.items()
                            if key not in {"engine", "engine_result"}
                        }
                    )
                    sanitized.update(engine=engine, engine_result=result, engine_aliases=aliases)
                    comparison = hashlib.sha256(
                        json.dumps(
                            {**sanitized, "engine": {**engine, "now": None}},
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                    ).digest()
                    if comparison == self._last_decision:
                        return
                    self._last_decision = comparison
                else:
                    sanitized = self.sanitizer.fields(data)
                self.record(kind, sanitized)
            except Exception:
                self.write_errors += 1
                self.dropped += 1
                self._pending_gap += 1

    def health(self) -> dict:
        return {
            "enabled": self.enabled,
            "healthy": not self.write_errors and not self.dropped,
            "session_id": self.session_id,
            "last_sequence": self.sequence,
            "durable_sequence": self.durable_sequence,
            "queued_records": self.queue.qsize(),
            "dropped_records": self.dropped,
            "write_errors": self.write_errors,
            "rotations": self.rotations,
            "queue_limit": QUEUE_LIMIT,
            "record_bytes_limit": RECORD_LIMIT,
            "disk_bytes_limit": self.disk.segment_bytes * self.disk.segments,
            "unclean_previous": self._unclean_previous,
            "history_gap": self._history_gap,
            "last_heartbeat": self.last_heartbeat,
            "last_write_at": self.last_write,
            "complete": self.enabled
            and not self.dropped
            and not self.write_errors
            and not self.rotations
            and not self._unclean_previous
            and not self._history_gap
            and self.durable_sequence == self.sequence,
        }

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        heartbeat_due = loop.time() + HEARTBEAT_SECONDS
        while not self._closing or not self.queue.empty():
            if not self._closing and loop.time() >= heartbeat_due:
                self.last_heartbeat = time()
                self.record("heartbeat", self.health())
                heartbeat_due = loop.time() + HEARTBEAT_SECONDS
            try:
                async with asyncio.timeout(max(0.001, heartbeat_due - loop.time())):
                    first = await self.queue.get()
            except asyncio.CancelledError:
                # HA cancellation at an idle wait still drains the captured tail.
                self._closing = True
                self.write_errors += 1
                continue
            except TimeoutError:
                continue
            self._inflight = True
            successful = False
            batch = [first]
            while len(batch) < 64 and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                result, cancelled = await async_settle(
                    self.hass.async_add_executor_job(self.disk.append, batch)
                )
                self.durable_sequence = max(self.durable_sequence, result["durable"])
                self.rotations += result["rotations"]
                self.dropped += result["oversized"]
                self._pending_gap += result["oversized"]
                self.last_write = time()
                successful = True
                if cancelled:
                    self.write_errors += 1
                    self._closing = True
            except Exception:
                self.write_errors += 1
                self.dropped += len(batch)
                self._pending_gap += len(batch)
            finally:
                self._inflight = False
                self._processed_sequence = batch[-1]["sequence"]
                for _ in batch:
                    self.queue.task_done()
                async with self._flush:
                    self._flush.notify_all()
            if successful and self._pending_gap and not self.queue.full() and not self._closing:
                count, self._pending_gap = self._pending_gap, 0
                self.record("gap", {"lost_records": count, "health": self.health()})

    async def async_export(self, after: int | None = None, limit: int = 100) -> dict:
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError("after must be a non-negative sequence")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        target = self.sequence
        if self._writer is not None and not self._writer.done():
            async with self._flush:
                await self._flush.wait_for(
                    lambda: (
                        self._processed_sequence >= target
                        or (self.queue.empty() and not self._inflight)
                    )
                )
        try:
            result = (
                await self.hass.async_add_executor_job(self.disk.export, after, limit, target)
                if self.enabled
                else {
                    "records": [],
                    "next_after": after,
                    "more": False,
                    "gap": False,
                }
            )
        except Exception:
            self.write_errors += 1
            result = {"records": [], "next_after": after, "more": False, "gap": True}
        return {
            "schema": SCHEMA,
            "integration_version": VERSION,
            "config_hash": self.sanitizer.config_hash,
            "component_sha256": self.component_sha256,
            "through_sequence": target,
            **result,
            "health": self.health(),
        }

    async def async_close(self) -> None:
        if self._closed_task is None:
            self._closed_task = self.hass.async_create_task(
                self._close(), "ha_operator:trace_close"
            )
        _, cancelled = await async_settle(self._closed_task)
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        if self._writer is None:
            self._closing = True
            return
        self.record("session_end", self.health())
        self._closing = True
        await self._writer
