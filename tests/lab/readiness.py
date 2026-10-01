"""Bounded readiness at native frontend and config-entry boundaries."""

from __future__ import annotations

import asyncio
import time


async def wait_native_tokens(page):
    """Navigation can finish before HA's asynchronous auth bootstrap saves tokens."""
    handle = await page.wait_for_function(
        """() => {
            try {
                const tokens = JSON.parse(localStorage.getItem('hassTokens'));
                return tokens && typeof tokens.access_token === 'string'
                    && tokens.access_token.length > 0 ? tokens : false;
            } catch {
                return false;
            }
        }""",
        timeout=30_000,
    )
    try:
        return await handle.json_value()
    finally:
        await handle.dispose()


async def wait_operator_ready(
    ha,
    entry_id,
    *,
    locked,
    timeout=30,  # noqa: ASYNC109 -- bounded native readiness polling
    interval=0.2,
):
    """Check loaded state and explanation after the awaited native reload."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        entries = await ha.ws("config_entries/get")
        entry = next((item for item in entries if item["entry_id"] == entry_id), None)
        last = {"entry_state": entry.get("state") if entry else None}
        if entry is not None and entry.get("state") == "loaded":
            explained = await ha.service(
                "ha_operator", "explain", {"config_entry_id": entry_id}, response=True
            )
            result = explained["service_response"]
            last["shadow_locked"] = result.get("shadow_locked")
            if last["shadow_locked"] is locked:
                return result
        await asyncio.sleep(interval)
    raise AssertionError(f"Operator did not become ready after native reload: {last!r}")
