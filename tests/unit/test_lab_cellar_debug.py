"""The bounded cellar run establishes feedback before using the unchanged cases."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.lab import runner, scenarios_cellar, scenarios_observe


@pytest.mark.asyncio
async def test_standalone_cellar_confirms_extraction_before_running_cases(monkeypatch):
    lab = SimpleNamespace(
        ha=SimpleNamespace(service=AsyncMock(), state=AsyncMock()), physical=AsyncMock()
    )
    checks = []

    async def wait(read, accepts):
        await read()
        checks.append(accepts)

    observe = AsyncMock()
    cases = AsyncMock()
    monkeypatch.setattr(runner, "eventually", wait)
    monkeypatch.setattr(scenarios_observe, "observe", observe)
    monkeypatch.setattr(scenarios_cellar, "run_cellar_scenarios", cases)
    await scenarios_cellar.run_cellar_debug(lab)

    assert [call.args for call in lab.ha.service.await_args_list] == [
        ("fan", "set_direction", {"entity_id": "fan.sim_exhaust", "direction": "forward"}),
        ("fan", "turn_on", {"entity_id": "fan.sim_exhaust", "percentage": 100}),
    ]
    lab.physical.assert_awaited_once_with("exhaust")
    lab.ha.state.assert_awaited_once_with("fan.sim_exhaust")
    physical = {"on": True, "percentage": 100, "direction": "forward"}
    feedback = {"state": "on", "attributes": {"percentage": 100, "direction": "forward"}}
    assert checks[0](physical)
    assert checks[1](feedback)
    assert not checks[0]({**physical, "on": False})
    assert not checks[0]({**physical, "percentage": 50})
    assert not checks[1]({**feedback, "state": "off"})
    assert not checks[1]({**feedback, "attributes": {"percentage": 100}})
    observe.assert_awaited_once_with(lab, locked=False)
    cases.assert_awaited_once_with(lab)
