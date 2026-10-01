"""Bounded selection provenance, separate from execution and physical evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .core import Decision, ManualLease, Occurrence, Target
from .data import PolicyConfig, RequirementConfig


def target_label(target: Target | None) -> str:
    """Human-readable target for Activity; the exact target remains on entities."""
    if target is None:
        return "none"
    if target.position is not None:
        return f"{target.position:g}%"
    if target.profile is not None:
        return target.profile
    if target.on is False:
        return "off"
    if target.percentage is not None:
        return f"{target.percentage:g}%" + (f" {target.direction}" if target.direction else "")
    return target.direction or "on"


@dataclass(frozen=True)
class Selection:
    """One evaluation's cause; no changing clocks or arbitrary entity attributes."""

    source_kind: str = "idle"
    source_id: str | None = None
    source_name: str | None = None
    selection_reason: str = "No eligible request"
    request_id: str | None = None
    occurrence_id: str | None = None
    expires_at: float | None = None
    related_entities: tuple[str, ...] = ()
    active_inputs: tuple[str, ...] = ()

    def attributes(self) -> dict[str, object]:
        return asdict(self)


def selection_for(
    decision: Decision,
    manual: ManualLease | None,
    policies: Mapping[str, PolicyConfig],
    occurrences: list[Occurrence],
    requirements: Mapping[str, RequirementConfig],
    activations: dict[str, tuple[tuple[str, str], ...]],
) -> Selection:
    """Match configured identities exactly, including occurrence IDs containing colons."""
    source = decision.source
    if source is None:
        return Selection()
    if manual and source == f"manual:{manual.source}":
        name = "Hands off" if manual.mode == "hands_off" else "Manual target"
        return Selection(
            "manual",
            manual.source,
            name,
            name,
            manual.request_id,
            expires_at=manual.expires_at,
        )
    for key, config in policies.items():
        occurrence = next(
            (
                item
                for item in occurrences
                if item.policy_id == key and source == f"policy:{key}:{item.occurrence_id}"
            ),
            None,
        )
        if source == f"policy:{key}" or occurrence:
            name = config["name"][:128]
            return Selection(
                "occurrence" if occurrence else "policy",
                key,
                name,
                name,
                occurrence_id=occurrence.occurrence_id if occurrence else None,
                expires_at=occurrence.expires_at if occurrence else None,
                related_entities=tuple(
                    dict.fromkeys(
                        entity_id
                        for entity_id in (
                            config.get("eligibility_entity"),
                            config.get("target_entity"),
                            config["input"]["entity_id"] if "input" in config else None,
                        )
                        if entity_id
                    )
                ),
            )
    for key, requirement in requirements.items():
        for provider in requirement["providers"]:
            if source != f"requirement:{key}:{provider['id']}":
                continue
            active = activations.get(key, ())
            name = requirement["name"][:128]
            reason = f"Airflow: {name}"
            if active:
                reason += " — " + ", ".join(label for _, label in active)
            return Selection(
                "requirement",
                key,
                name,
                reason[:255],
                related_entities=tuple(requirement["activation_entities"][:32]),
                active_inputs=tuple(entity_id for entity_id, _ in active),
            )
    return Selection("unknown", source, selection_reason="Source unavailable")
