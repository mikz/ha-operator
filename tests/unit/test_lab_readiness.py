"""Reload readiness cannot mistake the previous runtime or permanent API errors for success."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.lab.readiness import wait_native_tokens, wait_operator_ready
from tests.lab.runner import HA
from tests.lab.scenarios_observe import observe


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
    def __init__(self, states, *, locked=True):
        self.states = iter(states)
        self.locked = locked
        self.calls = []

    async def ws(self, command):
        state = next(self.states, "loaded")
        self.calls.append(("entry", state))
        return [{"entry_id": "test", "state": state}]

    async def service(self, domain, service, data, *, response):
        self.calls.append(("service", service))
        return {"service_response": {"shadow_locked": self.locked}}


async def test_waits_for_native_loaded_entry_and_expected_lock():
    ha = PendingHA(["unload_in_progress", "loaded"])
    result = await wait_operator_ready(ha, "test", locked=True, interval=0)
    assert result["shadow_locked"]
    assert ha.calls == [
        ("entry", "unload_in_progress"),
        ("entry", "loaded"),
        ("service", "explain"),
    ]


async def test_wrong_lock_never_becomes_ready():
    with pytest.raises(AssertionError, match="did not become ready"):
        await wait_operator_ready(
            PendingHA([], locked=False), "test", locked=True, timeout=0.002, interval=0
        )


async def test_permanent_service_error_is_not_suppressed():
    ha = PendingHA([])
    ha.service = AsyncMock(side_effect=AssertionError("persistent HTTP500"))
    with pytest.raises(AssertionError, match="persistent HTTP500"):
        await wait_operator_ready(ha, "test", locked=True, interval=0)


async def test_options_setup_awaits_explicit_reload_before_readiness(monkeypatch):
    ha = PendingHA([])

    async def options(entry, data):
        ha.calls.append(("options", data["shadow_lock"]))
        ha.calls.append(("reload", "completed"))

    async def ready(actual, entry, *, locked):
        assert actual is ha and entry == "test" and locked
        assert ha.calls[-1] == ("reload", "completed")

    ha.options = options
    monkeypatch.setattr("tests.lab.scenarios_observe.wait_operator_ready", ready)
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
        await ha.options("test", {"shadow_lock": True})
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


def native_diagnostics():
    return {
        "resources": {
            "a" * 12: {
                "return_monitor": {
                    "phase": "overdue",
                    "target_position": 7,
                    "due_at": 300,
                    "overdue": True,
                }
            }
        },
        "policies": {
            "b" * 12: {"enabled": True, "input": None},
            "c" * 12: {"enabled": False},
            "d" * 12: {
                "enabled": True,
                "input": {
                    "type": "qualified_numeric",
                    "entity": "e" * 12,
                    "qualification_seconds": 1800,
                    "threshold": 16,
                    "comparison": "below",
                    "unit": "f" * 12,
                    "state": {
                        "phase": "recovering",
                        "due_at": 300,
                        "qualified": True,
                        "source_quality": "unknown",
                        "recovery_pending": True,
                    },
                },
            },
            "e" * 12: {
                "enabled": True,
                "input": {
                    "type": "timer_episode",
                    "entity": "f" * 12,
                    "qualification_seconds": 60,
                    "request_seconds": 1800,
                    "state": {
                        "phase": "accepted",
                        "due_at": None,
                        "expires_at": 1860,
                        "finish_at": 600,
                        "episode_id": "a" * 12,
                    },
                },
            },
        },
    }


def test_native_diagnostics_accepts_only_known_anonymized_records():
    from tests.lab.runner import validate_native_diagnostics

    data = native_diagnostics()
    validate_native_diagnostics(data)
    data["resources"]["a" * 12].pop("return_monitor")
    data["policies"]["d" * 12]["input"]["state"] = None
    data["policies"]["e" * 12]["input"]["state"]["episode_id"] = None
    validate_native_diagnostics(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "secret-status"),
        ("overdue", 1),
        ("due_at", float("nan")),
        ("target_position", "7"),
        ("secret", "private-name"),
    ],
)
def test_native_return_diagnostics_rejects_unknown_fields_and_types(field, value):
    from tests.lab.runner import validate_native_diagnostics

    data = native_diagnostics()
    data["resources"]["a" * 12]["return_monitor"][field] = value
    with pytest.raises(AssertionError):
        validate_native_diagnostics(data)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("numeric", "entity", "sensor.private_temperature"),
        ("numeric", "unit", "°C"),
        ("numeric", "threshold", True),
        ("numeric", "comparison", "above"),
        ("numeric", "type", "other"),
        ("numeric", "secret", "private-name"),
        ("timer", "request_seconds", float("inf")),
    ],
)
def test_native_input_diagnostics_rejects_plain_identifiers_and_unknown_fields(kind, field, value):
    from tests.lab.runner import validate_native_diagnostics

    data = native_diagnostics()
    key = "d" if kind == "numeric" else "e"
    data["policies"][key * 12]["input"][field] = value
    with pytest.raises(AssertionError):
        validate_native_diagnostics(data)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("numeric", "qualified", 1),
        ("numeric", "source_quality", "private-quality"),
        ("numeric", "recovery_pending", 1),
        ("numeric", "phase", "private-phase"),
        ("timer", "episode_id", "plain-episode-id"),
        ("timer", "expires_at", "1860"),
        ("timer", "phase", "private-phase"),
        ("timer", "secret", "private-name"),
    ],
)
def test_native_input_state_diagnostics_rejects_unrecognized_records(kind, field, value):
    from tests.lab.runner import validate_native_diagnostics

    data = native_diagnostics()
    key = "d" if kind == "numeric" else "e"
    data["policies"][key * 12]["input"]["state"][field] = value
    with pytest.raises(AssertionError):
        validate_native_diagnostics(data)
