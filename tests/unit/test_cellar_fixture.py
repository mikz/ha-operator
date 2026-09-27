"""The public legacy summary preserves intervals without publishing calendar dates."""

import json
import re
from decimal import Decimal
from pathlib import Path


def test_cellar_observation_fixture_is_rebased_and_preserves_intervals():
    path = Path(__file__).resolve().parents[1] / "lab/fixtures/cellar_observations.json"
    text = path.read_text()
    fixture = json.loads(text)
    assert fixture["provenance"] == "rebased_sanitized_legacy_observation_summary"
    assert fixture["time_basis"] == {
        "clock": "relative_seconds",
        "origin": "synthetic_zero",
        "calendar_dates": "removed",
        "within_fixture_intervals": "preserved",
    }
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)
    assert "source_capture_date" not in fixture
    manual, availability = fixture["records"]
    assert "started_at" not in manual and "ended_at" not in manual

    def duration(pair):
        return Decimal(str(pair[1])) - Decimal(str(pair[0]))

    assert duration((manual["started_at_offset_seconds"], manual["ended_at_offset_seconds"])) == (
        Decimal("141694.329185")
    )
    observed = availability["observed"]
    assert [
        duration(pair) for pair in observed["direction_unavailable_intervals_offset_seconds"]
    ] == [Decimal("10.349"), Decimal("0.912")]
    assert [duration(pair) for pair in observed["power_unavailable_intervals_offset_seconds"]] == [
        Decimal("10.343"),
        Decimal("0.701"),
    ]
    assert fixture["physical_effects"] == "not_observed"
    assert fixture["operator_admissions"] == "not_observed"
