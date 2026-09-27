"""Bounded readiness after an awaited native config-entry reload."""

from __future__ import annotations

import asyncio
import time


async def wait_trace_ready(
    ha,
    entry_id,
    previous_session,
    *,
    locked,
    timeout=30,  # noqa: ASYNC109 -- polling owns a bounded readiness deadline
    interval=0.2,
):
    """Require a new recorder instance, not a stale pre-reload loaded state."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        entries = await ha.ws("config_entries/get")
        entry = next((item for item in entries if item["entry_id"] == entry_id), None)
        last = {"entry_state": entry.get("state") if entry else None}
        if entry is not None and entry.get("state") == "loaded":
            result = await ha.service(
                "ha_operator",
                "export_trace",
                {"config_entry_id": entry_id, "limit": 1},
                response=True,
            )
            health = result["service_response"]["health"]
            last.update(enabled=health["enabled"], session_id=health["session_id"])
            if health["enabled"] and health["session_id"] != previous_session:
                explained = await ha.service(
                    "ha_operator", "explain", {"config_entry_id": entry_id}, response=True
                )
                last["shadow_locked"] = explained["service_response"].get("shadow_locked")
                if last["shadow_locked"] is locked:
                    return health
        await asyncio.sleep(interval)
    raise AssertionError(f"Trace did not become ready after native reload: {last!r}")
