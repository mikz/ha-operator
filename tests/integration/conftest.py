"""Native translation cache for direct adapter/runtime fixture calls."""

import pytest
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

from custom_components.ha_operator.const import DOMAIN

_NATIVE_LOAD = Store._async_load
_NATIVE_WRITE = Store._async_write_data
_NATIVE_REMOVE = Store.async_remove


@pytest.fixture(autouse=True)
def _native_operator_storage(hass_storage):
    """Exercise real Store I/O for Operator while other HA stores stay isolated."""
    with pytest.MonkeyPatch.context() as patcher:
        for method, native in (
            ("_async_load", _NATIVE_LOAD),
            ("_async_write_data", _NATIVE_WRITE),
            ("async_remove", _NATIVE_REMOVE),
        ):
            mocked = getattr(Store, method)

            def route(native=native, mocked=mocked):
                async def dispatch(store, *args, **kwargs):
                    handler = native if store.key.startswith(f"{DOMAIN}.") else mocked
                    return await handler(store, *args, **kwargs)

                return dispatch

            patcher.setattr(Store, method, route())
        yield


@pytest.fixture(autouse=True)
async def _native_exception_translations(hass):
    """Use the same translation loader as HA before asserting rendered exceptions."""
    assert await async_setup_component(hass, DOMAIN, {})
    await async_get_translations(hass, "en", "exceptions", {DOMAIN})
