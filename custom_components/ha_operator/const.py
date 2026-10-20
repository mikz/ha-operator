"""Shared integration constants."""

DOMAIN = "ha_operator"
NAME = "HA Operator"
VERSION = "0.5.0"
PLATFORMS = (
    "light",
    "cover",
    "fan",
    "switch",
    "sensor",
    "binary_sensor",
    "select",
    "button",
    "number",
)
DEFAULT_MANUAL_DURATION = 1800.0
DEFAULT_RETRY_INTERVAL = 300.0
DEFAULT_COMMAND_INTERVAL = 30.0
DEFAULT_MOVEMENT_TIMEOUT = 120.0
DEFAULT_TOLERANCE = 2.0
