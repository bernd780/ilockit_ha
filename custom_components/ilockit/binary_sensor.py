"""Binary sensors for I LOCK IT (cloud side).

Exposes the Advanced Theft Protection (ATP / "advTheftMode") state reported by
the haveltec cloud. Read-only and cloud-only — never touches BLE, so it does not
take the lock's control slot away from the phone app. The entity is only created
for locks whose cloud record actually carries the attribute.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud import LockDeviceData
from .const import CONF_CLOUD_DEVICE_ID, CONF_EMAIL, CONF_PASSWORD
from .device_coordinator import ILockItDeviceCoordinator, get_device_coordinator
from .sensor import _lock_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up cloud binary sensors for one lock."""
    email = entry.data.get(CONF_EMAIL)
    password = entry.data.get(CONF_PASSWORD)
    device_id = entry.data.get(CONF_CLOUD_DEVICE_ID)
    if not email or not password or device_id is None:
        return

    coordinator, created = get_device_coordinator(hass, email, password)
    if created:
        await coordinator.async_config_entry_first_refresh()
    elif coordinator.data is None:
        # Another platform/entry created the coordinator but its first refresh
        # may still be in flight (concurrent setup); make sure we have data
        # before deciding whether this lock reports theft mode.
        await coordinator.async_refresh()

    did = int(device_id)
    data: LockDeviceData | None = (coordinator.data or {}).get(did)
    if data is None or data.adv_theft_mode is None:
        # This lock's cloud record doesn't report theft mode → no entity.
        _LOGGER.debug("No advTheftMode for %s; skipping theft-mode sensor", entry.title)
        return

    async_add_entities([ILockItTheftModeSensor(coordinator, entry, did)])


class ILockItTheftModeSensor(
    CoordinatorEntity[ILockItDeviceCoordinator], BinarySensorEntity
):
    """Advanced Theft Protection armed state (from the cloud)."""

    _attr_has_entity_name = True
    _attr_translation_key = "theft_mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ILockItDeviceCoordinator,
        entry: ConfigEntry,
        device_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_theft_mode"
        self._attr_device_info = _lock_device_info(entry)

    @property
    def _data(self) -> LockDeviceData | None:
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def available(self) -> bool:
        data = self._data
        return super().available and data is not None and data.adv_theft_mode is not None

    @property
    def is_on(self) -> bool | None:
        data = self._data
        return data.adv_theft_mode if data else None

    @property
    def icon(self) -> str:
        return "mdi:shield-lock" if self.is_on else "mdi:shield-off-outline"
