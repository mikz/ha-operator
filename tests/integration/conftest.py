"""Native translation cache for direct adapter/runtime fixture calls."""

import pytest
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

from custom_components.ha_operator.const import DOMAIN


@pytest.fixture(autouse=True)
async def _native_exception_translations(hass):
    """Use the same translation loader as HA before asserting rendered exceptions."""
    assert await async_setup_component(hass, DOMAIN, {})
    await async_get_translations(hass, "en", "exceptions", {DOMAIN})
