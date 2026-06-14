"""Sensors for I LOCK IT locks (cloud side).

Currently exposes the *source* of the lock's reported position as its own
entity, so it is immediately obvious in HA whether the shown location came from
the phone app (recorded at lock time) or from the lock's own integrated
GPS/SIM. History on this sensor also shows when it flips between the two.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud import LockDeviceData, LockPosition
from .const import (
    CONF_ADDRESS,
    CONF_CLOUD_DEVICE_ID,
    CONF_EMAIL,
    CONF_LOCK_NAME,
    CONF_PASSWORD,
    DOMAIN,
)
from .device_coordinator import ILockItDeviceCoordinator, get_device_coordinator
from .device_tracker import ILockItPositionCoordinator, _get_position_coordinator
from .protocol import color_names

# Human-readable state values are provided via translations/*.json.
_SOURCE_OPTIONS = ["lock_gps", "phone", "unknown"]
_SOURCE_ICONS = {
    "lock_gps": "mdi:satellite-variant",
    "phone": "mdi:cellphone-marker",
    "unknown": "mdi:map-marker-question",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the lock's sensors (BLE battery + cloud diagnostics)."""
    entities: list[SensorEntity] = []

    # BLE battery sensor (updated for free on each lock/unlock or via refresh).
    device = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if device is not None:
        entities.append(ILockItBatterySensor(device, entry))

    # Cloud-side diagnostic sensors.
    email = entry.data.get(CONF_EMAIL)
    password = entry.data.get(CONF_PASSWORD)
    device_id = entry.data.get(CONF_CLOUD_DEVICE_ID)
    if email and password and device_id is not None:
        coordinator, created = _get_position_coordinator(hass, email, password)
        if created:
            await coordinator.async_config_entry_first_refresh()

        dev_coordinator, dev_created = get_device_coordinator(hass, email, password)
        if dev_created:
            await dev_coordinator.async_config_entry_first_refresh()

        did = int(device_id)
        entities += [
            ILockItLocationSourceSensor(coordinator, entry, did),
            ILockItFirmwareSensor(dev_coordinator, entry, did),
            ILockItColorCodeSensor(dev_coordinator, entry, did),
        ]

    if entities:
        async_add_entities(entities)


def _lock_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build the shared device registry entry for a lock."""
    address = entry.data.get(CONF_ADDRESS)
    lock_name = entry.data.get(CONF_LOCK_NAME) or entry.title
    return DeviceInfo(
        identifiers={(DOMAIN, address or entry.entry_id)},
        name=lock_name,
        manufacturer="haveltec GmbH",
        model="I LOCK IT",
    )


class ILockItBatterySensor(SensorEntity):
    """Battery level read over BLE (on each lock/unlock or manual refresh)."""

    _attr_has_entity_name = True
    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{entry.entry_id}_battery"
        self._attr_device_info = _lock_device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._device.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        # Keep showing the last reading; only None until the first BLE session.
        return self._device.battery is not None

    @property
    def native_value(self) -> int | None:
        return self._device.battery


class ILockItLocationSourceSensor(
    CoordinatorEntity[ILockItPositionCoordinator], SensorEntity
):
    """Shows where the current position came from: the lock's GPS or the phone."""

    _attr_has_entity_name = True
    _attr_translation_key = "location_source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _SOURCE_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ILockItPositionCoordinator,
        entry: ConfigEntry,
        device_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_location_source"

        address = entry.data.get(CONF_ADDRESS)
        lock_name = entry.data.get(CONF_LOCK_NAME) or entry.title
        info = DeviceInfo(
            identifiers={(DOMAIN, address or entry.entry_id)},
            name=lock_name,
            manufacturer="haveltec GmbH",
            model="I LOCK IT",
        )
        self._attr_device_info = info

    @property
    def _position(self) -> LockPosition | None:
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def available(self) -> bool:
        return super().available and self._position is not None

    @property
    def native_value(self) -> str | None:
        pos = self._position
        return pos.source if pos else None

    @property
    def icon(self) -> str:
        pos = self._position
        return _SOURCE_ICONS.get(pos.source if pos else "unknown", _SOURCE_ICONS["unknown"])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        pos = self._position
        if pos is None:
            return {}
        return {
            "protocol": pos.protocol,
            "fix_time": pos.fix_time.isoformat() if pos.fix_time else None,
            "satellites": pos.satellites,
        }


class _DeviceDataEntity(CoordinatorEntity[ILockItDeviceCoordinator], SensorEntity):
    """Base for cloud device-attribute sensors (firmware, color code)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ILockItDeviceCoordinator,
        entry: ConfigEntry,
        device_id: int,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = _lock_device_info(entry)

    @property
    def _data(self) -> LockDeviceData | None:
        return (self.coordinator.data or {}).get(self._device_id)


class ILockItFirmwareSensor(_DeviceDataEntity):
    """Firmware version reported by the cloud."""

    _attr_icon = "mdi:chip"

    def __init__(self, coordinator, entry, device_id) -> None:
        super().__init__(coordinator, entry, device_id, "firmware")

    @property
    def available(self) -> bool:
        data = self._data
        return super().available and data is not None and data.firmware is not None

    @property
    def native_value(self) -> str | None:
        data = self._data
        return data.firmware if data else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self._data
        if data is None or data.settings_timestamp is None:
            return {}
        return {"settings_timestamp": data.settings_timestamp}


class ILockItColorCodeSensor(_DeviceDataEntity):
    """The lock's printed 6-symbol color code, with decoded color names."""

    _attr_icon = "mdi:palette"

    def __init__(self, coordinator, entry, device_id) -> None:
        super().__init__(coordinator, entry, device_id, "color_code")

    @property
    def available(self) -> bool:
        data = self._data
        return super().available and data is not None and data.color_code is not None

    @property
    def native_value(self) -> str | None:
        data = self._data
        return data.color_code if data else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self._data
        if data is None or data.color_code is None:
            return {}
        names = color_names(data.color_code)
        return {"colors": names} if names else {}
