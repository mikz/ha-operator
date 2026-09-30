"""Durability tests inspect committed files independently of exposed store state."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from custom_components.ha_operator import storage
from custom_components.ha_operator.storage import IntentStore, IntentStoreError, InvalidSnapshot


class ExecutorHost:
    """Real background executor with no running Home Assistant required."""

    def async_add_executor_job(self, target, *args):
        return asyncio.get_running_loop().run_in_executor(None, target, *args)


@pytest.fixture
def snapshot_path(tmp_path: Path) -> Path:
    return tmp_path / ".storage" / "ha_operator_intent.json"


@pytest.fixture
def store(snapshot_path: Path) -> IntentStore:
    return IntentStore(ExecutorHost(), snapshot_path)


async def wait_thread(event: threading.Event) -> None:
    """Wait without blocking the event loop or leaving runaway test threads."""
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 5), timeout=6)


def read_data(path: Path) -> dict:
    return json.loads(path.read_text())["data"]


async def test_first_load_and_full_state_round_trip(store, snapshot_path):
    await store.async_load()
    assert store.state == {
        "revision": 0,
        "manuals": {},
        "occurrences": {},
        "modes": {},
        "policy_enabled": {},
        "requests": {},
        "intents": {},
        "policy_inputs": {},
        "return_monitors": {},
    }
    assert not snapshot_path.exists()

    def accept(state):
        state["manuals"]["window"] = {
            "mode": "target",
            "target": {"position": 42.0},
            "expires_at": 200.0,
        }
        state["occurrences"]["morning:2026-10-25T01:00Z"] = {"status": "skipped", "expires_at": 300}
        state["modes"]["window"] = "live"
        state["policy_enabled"]["morning"] = False
        state["requests"]["r1"] = {"accepted": True, "resources": ["window"]}
        state["faults"] = {"fan": None}

    accepted = await store.async_update(accept)
    assert accepted["revision"] == 1
    assert read_data(snapshot_path) == accepted
    assert snapshot_path.stat().st_mode & 0o077 == 0
    reloaded = IntentStore(ExecutorHost(), snapshot_path)
    await reloaded.async_load(expected_existing=True)
    assert reloaded.state == accepted
    assert reloaded.fault is None
    # Repeated load does not overwrite the current committed state.
    snapshot_path.write_text("invalid")
    await reloaded.async_load(expected_existing=True)
    assert reloaded.state == accepted


async def test_missing_initialized_snapshot_inhibits(store, snapshot_path):
    with pytest.raises(IntentStoreError, match="load failed"):
        await store.async_load(expected_existing=True)
    assert store.fault
    with pytest.raises(IntentStoreError, match="load failed"):
        await store.async_update(lambda state: state.clear())
    with pytest.raises(IntentStoreError, match="load failed"):
        await store.async_load()
    assert not snapshot_path.exists()


async def test_v1_snapshot_upgrades_without_losing_existing_intent(store, snapshot_path):
    await store.async_load()
    await store.async_update(lambda state: state["modes"].update(roof="live"))
    envelope = json.loads(snapshot_path.read_text())
    envelope["version"] = 1
    envelope["data"].pop("intents")
    snapshot_path.write_text(json.dumps(envelope))
    loaded = IntentStore(ExecutorHost(), snapshot_path)
    await loaded.async_load(expected_existing=True)
    assert loaded.state == {**envelope["data"], "intents": {}}
    await loaded.async_update(lambda state: state["intents"].update(central=True, room=False))
    current = json.loads(snapshot_path.read_text())
    assert current["version"] == 3
    assert current["data"]["modes"] == {"roof": "live"}
    assert current["data"]["intents"] == {"central": True, "room": False}
    with pytest.raises(InvalidSnapshot, match="boolean"):
        await loaded.async_update(lambda state: state["intents"].update(room="off"))


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "null",
        "[]",
        "{}",
        "invalid",
        '{"version": true, "data": {}}',
        '{"version": 2, "data": {}}',
        '{"version": 1, "data": null}',
        '{"version": 1, "version": 1, "data": {}}',
        '{"version": 1, "data": {"revision": NaN}}',
        '{"version": 1, "data": {"revision": Infinity}}',
        '{"version": 1, "data": {"revision": -Infinity}}',
    ],
)
async def test_corrupt_or_unsupported_snapshot_preserved(store, snapshot_path, contents):
    snapshot_path.parent.mkdir()
    snapshot_path.write_text(contents)
    with pytest.raises(IntentStoreError, match="load failed"):
        await store.async_load()
    assert snapshot_path.read_text() == contents
    assert store.state["revision"] == 0
    assert store.fault is not None


async def test_read_permission_failure_inhibits(store, monkeypatch):
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(IntentStoreError, match="PermissionError"):
        await store.async_load()
    assert store.fault is not None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.pop("manuals"),
        lambda state: state.update(manuals=[]),
        lambda state: state.update(modes={"window": "unknown"}),
        lambda state: state.update(policy_enabled={"morning": 1}),
        lambda state: state.update(requests={1: "integer key"}),
        lambda state: state.update(requests={"bad": object()}),
        lambda state: state.update(requests={"bad": float("nan")}),
        lambda state: state.update(requests={"bad": float("inf")}),
        lambda state: state.update(requests={"bad": (1, 2)}),
        lambda state: state.update(recursive=state),
    ],
)
async def test_invalid_mutation_is_not_storage_fault(store, snapshot_path, mutation):
    await store.async_load()
    before = store.state
    with pytest.raises(InvalidSnapshot):
        await store.async_update(mutation)
    assert store.fault is None
    assert store.state == before
    assert not snapshot_path.exists()
    await store.async_update(lambda state: state["modes"].update(window="live"))
    assert store.state["revision"] == 1


@pytest.mark.parametrize("revision", [-1, True, 1.0, None, "1"])
async def test_invalid_loaded_revision_inhibits(store, snapshot_path, revision):
    data = store.state
    data["revision"] = revision
    snapshot_path.parent.mkdir()
    snapshot_path.write_text(json.dumps({"version": 1, "data": data}))
    with pytest.raises(IntentStoreError, match="InvalidSnapshot"):
        await store.async_load()


async def test_mutator_exception_does_not_publish_or_fault(store, snapshot_path):
    await store.async_load()

    def invalid_request(state):
        state["modes"]["window"] = "live"
        raise ValueError("request expired")

    with pytest.raises(ValueError, match="request expired"):
        await store.async_update(invalid_request)
    assert store.fault is None
    assert store.state["modes"] == {}
    assert not snapshot_path.exists()


async def test_detaches_mutator_argument_return_value_and_public_state(store, snapshot_path):
    await store.async_load()
    retained = []

    def retain(state):
        retained.append(state)
        state["requests"]["r1"] = {"accepted": True}

    result = await store.async_update(retain)
    retained[0]["requests"].clear()
    result["requests"].clear()
    view = store.state
    view["requests"].clear()
    assert store.state["requests"] == {"r1": {"accepted": True}}
    assert read_data(snapshot_path) == store.state


async def test_update_requires_load(store):
    with pytest.raises(IntentStoreError, match="not been loaded"):
        await store.async_update(lambda state: None)
    assert store.fault is None


@pytest.mark.parametrize("stage", ["before_write", "temporary", "before_rename", "after_rename"])
async def test_write_failure_never_acknowledges_or_overwrites_in_memory(
    store, snapshot_path, monkeypatch, stage
):
    await store.async_load()
    old = await store.async_update(lambda state: state["modes"].update(window="observe"))
    real_save = storage.save_json
    temporary = snapshot_path.with_suffix(".candidate")

    def fail(filename, data, private, atomic_writes):
        assert private is True and atomic_writes is True
        if stage in ("temporary", "before_rename"):
            temporary.write_text(json.dumps(data)[:8] if stage == "temporary" else json.dumps(data))
        elif stage == "after_rename":
            real_save(filename, data, private=private, atomic_writes=atomic_writes)
        raise OSError(stage)

    monkeypatch.setattr(storage, "save_json", fail)
    with pytest.raises(IntentStoreError, match="save failed"):
        await store.async_update(lambda state: state["modes"].update(window="live"))
    assert store.state == old
    assert store.fault is not None
    disk = read_data(snapshot_path)
    assert disk["modes"]["window"] == ("live" if stage == "after_rename" else "observe")
    with pytest.raises(IntentStoreError, match="save failed"):
        await store.async_update(lambda state: None)
    # A new process accepts only the complete version actually on disk.
    fresh = IntentStore(ExecutorHost(), snapshot_path)
    await fresh.async_load(expected_existing=True)
    assert fresh.state == disk


async def test_cancellation_serializes_writes_and_publishes_committed_revision(
    store, snapshot_path, monkeypatch
):
    await store.async_load()
    entered = threading.Event()
    release = threading.Event()
    revisions = []
    real_write = storage._write_snapshot

    def delayed_write(path, state):
        revisions.append(state["revision"])
        if state["revision"] == 1:
            entered.set()
            assert release.wait(5)
        real_write(path, state)

    monkeypatch.setattr(storage, "_write_snapshot", delayed_write)
    first = asyncio.create_task(
        store.async_update(lambda state: state["modes"].update(window="live"))
    )
    try:
        await wait_thread(entered)
        assert store.state["revision"] == 0
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()  # Repeated cancellation cannot drop the writer lock.
        second = asyncio.create_task(
            store.async_update(lambda state: state["requests"].update(second={"accepted": True}))
        )
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert revisions == [1]
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await second
    assert revisions == [1, 2]
    assert result["modes"] == {"window": "live"}
    assert result["requests"] == {"second": {"accepted": True}}
    assert read_data(snapshot_path) == result


async def test_cancelled_failed_write_inhibits(store, monkeypatch):
    await store.async_load()
    entered = threading.Event()
    release = threading.Event()

    def delayed_failure(path, state):
        entered.set()
        assert release.wait(5)
        raise OSError("uncertain write")

    monkeypatch.setattr(storage, "_write_snapshot", delayed_failure)
    task = asyncio.create_task(store.async_update(lambda state: None))
    try:
        await wait_thread(entered)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(IntentStoreError, match="save failed"):
        await task
    assert store.state["revision"] == 0
    assert store.fault


async def test_cancelled_load_finishes_before_propagating(store, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    loaded = deepcopy(store.state)
    loaded["revision"] = 9

    def delayed_read(path, expected_existing):
        entered.set()
        assert release.wait(5)
        return loaded

    monkeypatch.setattr(storage, "_read_snapshot", delayed_read)
    task = asyncio.create_task(store.async_load())
    try:
        await wait_thread(entered)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.state["revision"] == 9
    await store.async_update(lambda state: None)
    assert store.state["revision"] == 10


async def test_close_drains_write_even_when_close_is_cancelled(store, monkeypatch):
    await store.async_load()
    entered = threading.Event()
    release = threading.Event()

    def delayed_write(path, state):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(storage, "_write_snapshot", delayed_write)
    write = asyncio.create_task(store.async_update(lambda state: None))
    try:
        await wait_thread(entered)
        close = asyncio.create_task(store.async_close())
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not close.done()
        with pytest.raises(IntentStoreError, match="closed"):
            await store.async_update(lambda state: None)
    finally:
        release.set()
    await write
    with pytest.raises(asyncio.CancelledError):
        await close
    assert store.state["revision"] == 1
    with pytest.raises(IntentStoreError, match="closed"):
        await store.async_load()
    await store.async_close()


async def test_close_rejects_queued_mutation(store, monkeypatch):
    await store.async_load()
    entered = threading.Event()
    release = threading.Event()

    def delayed_write(path, state):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(storage, "_write_snapshot", delayed_write)
    first = asyncio.create_task(store.async_update(lambda state: None))
    try:
        await wait_thread(entered)
        queued = asyncio.create_task(store.async_update(lambda state: None))
        await asyncio.sleep(0)
        close = asyncio.create_task(store.async_close())
        await asyncio.sleep(0)
    finally:
        release.set()
    await first
    with pytest.raises(IntentStoreError, match="closed"):
        await queued
    await close
    assert store.state["revision"] == 1


@pytest.mark.parametrize(
    ("stage", "expected_mode"),
    [
        ("get_fileobject", "observe"),
        ("sync", "observe"),
        ("commit", "observe"),
        ("directory_sync", "live"),
    ],
)
async def test_real_atomic_writer_failure_boundaries(
    store, snapshot_path, monkeypatch, stage, expected_mode
):
    """Inject into HA's actual atomic writer, including directory fsync after rename."""
    import atomicwrites

    await store.async_load()
    old = await store.async_update(lambda state: state["modes"].update(window="observe"))

    def fail(*args, **kwargs):
        raise OSError("injected filesystem error")

    if stage == "directory_sync":
        monkeypatch.setattr(atomicwrites, "_sync_directory", fail)
    else:
        monkeypatch.setattr(atomicwrites.AtomicWriter, stage, fail)
    with pytest.raises(IntentStoreError, match="WriteError"):
        await store.async_update(lambda state: state["modes"].update(window="live"))
    assert store.fault is not None
    assert store.state == old
    assert read_data(snapshot_path)["modes"]["window"] == expected_mode
    assert list(snapshot_path.parent.iterdir()) == [snapshot_path]


