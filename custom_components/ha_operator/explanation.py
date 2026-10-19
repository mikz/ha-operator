"""Bounded native Script traces of existing Operator decision snapshots."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextvars import Context as ExecutionContext
from typing import Any

from homeassistant.components.script.trace import trace_script
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script
from homeassistant.helpers.trace import trace_get, trace_path

_LOGGER = logging.getLogger(__name__)
# Facts are run variables, never action configuration/Jinja templates.
_SEQUENCE = cv.SCRIPT_SCHEMA([{"alias": "Operator decision snapshot", "variables": {}}])


def create_script(hass: HomeAssistant) -> Script:
    return Script(
        hass,
        _SEQUENCE,
        "Operator decision snapshot",
        "ha_operator",
        script_mode="parallel",
        max_runs=1,
    )


def start_trace(
    hass: HomeAssistant,
    script: Script,
    entity_id: str,
    unique_id: str,
    parent: Context | None,
    facts: dict[str, Any],
    still_current: Callable[[], bool],
) -> tuple[Context | None, asyncio.Task[None]]:
    """Start a real native Script before this same synchronous publication.

    Future HA changes that defer the start hook must not attribute a later state
    write or re-publish an obsolete snapshot. Control never waits for this trace.
    """
    context = Context(parent_id=parent.id if parent else None)
    active = True
    started = False

    def actual_start() -> None:
        nonlocal started
        if active and still_current():
            started = True
            hass.bus.async_fire(
                "script_started",
                {"name": "Operator decision snapshot", "entity_id": entity_id},
                context=context,
            )

    async def run() -> None:
        try:
            with trace_script(
                hass,
                unique_id,
                {"alias": "Operator decision snapshot", "sequence": _SEQUENCE},
                None,
                context,
                {"stored_traces": 5},
            ) as trace:
                trace.set_trace(trace_get())
                with trace_path("sequence"):
                    await script.async_run(facts, context, started_action=actual_start)
        except Exception:
            _LOGGER.debug("Native explanation trace unavailable", exc_info=True)

    # Do not inherit/mutate an unrelated native trace stack in the caller task.
    pending = run()
    try:
        task = ExecutionContext().run(
            hass.async_create_task, pending, "ha_operator:explanation", eager_start=True
        )
    except BaseException:
        pending.close()
        raise
    active = False
    return (context if started and still_current() else None), task
