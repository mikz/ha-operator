"""Real platform setup helpers; no mocked operator runtime or persistence."""

from __future__ import annotations

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_operator.const import DOMAIN


class PhysicalCover(CoverEntity):
    """Test device with independent observed position and a command journal."""

    _attr_name = "Physical roof"
    _attr_unique_id = "physical_roof"
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.STOP
    )
    _attr_current_cover_position = 0

    def __init__(self):
        self.entity_id = "cover.physical_roof"
        self.commands = []
        self.refuse = False

    @property
    def is_closed(self):
        return self.current_cover_position == 0

    async def async_set_cover_position(self, **kwargs):
        self.commands.append(("position", kwargs["position"]))
        if not self.refuse:
            self._attr_current_cover_position = kwargs["position"]
            self.async_write_ha_state()

    async def async_open_cover(self, **kwargs):
        await self.async_set_cover_position(position=100)

    async def async_close_cover(self, **kwargs):
        await self.async_set_cover_position(position=0)

    async def async_stop_cover(self, **kwargs):
        self.commands.append(("stop", None))


async def async_add_physical_cover(hass):
    assert await async_setup_component(hass, "cover", {})
    raw = PhysicalCover()
    await hass.data["cover"].async_add_entities([raw])
    return raw


def operator_entry(
    *, resources=None, policies=None, requirements=None, intents=None, data=None, version=3
):
    """Build genuine HA subentries with stable identifiers."""
    subentries = []
    if resources is None:
        resources = {
            "roof": {
                "name": "Roof",
                "kind": "cover",
                "entity_id": "cover.physical_roof",
            }
        }
    for kind, configs in (
        ("resource", resources),
        ("policy", policies or {}),
        ("requirement", requirements or {}),
        ("intent", intents or {}),
    ):
        for key, config in configs.items():
            subentries.append(
                {
                    "subentry_id": key,
                    "subentry_type": kind,
                    "title": config["name"],
                    "data": config,
                    "unique_id": None,
                }
            )
    return MockConfigEntry(
        domain=DOMAIN,
        title="HA Operator",
        data=data or {},
        version=version,
        subentries_data=subentries,
    )


async def async_setup_operator(hass, tmp_path, **kwargs):
    """Load all production platforms and native Store persistence in a temporary directory."""
    hass.config.config_dir = str(tmp_path)
    entry = operator_entry(**kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def managed_id(hass, platform, resource_id="roof", key="managed"):
    identifier = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{resource_id}_{key}")
    assert identifier is not None
    return identifier


async def async_activate(hass, resource_id="roof"):
    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": managed_id(hass, "select", resource_id, "mode"),
            "option": "live",
        },
        blocking=True,
    )
    await hass.async_block_till_done()
