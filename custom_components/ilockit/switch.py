"""Switches for I LOCK IT settings (BLE).

Each toggle opens one on-demand BLE session (briefly evicting the phone app's
control). The lock gives no read-back for do-not-disturb, automatic open/close
or the sound flags, so those are exposed as assumed-state switches (we track the
last value written). The alarm on/off state is read back from the lock-config.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from homeassistant.components.switch import SwitchEntity
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
    """Set up BLE setting switches for one lock."""
    d: ILockItDevice | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if d is None:
        return
    async_add_entities(
        [
            ILockItSwitch(
                d, entry, "alarm", "mdi:alarm-light",
                lambda x: x.alarm_active,
                lambda x, on: x.async_set_alarm(on),
                assumed=False,
            ),
            ILockItSwitch(
                d, entry, "do_not_disturb", "mdi:bell-sleep",
                lambda x: x.dnd,
                lambda x, on: x.async_set_dnd(on),
                assumed=True,
            ),
            ILockItSwitch(
                d, entry, "auto_open_close", "mdi:gesture-tap-button",
                lambda x: x.auto_open_close,
                lambda x, on: x.async_set_auto(on),
                assumed=True,
            ),
            ILockItSwitch(
                d, entry, "sound_closing", "mdi:volume-high",
                lambda x: x.sound_closing,
                lambda x, on: x.async_set_sound(on, x.sound_opening, x.sound_warning),
                assumed=True,
            ),
            ILockItSwitch(
                d, entry, "sound_opening", "mdi:volume-high",
                lambda x: x.sound_opening,
                lambda x, on: x.async_set_sound(x.sound_closing, on, x.sound_warning),
                assumed=True,
            ),
            ILockItSwitch(
                d, entry, "sound_warning", "mdi:volume-high",
                lambda x: x.sound_warning,
                lambda x, on: x.async_set_sound(x.sound_closing, x.sound_opening, on),
                assumed=True,
            ),
        ]
    )


class ILockItSwitch(SwitchEntity):
    """A single BLE-backed setting toggle."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        device: ILockItDevice,
        entry: ConfigEntry,
        key: str,
        icon: str,
        getter: Callable[[ILockItDevice], bool | None],
        setter: Callable[[ILockItDevice, bool], Awaitable[None]],
        *,
        assumed: bool,
    ) -> None:
        self._device = device
        self._getter = getter
        self._setter = setter
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_assumed_state = assumed
        self._attr_device_info = _lock_device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._device.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def is_on(self) -> bool | None:
        return self._getter(self._device)

    async def async_turn_on(self, **kwargs) -> None:
        await self._setter(self._device, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._setter(self._device, False)