@pytest.mark.parametrize("version", [1, 2])
async def test_old_snapshot_adds_empty_input_maps_preserving_intent(store, snapshot_path, version):
    await store.async_load()
    await store.async_update(lambda state: state["modes"].update(roof="live"))
    envelope = json.loads(snapshot_path.read_text())
    envelope["version"] = version
    envelope["data"].pop("policy_inputs")
    envelope["data"].pop("return_monitors")
    if version == 1:
        envelope["data"].pop("intents")
    snapshot_path.write_text(json.dumps(envelope))
    loaded = IntentStore(ExecutorHost(), snapshot_path)
    await loaded.async_load(expected_existing=True)
    assert loaded.state["modes"] == {"roof": "live"}
    assert loaded.state["policy_inputs"] == loaded.state["return_monitors"] == {}


@pytest.mark.parametrize(
    "record",
    [
        {"type": "other", "fingerprint": "a" * 64, "state": {}},
        {"type": "qualified_numeric", "fingerprint": "invalid", "state": {}},
        {"type": "qualified_numeric", "fingerprint": "a" * 64, "state": {"qualified": True}},
        {"type": "timer_episode", "fingerprint": "a" * 64, "state": {"phase": "accepted"}},
    ],
)
async def test_corrupt_typed_input_never_publishes(store, snapshot_path, record):
    await store.async_load()
    with pytest.raises(InvalidSnapshot, match="policy input"):
        await store.async_update(lambda state: state["policy_inputs"].update(cold=record))
    assert store.state["policy_inputs"] == {}
    assert not snapshot_path.exists()
    envelope = {"version": 3, "data": store.state}
    envelope["data"]["policy_inputs"]["cold"] = record
    snapshot_path.parent.mkdir(exist_ok=True)
    snapshot_path.write_text(json.dumps(envelope))
    loaded = IntentStore(ExecutorHost(), snapshot_path)
    with pytest.raises(IntentStoreError):
        await loaded.async_load()
    assert loaded.fault
    assert loaded.state["policy_inputs"] == {}
