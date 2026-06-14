"""Buttons for I LOCK IT — momentary BLE actions.

Each press opens one on-demand BLE session (which briefly takes the phone app's
control slot) and issues a single command.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ILockItDevice
from .sensor import _lock_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BLE action buttons for one lock."""
    device: ILockItDevice | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if device is None:
        return
    async_add_entities(
        [
            ILockItButton(device, entry, "refresh", "mdi:refresh", device.async_refresh),
            ILockItButton(device, entry, "beep", "mdi:bullhorn", device.async_beep),
            ILockItButton(
                device, entry, "stop_alarm", "mdi:alarm-light-off", device.async_stop_alarm
            ),
        ]
    )


class ILockItButton(ButtonEntity):
    """A single BLE action button."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        device: ILockItDevice,
        entry: ConfigEntry,
        key: str,
        icon: str,
        action: Callable[[], Awaitable[None]],
    ) -> None:
        self._device = device
        self._action = action
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = _lock_device_info(entry)

    @property
    def available(self) -> bool:
        return self._device.available

    async def async_press(self) -> None:
        await self._action()
