"""Shadow trace privacy and bounded persistence without production connections."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import Context, State

from custom_components.ha_operator import shadow
from custom_components.ha_operator.shadow import (
    SCHEMA,
    Sanitizer,
    ShadowTrace,
    TraceDisk,
    alias,
    entity_alias,
)


@pytest.fixture
def configuration():
    return {
        "resources": {
            "roof-private": {
                "name": "Private bedroom window",
                "kind": "cover",
                "entity_id": "cover.private_roof",
                "restriction_entity": "binary_sensor.private_rain",
                "fault_entity": "binary_sensor.private_fault",
                "default_target": {"position": 40},
            },
            "fan-private": {
                "name": "Private cellar fan",
                "kind": "relay_fan",
                "outputs": ["switch.private_low"],
                "profiles": {
                    "private_off": {"outputs": {"switch.private_low": False}, "percentage": 0},
                    "private_low": {
                        "outputs": {"switch.private_low": True},
                        "percentage": 100,
                        "direction": "forward",
                    },
                },
                "default_target": {"profile": "private_low"},
            },
        },
        "policies": {
            "morning-private": {
                "name": "Private sleep schedule",
                "resource_id": "roof-private",
                "kind": "state",
                "eligibility_entity": "schedule.private_morning",
                "target_entity": "sensor.private_target",
                "target_attribute": "private_position",
                "target": {"position": 60},
            }
        },
        "requirements": {
            "air-private": {
                "name": "Private air",
                "activation_entities": ["switch.private_extract"],
                "providers": [
                    {
                        "id": "inlet-private",
                        "resource_id": "roof-private",
                        "target": {"position": 100},
                        "evidence": [
                            {
                                "entity_id": "cover.private_roof",
                                "attribute": "current_position",
                                "kind": "position",
                                "operator": "gte",
                                "value": 20,
                            }
                        ],
                    }
                ],
            }
        },
    }


@pytest.fixture
def intent():
    return {
        "manuals": {},
        "modes": {},
        "policy_enabled": {},
        "occurrences": {},
        "requests": {},
    }


def row(sequence, *, kind="input", **kwargs):
    return {
        "schema": SCHEMA,
        "sequence": sequence,
        "session_id": "00000000-0000-4000-8000-000000000001",
        "at": float(sequence),
        "kind": kind,
        "data": {},
        **kwargs,
    }


class Host:
    def __init__(self, path):
        self.config = SimpleNamespace(path=lambda name: str(path / name), time_zone="Europe/Prague")

    def async_add_executor_job(self, function, *args):
        return asyncio.get_running_loop().run_in_executor(None, function, *args)

    def async_create_task(self, coroutine, name):
        return asyncio.create_task(coroutine, name=name)


@pytest.fixture
async def trace_factory(tmp_path, configuration):
    instances = []

    def create(*, enabled=True, extras=None, entry_id="trace-test"):
        entry = SimpleNamespace(
            entry_id=entry_id,
            options={"trace_enabled": enabled},
            async_create_background_task=lambda _hass, coroutine, name: asyncio.create_task(
                coroutine, name=name
            ),
        )
        trace = ShadowTrace(Host(tmp_path), entry, deepcopy(configuration), extras or [])
        instances.append(trace)
        return trace

    yield create
    for trace in instances:
        await trace.async_close()


async def wait_thread(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4)


def test_deterministic_aliases_preserve_graph_links_without_private_names(configuration):
    sanitizer = Sanitizer(configuration, ["timer.private_sleep", "invalid value"])
    other = Sanitizer(deepcopy(configuration), ["timer.private_sleep", "invalid value"])
    assert sanitizer.config == other.config
    assert sanitizer.config_hash == other.config_hash
    text = json.dumps(sanitizer.config)
    for private in (
        "Private bedroom",
        "Private cellar",
        "Private sleep",
        "Private air",
        "roof-private",
        "private_roof",
        "private_low",
        "private_position",
        "private_morning",
        "private_extract",
        "inlet-private",
        "private_sleep",
    ):
        assert private not in text
    roof = alias("roof-private", "r_")
    policy = sanitizer.config["policies"][alias("morning-private", "p_")]
    provider = sanitizer.config["requirements"][alias("air-private", "q_")]["providers"][0]
    assert policy["resource_id"] == provider["resource_id"] == roof
    assert provider["id"] == alias("inlet-private", "v_")
    assert sanitizer.config["resources"][roof]["entity_id"] == entity_alias("cover.private_roof")
    assert provider["evidence"][0]["entity_id"] == entity_alias("cover.private_roof")
    assert sanitizer.config["trace_entities"] == [entity_alias("timer.private_sleep")]
    assert len(alias("a", "r_")) == 22
    assert entity_alias("cover.private_roof").startswith("cover.shadow_")
    changed = deepcopy(configuration)
    changed["resources"]["roof-private"]["default_target"]["position"] = 41
    assert Sanitizer(changed, []).config_hash != Sanitizer(configuration, []).config_hash


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (True, True),
        (4, 4),
        (4.5, 4.5),
        (float("nan"), None),
        (float("inf"), None),
        ("on", "on"),
        ("00:20:00", "00:20:00"),
        ("125:00:00.1", "125:00:00.1"),
        ("2026-09-27T12:30:00Z", "2026-09-27T12:30:00Z"),
        ("2026-09-27T12:30:00+02:00", "2026-09-27T12:30:00+02:00"),
        ("42.5", "42.5"),
        ([], []),
        ([{"private": "nested"}], []),
        ({"private": "value"}, {}),
    ],
)
def test_only_safe_scalar_observations_are_preserved(configuration, value, expected):
    assert Sanitizer(configuration, []).value(value) == expected


@pytest.mark.parametrize("value", ["private text", "nan", "inf", "9" * 100])
def test_private_or_unbounded_strings_are_hashed(configuration, value):
    sanitized = Sanitizer(configuration, []).value(value)
    assert sanitized == alias(value, "value_")
    assert value not in sanitized


def test_sanitizer_target_intent_and_source_identifiers(configuration, intent):
    sanitizer = Sanitizer(configuration, [])
    target = sanitizer.target({"profile": "private_low", "on": True, "secret": "private"})
    assert target == {"profile": alias("private_low", "profile_"), "on": True}
    assert sanitizer.target(None) is None
    intent["manuals"] = {
        "roof-private": {
            "source": "service",
            "mode": "target",
            "target": {"position": 40},
            "request_id": "private-request",
        },
        "unknown-resource": {"target": {"position": 80}},
    }
    intent["modes"] = {"roof-private": "observe"}
    intent["policy_enabled"] = {"morning-private": False}
    intent["occurrences"] = {
        "private-key": {
            "policy_id": "morning-private",
            "occurrence_id": "private-occurrence",
            "target": {"position": 50},
        }
    }
    sanitized = sanitizer.intent(intent)
    assert set(sanitized["manuals"]) == {alias("roof-private", "r_")}
    assert sanitized["modes"] == {alias("roof-private", "r_"): "observe"}
    assert sanitized["policy_enabled"] == {alias("morning-private", "p_"): False}
    assert "requests" not in sanitized
    assert "private" not in json.dumps(sanitized)
    cases = {
        "policy:morning-private": "policy:" + alias("morning-private", "p_"),
        "policy:morning-private:2026-09-27": "policy:"
        + alias("morning-private", "p_")
        + ":"
        + alias("2026-09-27", "occurrence_id_"),
        "policy:unknown": "policy:unknown",
        "requirement:air-private:inlet-private": "requirement:"
        + alias("air-private", "q_")
        + ":"
        + alias("inlet-private", "v_"),
        "manual:entity": "manual:entity",
        "manual:private": "manual:" + alias("private", "source_"),
        "private": alias("private", "source_"),
        "entity": "entity",
    }
    for source, expected in cases.items():
        assert sanitizer.fields({"source": source}) == {"source": expected}
    assert sanitizer.fields({"resource_id": None, "context_id": None, "profile": None}) == {
        "resource_id": None,
        "context_id": None,
        "profile": None,
    }
    assert sanitizer.fields({"failed_until": [("inlet-private", 123)]}) == {
        "failed_until": [[alias("inlet-private", "v_"), 123]],
    }
    assert sanitizer.fields(object()) is None
    assert sanitizer.fields("private", depth=13) is None
    assert len(sanitizer.fields(tuple(range(600)))) == 512


def test_input_allowlist_preserves_timer_and_selected_predicate_attributes(configuration):
    sanitizer = Sanitizer(
        configuration, ["timer.private_sleep", "input_datetime.private_wake", "fan.private_test"]
    )
    timestamp = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    state = State(
        "timer.private_sleep",
        "active",
        {
            "duration": "00:20:00",
            "remaining": "00:05:00",
            "finishes_at": "2026-09-27T12:05:00Z",
            "friendly_name": "Private sleep timer",
            "secret": "password",
            "restored": False,
        },
        last_changed=timestamp,
        last_updated=timestamp,
        last_reported=timestamp,
    )
    context = Context(id="private-context")
    observed = sanitizer.input("timer.private_sleep", state, "state_changed", context, timestamp)
    assert observed["entity_id"] == entity_alias("timer.private_sleep")
    assert observed["attributes"] == {
        "duration": "00:20:00",
        "remaining": "00:05:00",
        "finishes_at": "2026-09-27T12:05:00Z",
        "restored": False,
    }
    assert observed["event_context"] == alias("private-context", "context_id_")
    assert observed["old_last_reported"] == timestamp.timestamp()
    assert (
        observed["last_changed"]
        == observed["last_updated"]
        == observed["last_reported"]
        == timestamp.timestamp()
    )
    assert "Private" not in repr(observed) and "password" not in repr(observed)
    target = State("sensor.private_target", "ready", {"private_position": 25, "secret": "password"})
    selected = sanitizer.input("sensor.private_target", target, "state_reported")
    assert selected["attributes"] == {alias("private_position", "attribute_"): 25}
    missing = sanitizer.input("cover.private_roof", None, "state_changed")
    assert missing["state"] is None and missing["attributes"] == {}
    assert missing["event_context"] is None and missing["last_reported"] is None
    assert "timestamp" in sanitizer.attrs["input_datetime.private_wake"]
    assert "percentage" in sanitizer.attrs["fan.private_test"]


def test_trace_disk_rotation_is_bounded_and_pagination_marks_eviction(tmp_path):
    disk = TraceDisk(tmp_path / "trace", segment_bytes=250, segments=2)
    initial = disk.load()
    assert initial["sequence"] == 0 and initial["previous"] is None and initial["invalid"] == 0
    for sequence in range(1, 9):
        result = disk.append([row(sequence, data={"state": "open"})])
        assert result["durable"] == sequence
    paths = list(disk.path.glob("*.jsonl"))
    assert len(paths) <= 2
    assert sum(path.stat().st_size for path in paths) <= 500
    assert all(path.stat().st_mode & 0o077 == 0 for path in paths)
    page = disk.export(None, 1)
    assert page["gap"] is True and page["more"] is True
    first = page["records"][0]["sequence"]
    assert first > 1
    tail = disk.export(page["next_after"], 100)
    assert [record["sequence"] for record in tail["records"]] == list(range(first + 1, 9))
    assert tail["more"] is False
    assert disk.load()["sequence"] == 8


def test_trace_disk_torn_tail_is_rotated_before_new_append(tmp_path):
    disk = TraceDisk(tmp_path / "trace", segments=3)
    disk.load()
    disk.append([row(1)])
    with disk._file(0).open("ab") as file:
        file.write(b'{"schema":1,"sequence":2')
    loaded = disk.load()
    assert loaded["sequence"] == 1 and loaded["invalid"] >= 1
    assert disk._file(1).exists()
    disk.append([row(2)])
    exported = disk.export(None, 100)
    assert [record["sequence"] for record in exported["records"]] == [1, 2]
    assert exported["gap"] is True


@pytest.mark.parametrize(
    "malformed",
    [
        b"null\n",
        b"[]\n",
        b"not json\n",
        b'{"schema":2,"sequence":1}\n',
        b'{"schema":1,"sequence":true}\n',
        b'{"schema":1,"sequence":1}\n',
        b"\xff\n",
    ],
)
def test_trace_disk_malformed_rows_are_counted_and_never_recovered_as_valid(tmp_path, malformed):
    disk = TraceDisk(tmp_path / "trace")
    disk.load()
    disk._file(0).write_bytes(malformed)
    loaded = disk.load()
    assert loaded["sequence"] == 0
    assert loaded["previous"] is None
    assert loaded["invalid"] >= 1
    assert disk.export(None, 100)["gap"] is True


def test_trace_disk_caps_record_bytes_and_reports_sequence_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "RECORD_LIMIT", 500)
    disk = TraceDisk(tmp_path / "trace", segment_bytes=1000)
    disk.load()
    result = disk.append([row(1, data={"large": "x" * 501}), row(2)])
    assert result == {"rotations": 0, "oversized": 1, "durable": 2}
    assert [record["sequence"] for record in disk.export(None, 100)["records"]] == [2]
    assert disk.export(None, 100)["gap"] is True


def test_trace_disk_export_through_is_a_stable_page_boundary(tmp_path):
    disk = TraceDisk(tmp_path / "trace")
    disk.load()
    disk.append([row(n) for n in range(1, 5)])
    page = disk.export(None, 2, through=3)
    assert [record["sequence"] for record in page["records"]] == [1, 2]
    assert page["more"] is True
    next_page = disk.export(page["next_after"], 2, through=3)
    assert [record["sequence"] for record in next_page["records"]] == [3]
    assert next_page["more"] is False


async def test_disabled_trace_has_no_tasks_or_disk_io(trace_factory, intent, monkeypatch):
    trace = trace_factory(enabled=False)
    monkeypatch.setattr(trace.disk, "load", Mock(side_effect=AssertionError("disabled disk load")))
    monkeypatch.setattr(
        trace.disk, "export", Mock(side_effect=AssertionError("disabled disk export"))
    )
    await trace.async_start(intent, True)
    trace.record("private", {"secret": "ignored"})
    trace.event("dispatch", {"context_id": "private"})
    trace.input("cover.private_roof", None, "state_changed")
    exported = await trace.async_export()
    assert exported["records"] == []
    assert exported["health"]["enabled"] is False
    assert trace.sequence == 0 and trace._writer is None
    assert not trace.disk.path.exists()
    await trace.async_close()
    assert trace._closing


@pytest.mark.parametrize("after", [-1, True, 1.2, "1"])
async def test_export_rejects_invalid_cursor(trace_factory, after):
    with pytest.raises(ValueError, match="after"):
        await trace_factory(enabled=False).async_export(after=after)


@pytest.mark.parametrize("limit", [0, 1001, True, 1.2, "1"])
async def test_export_rejects_invalid_limit(trace_factory, limit):
    with pytest.raises(ValueError, match="limit"):
        await trace_factory(enabled=False).async_export(limit=limit)


async def test_trace_records_sanitized_sources_and_clean_restart(trace_factory, intent):
    trace = trace_factory(extras=["timer.private_sleep"])
    await trace.async_start(intent, True)
    trace.input(
        "cover.private_roof",
        State(
            "cover.private_roof", "closed", {"current_position": 0, "secret": "private-password"}
        ),
        "state_changed",
        at=123,
    )
    trace.input("cover.ignored", None, "state_changed")
    trace.event(
        "dispatch",
        {
            "resource_id": "roof-private",
            "context_id": "private-context",
            "domain": "cover",
            "service": "set_cover_position",
            "data": {"entity_id": "cover.private_roof", "position": 40},
        },
    )
    exported = await trace.async_export()
    assert [record["kind"] for record in exported["records"]] == [
        "session_start",
        "input",
        "dispatch",
    ]
    assert exported["through_sequence"] == 3
    assert exported["health"]["durable_sequence"] == 3
    assert exported["health"]["complete"] is True
    assert "private-password" not in repr(exported) and "private-context" not in repr(exported)
    assert exported["records"][1]["at"] == 123
    assert exported["records"][0]["data"]["shadow_lock"] is True
    await trace.async_close()
    ended = await trace.async_export()
    assert ended["records"][-1]["kind"] == "session_end"
    trace.record("ignored-after-close", {})
    other = trace_factory()
    await other.async_start(intent, True)
    restarted = await other.async_export(after=ended["next_after"])
    assert restarted["records"][0]["kind"] == "session_start"
    assert restarted["records"][0]["data"]["previous_session_closed"] is True
    assert restarted["health"]["unclean_previous"] is False


async def test_unclean_restart_reports_history_gap(trace_factory, intent):
    trace = trace_factory()
    trace.disk.load()
    trace.disk.append([row(5, kind="heartbeat")])
    await trace.async_start(intent, True)
    exported = await trace.async_export()
    assert exported["health"]["unclean_previous"] is True
    assert exported["health"]["history_gap"] is True
    assert exported["health"]["complete"] is False
    assert exported["records"][-1]["data"]["previous_session_closed"] is False


async def test_queue_overflow_is_counted_and_followed_by_gap_record(
    trace_factory, intent, monkeypatch
):
    monkeypatch.setattr(shadow, "QUEUE_LIMIT", 2)
    trace = trace_factory()
    entered, release = threading.Event(), threading.Event()
    original = trace.disk.append

    def delayed(records):
        entered.set()
        assert release.wait(3)
        return original(records)

    monkeypatch.setattr(trace.disk, "append", delayed)
    await trace.async_start(intent, True)
    try:
        await wait_thread(entered)
        for _ in range(3):
            trace.record("input", {})
        assert trace.queue.qsize() == 2 and trace.dropped == 1
        release.set()
        await trace.async_export()
        exported = await trace.async_export()
        assert any(record["kind"] == "gap" for record in exported["records"])
        await trace.async_close()
        assert exported["health"]["dropped_records"] >= 1
        assert exported["health"]["complete"] is False
    finally:
        release.set()


async def test_oversized_record_increments_health_without_becoming_durable(trace_factory, intent):
    trace = trace_factory()
    await trace.async_start(intent, True)
    await trace.async_export()
    trace.record("input", {"large": "x" * shadow.RECORD_LIMIT})
    exported = await trace.async_export()
    assert exported["health"]["dropped_records"] == 1
    assert exported["health"]["complete"] is False
    assert all("large" not in record["data"] for record in exported["records"])


async def test_persistent_append_errors_do_not_spin_or_generate_recovery_loops(
    trace_factory, intent, monkeypatch
):
    trace = trace_factory()
    failing = Mock(side_effect=OSError("private device path"))
    monkeypatch.setattr(trace.disk, "append", failing)
    await trace.async_start(intent, True)
    await trace.async_export()
    calls = failing.call_count
    await asyncio.sleep(0.02)
    assert failing.call_count == calls == 1
    assert trace.write_errors == 1 and trace.dropped == 1
    trace.record("input", {})
    await trace.async_export()
    assert failing.call_count == 2
    await trace.async_close()
    assert failing.call_count == 3
    assert trace.write_errors == 3


async def test_export_read_error_reports_gap_and_does_not_expose_exception_text(
    trace_factory, intent, monkeypatch
):
    trace = trace_factory()
    await trace.async_start(intent, True)
    await trace.async_export()
    monkeypatch.setattr(
        trace.disk, "export", Mock(side_effect=PermissionError("private-device-address"))
    )
    result = await trace.async_export()
    assert result["gap"] is True and result["records"] == []
    assert result["health"]["write_errors"] == 1
    assert "private-device-address" not in repr(result)


async def test_load_failure_keeps_optional_trace_failure_out_of_control(
    trace_factory, intent, monkeypatch
):
    trace = trace_factory()
    monkeypatch.setattr(trace.disk, "load", Mock(side_effect=PermissionError("private-path")))
    await trace.async_start(intent, True)
    await trace.async_export()
    assert trace.write_errors >= 1
    assert trace.enabled and trace._writer is not None


async def test_writer_cancellation_settles_executor_before_finishing(
    trace_factory, intent, monkeypatch
):
    trace = trace_factory()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = trace.disk.append

    def delayed(records):
        entered.set()
        assert release.wait(3)
        result = original(records)
        finished.set()
        return result

    monkeypatch.setattr(trace.disk, "append", delayed)
    await trace.async_start(intent, True)
    try:
        await wait_thread(entered)
        trace._writer.cancel()
        await asyncio.sleep(0)
        trace._writer.cancel()
        await asyncio.sleep(0)
        assert not trace._writer.done()
        release.set()
        await asyncio.wait_for(trace._writer, 1)
        assert finished.is_set()
        assert trace._closing and trace.write_errors >= 1
    finally:
        release.set()


async def test_close_cancellation_drains_records_and_session_end(
    trace_factory, intent, monkeypatch
):
    trace = trace_factory()
    await trace.async_start(intent, True)
    await trace.async_export()
    entered, release = threading.Event(), threading.Event()
    original = trace.disk.append

    def delayed(records):
        entered.set()
        assert release.wait(3)
        return original(records)

    monkeypatch.setattr(trace.disk, "append", delayed)
    trace.record("input", {})
    closing = None
    try:
        await wait_thread(entered)
        closing = asyncio.create_task(trace.async_close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert trace._writer.done()
        exported = await trace.async_export()
        assert exported["records"][-1]["kind"] == "session_end"
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)


def test_component_fingerprint_tracks_source_and_ignores_cache(tmp_path):
    (tmp_path / "integration.py").write_text("VERSION = 1\n")
    (tmp_path / "manifest.json").write_text('{"version":"1"}')
    original = shadow.component_fingerprint(tmp_path)
    assert len(original) == 64
    assert original == shadow.component_fingerprint(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "integration.pyc").write_bytes(b"irrelevant cache")
    assert shadow.component_fingerprint(tmp_path) == original
    (tmp_path / "integration.py").write_text("VERSION = 2\n")
    assert shadow.component_fingerprint(tmp_path) != original


def test_engine_frame_aliases_preserve_tie_order_and_occurrence_source_links(configuration):
    sanitizer = Sanitizer(configuration, [])
    engine = {
        "resources": {"roof-private": {"id": "roof-private"}},
        "occurrences": [
            {"policy_id": "morning-private", "occurrence_id": "z-date"},
            {"policy_id": "morning-private", "occurrence_id": "a-date"},
        ],
    }
    result = {"decisions": {"roof-private": {"source": "policy:morning-private:a-date"}}}
    framed_engine, framed_result, aliases = sanitizer.engine_frames(engine, result)
    resource = aliases[alias("roof-private", "r_")]
    policy = aliases[alias("morning-private", "p_")]
    occurrence = aliases[alias("a-date", "occurrence_id_")]
    assert framed_engine["resources"][resource]["id"] == resource
    assert framed_engine["occurrences"][1]["occurrence_id"] == occurrence
    assert framed_result["decisions"][resource]["source"] == f"policy:{policy}:{occurrence}"
    assert (
        framed_engine["occurrences"][1]["occurrence_id"]
        < framed_engine["occurrences"][0]["occurrence_id"]
    )
    # The stable alias map remains unchanged by snapshot-local ordinal aliases.
    assert sanitizer.ids["roof-private"] == alias("roof-private", "r_")


async def test_decision_capture_sanitizes_replay_frames_and_counts_unserializable_events(
    trace_factory, intent
):
    trace = trace_factory()
    await trace.async_start(intent, True)
    trace.event(
        "decision",
        {
            "engine": {"resources": {"roof-private": {"id": "roof-private"}}, "occurrences": []},
            "engine_result": {"decisions": {"roof-private": {"status": "idle"}}},
            "resource_id": "roof-private",
        },
    )
    trace.event("decision", {"engine": {"missing": "required keys"}})
    exported = await trace.async_export()
    decisions = [record for record in exported["records"] if record["kind"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["data"]["engine_aliases"]
    assert "roof-private" not in repr(decisions)
    assert exported["health"]["dropped_records"] == 1
    assert exported["health"]["write_errors"] == 1


async def test_capture_detaches_selected_values_and_rejects_invalid_json(trace_factory, intent):
    trace = trace_factory()
    await trace.async_start(intent, True)
    data = {"nested": {"value": 1}}
    trace.record("input", data)
    data["nested"]["value"] = 2
    trace.record("input", {"invalid": object()})
    exported = await trace.async_export()
    selected = [record for record in exported["records"] if record["kind"] == "input"]
    assert selected[0]["data"] == {"nested": {"value": 1}}
    assert exported["health"]["dropped_records"] == 1


async def test_idle_writer_heartbeat_and_cancellation_finish_without_spin(
    trace_factory, intent, monkeypatch
):
    monkeypatch.setattr(shadow, "HEARTBEAT_SECONDS", 0.005)
    trace = trace_factory()
    await trace.async_start(intent, True)
    await trace.async_export()
    for _ in range(50):
        if trace.last_heartbeat is not None:
            break
        await asyncio.sleep(0.002)
    assert trace.last_heartbeat is not None
    exported = await trace.async_export()
    assert any(record["kind"] == "heartbeat" for record in exported["records"])
    trace._writer.cancel()
    await asyncio.wait_for(trace._writer, 1)
    assert trace._closing and trace.write_errors >= 1


def test_oversized_disk_line_is_streamed_past_to_next_valid_row(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "RECORD_LIMIT", 200)
    disk = TraceDisk(tmp_path / "trace")
    disk.load()
    disk._file(0).write_bytes(b"x" * 601 + b"\n" + (json.dumps(row(1)) + "\n").encode())
    loaded = disk.load()
    assert loaded["invalid"] == 1
    assert loaded["sequence"] == 1
    assert [item["sequence"] for item in disk.export(None, 10)["records"]] == [1]


def test_export_byte_budget_does_not_claim_tail_is_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "EXPORT_BYTES", 350)
    disk = TraceDisk(tmp_path / "trace")
    disk.load()
    disk.append([row(sequence, data={"padding": "x" * 80}) for sequence in range(1, 5)])
    first = disk.export(None, 100)
    assert len(first["records"]) == 1 and first["more"] is True
    second = disk.export(first["next_after"], 100)
    assert second["records"][0]["sequence"] == 2


def test_duplicate_sequence_is_bad_evidence_without_losing_later_rows(tmp_path):
    disk = TraceDisk(tmp_path / "trace")
    disk.load()
    disk.append([row(1), row(1), row(2)])
    loaded = disk.load()
    assert loaded["invalid"] == 1 and loaded["sequence"] == 2
    assert [item["sequence"] for item in disk.export(None, 10)["records"]] == [1, 2]


async def test_persisted_loss_remains_visible_after_clean_restart(trace_factory, intent):
    trace = trace_factory()
    trace.disk.load()
    trace.disk.append([row(1, kind="session_end", data={"dropped_records": 3})])
    await trace.async_start(intent, True)
    exported = await trace.async_export()
    assert exported["health"]["history_gap"] is True
    assert exported["health"]["unclean_previous"] is False
    assert exported["health"]["complete"] is False


def test_fingerprint_rejects_symlinks_and_excludes_unshipped_extensions(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    before = shadow.component_fingerprint(tmp_path)
    (tmp_path / "notes.md").write_text("not production source")
    (tmp_path / ".private.py").write_text("ignored")
    assert shadow.component_fingerprint(tmp_path) == before
    (tmp_path / "services.yaml").write_text("request: {}")
    assert shadow.component_fingerprint(tmp_path) != before
    (tmp_path / "linked.py").symlink_to(tmp_path / "manifest.json")
    with pytest.raises(ValueError, match="symlink"):
        shadow.component_fingerprint(tmp_path)


def test_input_parent_context_and_huge_attribute_are_sanitized(configuration):
    sanitizer = Sanitizer(configuration, [])
    state = State("sensor.private_target", "0", {"private_position": "x" * 513})
    observed = sanitizer.input(
        "sensor.private_target", state, "state_changed", Context(parent_id="private-parent")
    )
    assert observed["event_parent_context"] == alias("private-parent", "context_id_")
    assert observed["attributes"][alias("private_position", "attribute_")] is None


@pytest.mark.parametrize("stage", ["load", "fingerprint"])
async def test_startup_cancellation_settles_disk_work_before_returning(
    trace_factory, intent, monkeypatch, stage
):
    trace = trace_factory()
    trace.disk.load()
    trace.disk._file(0).write_bytes(b'{"torn":')
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = trace.disk.load if stage == "load" else shadow.component_fingerprint

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        result = original(*args)
        finished.set()
        return result

    if stage == "load":
        monkeypatch.setattr(trace.disk, "load", delayed)
    else:
        monkeypatch.setattr(shadow, "component_fingerprint", delayed)
    starting = asyncio.create_task(trace.async_start(intent, True))
    try:
        await wait_thread(entered)
        starting.cancel()
        await asyncio.sleep(0)
        starting.cancel()
        await asyncio.sleep(0)
        assert not starting.done()
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert finished.is_set()
        assert trace.disk._file(1).exists()
        assert trace._writer is None
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
