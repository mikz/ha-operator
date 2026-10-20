"""Own native trigger registrations; admission stays in the existing runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import trigger

from .async_utils import async_settle
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .runtime import OperatorRuntime


class PolicyTriggers:
    """One registration epoch, fenced before readiness and permanently on close."""

    def __init__(self, runtime: OperatorRuntime, policy_id: str) -> None:
        self.runtime, self.policy_id = runtime, policy_id
        self.closed = False
        self.ready = False
        self.failed = False
        self.removers: list[Callable[[], None]] = []
        self.task = runtime.hass.async_create_task(
            self._setup(), f"ha_operator:triggers:{policy_id}", eager_start=False
        )
        self.task.add_done_callback(
            lambda _: runtime._trigger_epochs.discard(self) if self.closed else None
        )

    @callback
    def close(self) -> None:
        self.closed = True
        self.ready = False
        for remove in self.removers:
            remove()
        self.removers.clear()
        if self.task.done():
            self.runtime._trigger_epochs.discard(self)

    async def async_close(self) -> None:
        self.close()
        _, cancelled = await async_settle(self.task)
        self.close()
        if cancelled:
            raise asyncio.CancelledError

    async def _setup(self) -> None:
        runtime, policy_id = self.runtime, self.policy_id
        config = runtime.policies[policy_id]
        logged_error = False

        def log(level: int, message: str, **kwargs: Any) -> None:
            nonlocal logged_error
            logged_error |= level >= logging.ERROR
            _LOGGER.log(level, "%s: %s", config["name"], message, **kwargs)

        def action(cancel: bool) -> Callable[..., Any]:
            @callback
            def received(variables: dict[str, Any], context: Context | None = None) -> None:
                if not self.ready or self.closed:
                    return
                # Capture facts before scheduling; no rejected callback fences an actuator.
                runtime.trigger_occurrence(self, variables, context, cancel=cancel)

            return received

        incomplete_cleanup = False
        try:
            for field, cancel in (("triggers", False), ("cancel_triggers", True)):
                configs = await trigger.async_validate_trigger_config(
                    runtime.hass, cv.TRIGGER_SCHEMA(config.get(field, []))
                )
                if self.closed:
                    return
                try:
                    remove = await trigger.async_initialize_triggers(
                        runtime.hass, configs, action(cancel), DOMAIN, config["name"], log
                    )
                except Exception:
                    # Native lookup can raise after eager attachment but before a remover.
                    incomplete_cleanup = True
                    raise
                if remove:
                    self.removers.append(remove)
                if logged_error:
                    raise RuntimeError("Native trigger attachment failed")
                if self.closed:
                    return
            self.ready = True
            issue_id = f"policy_triggers_{runtime.entry.entry_id}_{policy_id}"
            issue = ir.async_get(runtime.hass).async_get_issue(DOMAIN, issue_id)
            if issue is not None and issue.translation_key != "policy_trigger_cleanup":
                ir.async_delete_issue(runtime.hass, DOMAIN, issue_id)
            runtime._queue_reconciliation({config["resource_id"]})
        except Exception:
            _LOGGER.exception("Native trigger setup failed for %s", config["name"])
            self.failed = True
            self.close()
            if not runtime._closed:
                ir.async_create_issue(
                    runtime.hass,
                    DOMAIN,
                    f"policy_triggers_{runtime.entry.entry_id}_{policy_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="policy_trigger_cleanup"
                    if incomplete_cleanup
                    else "policy_trigger_setup",
                )
        finally:
            if self.closed:
                self.close()
