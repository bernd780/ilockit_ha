"""Select entities for I LOCK IT settings (BLE).

The alarm sensitivity (UI level 1-4 on non-Pro, 1-6 on Pro). There is no
read-back, so this is assumed-state: it reflects the last value set and is
applied to the lock the next time the alarm is (re)armed.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ILockItDevice
from .sensor import _lock_device_info

_LOGGER = logging.getLogger(__name__)

_SENSITIVITY_OPTIONS = ["1", "2", "3", "4"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BLE setting selects for one lock."""
    device: ILockItDevice | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if device is None:
        return
    async_add_entities([ILockItSensitivitySelect(device, entry)])


class ILockItSensitivitySelect(SelectEntity):
    """Alarm sensitivity level (assumed-state)."""

    _attr_has_entity_name = True
    _attr_translation_key = "alarm_sensitivity"
    _attr_icon = "mdi:car-brake-alert"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = _SENSITIVITY_OPTIONS
    _attr_assumed_state = True

    def __init__(self, device: ILockItDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{entry.entry_id}_alarm_sensitivity"
        self._attr_device_info = _lock_device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._device.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def current_option(self) -> str | None:
        return str(self._device.alarm_sensitivity)

    async def async_select_option(self, option: str) -> None:
        await self._device.async_set_sensitivity(int(option))
