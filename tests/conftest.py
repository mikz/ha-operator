"""Common Home Assistant fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Enable the integration in disposable HA test instances only."""
