"""Reload readiness cannot mistake the previous runtime or permanent API errors for success."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.lab.readiness import wait_native_tokens, wait_trace_ready
from tests.lab.runner import HA
from tests.lab.scenarios_shadow import observe


async def test_native_tokens_wait_for_storage_condition_and_dispose_handle():
    tokens = {"access_token": "synthetic-test-value"}
    handle = SimpleNamespace(json_value=AsyncMock(return_value=tokens), dispose=AsyncMock())
    page = SimpleNamespace(wait_for_function=AsyncMock(return_value=handle))
    assert await wait_native_tokens(page) == tokens
    assert page.wait_for_function.call_args.kwargs == {"timeout": 30_000}
    handle.dispose.assert_awaited_once()


async def test_native_tokens_missing_storage_propagates_readiness_timeout():
    page = SimpleNamespace(wait_for_function=AsyncMock(side_effect=TimeoutError("not ready")))
    with pytest.raises(TimeoutError, match="not ready"):
        await wait_native_tokens(page)


class PendingHA:
    def __init__(self, states, sessions, *, enabled=True, locked=True):
        self.states = iter(states)
        self.sessions = iter(sessions)
        self.enabled, self.locked = enabled, locked
        self.calls = []

    async def ws(self, command):
        state = next(self.states, "loaded")
        self.calls.append(("entry", state))
        return [{"entry_id": "test", "state": state}]

    async def service(self, domain, service, data, *, response):
        self.calls.append(("service", service))
        value = (
            {"health": {"enabled": self.enabled, "session_id": next(self.sessions, "new")}}
            if service == "export_trace"
            else {"shadow_locked": self.locked}
        )
        return {"service_response": value}


async def test_waits_for_loaded_entry_and_changed_trace_session():
    ha = PendingHA(["unload_in_progress", "loaded", "loaded"], ["old", "new"])
    result = await wait_trace_ready(ha, "test", "old", locked=True, interval=0)
    assert result["session_id"] == "new"
    assert ha.calls == [
        ("entry", "unload_in_progress"),
        ("entry", "loaded"),
        ("service", "export_trace"),
        ("entry", "loaded"),
        ("service", "export_trace"),
        ("service", "explain"),
    ]


@pytest.mark.parametrize("enabled,locked", [(False, True), (True, False)])
async def test_disabled_trace_or_wrong_lock_never_becomes_ready(enabled, locked):
    ha = PendingHA([], [], enabled=enabled, locked=locked)
    with pytest.raises(AssertionError, match="did not become ready"):
        await wait_trace_ready(ha, "test", "old", locked=True, timeout=0.002, interval=0)


async def test_permanent_service_error_is_not_suppressed():
    ha = PendingHA([], [])

    async def fail(*args, **kwargs):
        raise AssertionError("persistent HTTP500 while loaded")

    ha.service = fail
    with pytest.raises(AssertionError, match="persistent HTTP500"):
        await wait_trace_ready(ha, "test", "old", locked=True, interval=0)


async def test_options_setup_awaits_explicit_reload_before_readiness(monkeypatch):
    ha = PendingHA([], ["old"])

    async def options(entry, data):
        ha.calls.append(("options", data["shadow_lock"]))
        ha.calls.append(("reload", "completed"))

    async def ready(actual, entry, previous, *, locked):
        assert actual is ha and entry == "test" and previous == "old" and locked
        assert ha.calls[-1] == ("reload", "completed")

    ha.options = options
    monkeypatch.setattr("tests.lab.scenarios_shadow.wait_trace_ready", ready)
    await observe(SimpleNamespace(ha=ha, entry="test"), locked=True)


@pytest.mark.parametrize("kind", ["options", "subentry"])
async def test_native_flow_helpers_do_not_return_before_reload_and_loaded_state(kind):
    class FlowHA(HA):
        def __init__(self):
            self.calls = []
            self.loaded_checks = 0

        async def request(self, method, path, data=None, **kwargs):
            self.calls.append(path)
            if path.endswith("/reload"):
                return {"require_restart": False}
            if path.endswith("/flow"):
                return {"flow_id": "test-flow-id"}
            return {"type": "create_entry"}

        async def ws(self, command, **fields):
            self.calls.append(command)
            if command == "config_entries/get":
                self.loaded_checks += 1
                return [{"entry_id": "test", "state": "loaded"}]
            assert self.loaded_checks == 1
            return [{"title": "Resource", "subentry_id": "resource"}]

    ha = FlowHA()
    if kind == "options":
        await ha.options("test", {"trace_enabled": True})
    else:
        assert await ha.add_subentry("test", "resource", {"name": "Resource"}) == "resource"
    reload_index = ha.calls.index("/api/config/config_entries/entry/test/reload")
    assert ha.calls[reload_index + 1] == "config_entries/get"
    assert ha.loaded_checks == 1


@pytest.mark.parametrize("source_type", ["qualified_numeric", "timer_episode"])
@pytest.mark.parametrize("reconfigure", [False, True])
async def test_native_policy_helper_submits_two_forms_and_keeps_id(source_type, reconfigure):
    class PolicyHA(HA):
        def __init__(self):
            self.requests = []
            self.loaded = False

        async def request(self, method, path, data=None, **kwargs):
            self.requests.append((path, data))
            if path.endswith("/reload"):
                self.loaded = True
                return {}
            if path == "/api/config/config_entries/subentries/flow":
                return {"flow_id": "flow"}
            if data.get("input_type"):
                assert "input" not in data
                return {"type": "form", "step_id": source_type, "errors": {}}
            assert "type" not in data and "comparison" not in data
            expected = "sensor.raw" if source_type == "qualified_numeric" else "timer.public"
            assert data["entity_id"] == expected
            return (
                {"type": "abort", "reason": "reconfigure_successful"}
                if reconfigure
                else {"type": "create_entry"}
            )

        async def ws(self, command, **kwargs):
            assert self.loaded
            if command == "config_entries/get":
                return [{"entry_id": "entry", "state": "loaded"}]
            return [{"subentry_id": "stable-policy", "title": "Policy"}]

    ha = PolicyHA()
    data = {
        "name": "Policy",
        "input": {
            "type": source_type,
            "entity_id": "sensor.raw" if source_type == "qualified_numeric" else "timer.public",
            "qualification_seconds": 2,
            **(
                {"comparison": "below", "threshold": 16, "unit": "°C"}
                if source_type == "qualified_numeric"
                else {"request_seconds": 12}
            ),
        },
    }
    if reconfigure:
        assert (
            await ha.reconfigure_subentry("entry", "stable-policy", "policy", data)
            == "stable-policy"
        )
        assert ha.requests[0][1]["subentry_id"] == "stable-policy"
    else:
        assert await ha.add_subentry("entry", "policy", data) == "stable-policy"
    assert len([path for path, _ in ha.requests if path.endswith("/flow/flow")]) == 2
    assert data["input"]["type"] == source_type
