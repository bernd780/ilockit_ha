"""Constants for the I LOCK IT integration."""

from __future__ import annotations

DOMAIN = "ilockit"

# Config-entry / data keys
CONF_ADDRESS = "address"
CONF_AUTH_ID = "auth_id"
CONF_PHONE_ID = "phone_id"  # hex string of the 8-byte phone id
CONF_LTK = "ltk"  # hex string of the 32-byte long-term key
CONF_SERIAL = "serial"  # hex string of the 4-byte serial
CONF_COUNTER = "counter"  # persisted lock-control replay counter
CONF_LOCK_NAME = "lock_name"

# Cloud (Traccar) keys — used for the location/device_tracker side
CONF_EMAIL = "email"
CONF_PASSWORD = "password"  # noqa: S105 - config key name, not a secret value
CONF_CLOUD_DEVICE_ID = "cloud_device_id"  # Traccar device id for /positions

# Default BLE connection timeout (seconds)
CONNECT_TIMEOUT = 25.0

# How often to poll the cloud for the lock's position (seconds). Positions only
# change when the bike moves or is locked, so a few minutes is plenty and keeps
# load on the haveltec backend low.
POSITION_UPDATE_INTERVAL = 300

# How often to poll the cloud for device attributes (firmware, color code,
# theft-mode). These change very rarely, so poll infrequently.
DEVICE_UPDATE_INTERVAL = 900
