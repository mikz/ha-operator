"""Native HA condition compilation, evaluation evidence, and owned cleanup."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import Context
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import condition
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.template import Template
from homeassistant.helpers.trace import trace_path

type ConditionStatus = Literal["passed", "failed", "unknown", "skipped"]
type HealthStatus = Literal["healthy", "unhealthy", "unknown", "waiting", "skipped"]


@dataclass(frozen=True)
class ConditionEvaluation:
    """A native result and factual source evidence, never a physical diagnosis."""

    status: ConditionStatus
    conditions: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    @property
    def permits_command(self) -> bool:
        return self.status in {"passed", "skipped"}


class NativeConditionList:
    """Own individually compiled native checkers so native errors remain explicit."""

    def __init__(self, hass: HomeAssistant, configs: list[Any]) -> None:
        self.hass = hass
        self.configs = deepcopy(configs)
        self._checkers: list[condition.ConditionChecker] = []
        self._validated: list[dict[str, Any]] = []
        self._enabled: list[bool] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self._error: str | None = None
        self._closed = False
        self._ready = False

    async def async_setup(self) -> None:
        """Resolve native targets and compile once, retaining a fail-closed error."""
        try:
            for config in cv.CONDITIONS_SCHEMA(deepcopy(self.configs)):
                validated = await condition.async_validate_condition_config(self.hass, config)
                enabled = _prepare_enabled(validated)
                self._checkers.append(await condition.async_from_config(self.hass, validated))
                self._validated.append(validated)
                self._enabled.append(enabled)
            self._ready = True
        except BaseException as err:
            for checker in self._checkers:
                checker.async_unload()
            self._checkers.clear()
            self._validated.clear()
            self._enabled.clear()
            if not isinstance(err, Exception):
                raise
            self._error = f"{type(err).__name__}: {err}"

    @property
    def setup_error(self) -> str | None:
        return self._error

    @property
    def needs_polling(self) -> bool:
        return any(checker.needs_polling for checker in self._checkers)

    async def async_track_changes(self, action: Callable[[], None]) -> None:
        """Track native dependencies; the runtime owns any required polling timer."""
        try:
            for checker in self._checkers:
                self._unsubscribers.append(await checker.async_track_changes(action))
        except BaseException:
            self.async_unload()
            raise

    @callback
    def async_unload(self) -> None:
        self._closed = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for checker in self._checkers:
            checker.async_unload()
        self._checkers.clear()

    @callback
    def evaluate(self) -> ConditionEvaluation:
        """Use HA's condition semantics, including native compound short-circuiting."""
        if self._closed or self._error or not self._ready:
            return ConditionEvaluation("unknown", error=self._error or "Condition list not ready")
        results: list[dict[str, Any]] = []
        statuses: set[ConditionStatus] = set()
        for index, (checker, config) in enumerate(
            zip(self._checkers, self._validated, strict=True)
        ):
            sources = self._sources(config)
            error = None
            try:

                def check(
                    checker: condition.ConditionChecker = checker, index: int = index
                ) -> bool | None:
                    with trace_path(str(index)):
                        return checker.async_check()

                result = Context().run(check)
                status: ConditionStatus = (
                    "skipped"
                    if result is None
                    else "failed"
                    if result is False
                    else "skipped"
                    if not self._enabled[index]
                    else "passed"
                )
                if (
                    status == "failed"
                    and config.get("condition") in {"state", "numeric_state"}
                    and any(not source["available"] for source in sources.values())
                ):
                    status = "unknown"
            except Exception as err:
                status = "unknown"
                error = f"{type(err).__name__}: {err}"
            statuses.add(status)
            results.append(
                {
                    "config": deepcopy(self.configs[index]),
                    "status": status,
                    "error": error,
                    "sources": sources,
                    "source_scope": "native_static_references",
                }
            )
        # A definite failed AND member remains failed even if another member errors.
        overall: ConditionStatus = "skipped"
        for value in ("failed", "unknown", "passed"):
            if value in statuses:
                overall = value
                break
        return ConditionEvaluation(overall, tuple(results))

    def _sources(self, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        sources: dict[str, dict[str, Any]] = {}

        def capture(item: dict[str, Any]) -> None:
            for entity_id in sorted(condition.async_extract_entities(item)):
                native = self.hass.states.get(entity_id)
                unit = native.attributes.get("unit_of_measurement") if native else None
                name = native.name if native else None
                source = sources.setdefault(
                    entity_id,
                    {
                        "state": native.state if native else None,
                        "available": native is not None
                        and native.state not in {"unknown", "unavailable"},
                        "name": name[:128]
                        if isinstance(name, str)
                        else entity_id
                        if native
                        else None,
                        "unit": unit[:32] if isinstance(unit, str) else None,
                        "updated_at": native.last_updated.isoformat() if native else None,
                    },
                )
                if native and (attribute := item.get("attribute")):
                    value = native.attributes.get(attribute)
                    source.setdefault("attributes", {})[attribute] = (
                        value[:255]
                        if isinstance(value, str)
                        else value
                        if isinstance(value, (int, float, bool)) or value is None
                        else None
                    )
            for child in item.get("conditions", []):
                capture(child)

        capture(config)
        return sources


def _prepare_enabled(config: dict[str, Any]) -> bool:
    """Match HA's setup-time enabled handling and count enabled evidence leaves."""
    enabled = config.get("enabled", True)
    if isinstance(enabled, Template):
        enabled = config["enabled"] = enabled.async_render(limited=True)
    if not enabled:
        return False
    if config.get("condition") in {"and", "or", "not"}:
        # Visit every child: short-circuiting here would leave enabled templates unresolved.
        children = [_prepare_enabled(child) for child in config["conditions"]]
        return any(children)
    return True


def health_status(
    conditions: ConditionEvaluation, qualification: ConditionEvaluation | None = None
) -> HealthStatus:
    """Qualification defers a check; it never supplies positive health evidence."""
    if qualification is not None:
        if qualification.status == "failed":
            return "waiting"
        if qualification.status in {"unknown", "skipped"}:
            return qualification.status
    if conditions.status == "passed":
        return "healthy"
    if conditions.status == "failed":
        return "unhealthy"
    return conditions.status


def aggregate_health(statuses: Iterable[HealthStatus]) -> HealthStatus:
    """Keep known failure visible while uncertainty or qualification remains."""
    values = set(statuses)
    for value in ("unhealthy", "unknown", "waiting", "skipped"):
        if value in values:
            return value
    return "healthy" if values else "skipped"
