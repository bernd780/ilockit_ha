"""The I LOCK IT smart bike lock integration.

Two independent sides per config entry:
  * BLE control (lock/unlock) via :class:`ILockItDevice`, requires the per-lock
    credentials (authId / phoneId / LTK) obtained from the haveltec cloud.
  * Cloud location (device_tracker + source sensor) via the Traccar REST API,
    requires the account email/password. Read-only, never touches BLE.

Either side may be absent for an entry; the platforms skip themselves when the
data they need is not present.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS,
    CONF_AUTH_ID,
    CONF_CLOUD_DEVICE_ID,
    CONF_EMAIL,
    CONF_LOCK_NAME,
    CONF_LTK,
    CONF_PASSWORD,
    CONF_PHONE_ID,
    CONF_SERIAL,
    DOMAIN,
)
from .protocol import Credentials

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.SELECT,
]


def _build_credentials(data: dict) -> Credentials | None:
    """Build BLE Credentials from entry data, or None if the lock is cloud-only."""
    ltk_hex = data.get(CONF_LTK)
    phone_hex = data.get(CONF_PHONE_ID)
    auth_id = data.get(CONF_AUTH_ID)
    if not ltk_hex or not phone_hex or auth_id is None:
        return None
    serial_hex = data.get(CONF_SERIAL)
    try:
        return Credentials(
            auth_id=int(auth_id),
            phone_id=bytes.fromhex(phone_hex),
            ltk=bytes.fromhex(ltk_hex),
            serial=bytes.fromhex(serial_hex) if serial_hex else None,
        )
    except (ValueError, TypeError) as err:
        _LOGGER.error("Invalid BLE credentials in config entry: %s", err)
        return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an I LOCK IT lock from a config entry."""
    domain_data: dict = hass.data.setdefault(DOMAIN, {})

    creds = _build_credentials(entry.data)
    address = entry.data.get(CONF_ADDRESS)
    if creds is not None and address:
        # Imported lazily: the BLE stack (bleak / bleak-retry-connector) is a
        # heavy import and only needed for lock control, never for the config
        # flow or the cloud-only platforms.
        from .coordinator import ILockItDevice

        device = ILockItDevice(
            hass,
            address,
            entry.data.get(CONF_LOCK_NAME) or entry.title,
            creds,
        )
        domain_data[entry.entry_id] = device

    # Prepare the shared cloud device-attribute coordinator (firmware / color /
    # theft mode) BEFORE forwarding platforms, so the binary_sensor platform can
    # reliably decide whether this lock reports theft mode (avoids a setup race
    # between the sensor and binary_sensor platforms over the first refresh).
    email = entry.data.get(CONF_EMAIL)
    password = entry.data.get(CONF_PASSWORD)
    if email and password and entry.data.get(CONF_CLOUD_DEVICE_ID) is not None:
        from .device_coordinator import get_device_coordinator

        dev_coordinator, created = get_device_coordinator(hass, email, password)
        if created:
            await dev_coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        device: ILockItDevice | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if device is not None:
            await device.async_shutdown()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options/data change."""
    await hass.config_entries.async_reload(entry.entry_id)
