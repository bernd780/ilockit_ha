"""Lock entity for I LOCK IT — BLE open/close control.

The actual transport lives in :class:`ILockItDevice` (coordinator.py): it
connects on demand through whatever connectable Bluetooth proxy/adapter Home
Assistant has, runs the verified authorize handshake and issues the lock/unlock
command. We connect per operation rather than holding the link open, because the
lock sleeps and drops idle connections (and holding the connection would evict
the phone app's control slot for longer than necessary).
"""

from __future__ import annotations

import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ADDRESS, CONF_LOCK_NAME, DOMAIN
from .coordinator import ILockItDevice
from .protocol import LockState

_LOGGER = logging.getLogger(__name__)

# Map the lock's reported state onto Home Assistant's lock attributes.
_LOCKED = {LockState.CLOSED}
_UNLOCKED = {LockState.OPEN}
_LOCKING = {LockState.LOCKING}
_UNLOCKING = {LockState.UNLOCKING}
_JAMMED = {
    LockState.ERROR_LOCKING_BLOCKED,
    LockState.ERROR_LOCKING_MOVED,
    LockState.ERROR_UNLOCKING_BLOCKED,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the BLE lock entity for one lock, if it has BLE credentials."""
    device: ILockItDevice | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if device is None:
        # Cloud-only entry (no authId/phoneId/LTK) → nothing to control.
        _LOGGER.debug("No BLE device for %s; skipping lock entity", entry.title)
        return

    # Recovery / destructive operations are exposed as entity services rather
    # than tappable buttons, so they cannot be triggered by accident.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "enter_pairing_mode", {}, "async_enter_pairing_mode"
    )
    platform.async_register_entity_service("reset", {}, "async_reset_lock")
    platform.async_register_entity_service("unpair", {}, "async_unpair")

    async_add_entities([ILockItLock(device, entry)])


class ILockItLock(LockEntity):
    """A single I LOCK IT bike lock, controlled over Bluetooth."""

    _attr_has_entity_name = True
    _attr_translation_key = "lock"
    # Primary feature of the device → inherit the device name.
    _attr_name = None

    def __init__(self, device: ILockItDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{entry.entry_id}_lock"

        address = entry.data.get(CONF_ADDRESS)
        lock_name = entry.data.get(CONF_LOCK_NAME) or entry.title
        info = DeviceInfo(
            identifiers={(DOMAIN, address or entry.entry_id)},
            name=lock_name,
            manufacturer="haveltec GmbH",
            model="I LOCK IT",
        )
        if address:
            info["connections"] = {(dr.CONNECTION_BLUETOOTH, address)}
        self._attr_device_info = info

    async def async_added_to_hass(self) -> None:
        """Subscribe to state pushes from the BLE device."""
        self.async_on_remove(
            self._device.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Available when a connectable proxy/adapter currently sees the lock."""
        return self._device.available

    @property
    def is_locked(self) -> bool | None:
        state = self._device.lock_state
        if state in _LOCKED:
            return True
        if state in _UNLOCKED:
            return False
        # Unknown until the first command this session — the lock doesn't push
        # its state unless we connect, and we only connect on demand.
        return None

    @property
    def is_locking(self) -> bool:
        return self._device.lock_state in _LOCKING

    @property
    def is_unlocking(self) -> bool:
        return self._device.lock_state in _UNLOCKING

    @property
    def is_jammed(self) -> bool:
        return self._device.lock_state in _JAMMED

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attrs: dict[str, object] = {
            "lock_state": self._device.lock_state.name.lower(),
        }
        if self._device.battery is not None:
            attrs["battery"] = self._device.battery
        if self._device.last_error is not None:
            attrs["last_error"] = self._device.last_error.name.lower()
        return attrs

    async def async_lock(self, **kwargs) -> None:
        """Close (lock) the bike lock."""
        await self._device.async_set_lock(True)

    async def async_unlock(self, **kwargs) -> None:
        """Open (unlock) the bike lock."""
        await self._device.async_set_lock(False)

    # -- entity services ---------------------------------------------------
    async def async_enter_pairing_mode(self) -> None:
        """Put the lock into pairing/coupling mode (for re-pairing in the app)."""
        await self._device.async_enter_pairing_mode()

    async def async_reset_lock(self) -> None:
        """Factory-reset the lock. DESTRUCTIVE — clears the lock's settings."""
        await self._device.async_reset()

    async def async_unpair(self) -> None:
        """Remove this phone's authorization. DESTRUCTIVE — needs re-pairing."""
        await self._device.async_unpair()
