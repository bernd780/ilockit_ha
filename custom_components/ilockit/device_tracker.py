"""Device tracker for I LOCK IT locks, fed by the haveltec Traccar cloud.

The lock's position is served by the cloud (`GET /api/positions`), reported
either by the lock's own integrated GPS/SIM (`protocol` topin/topinzx905) or as
a phone-uploaded fallback recorded at lock time (`protocol` app). This is a
read-only HTTP poll and is completely independent of the BLE control path, so
it never takes the lock's control slot away from the phone app.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .cloud import CloudError, ILockItCloud, LockPosition
from .const import (
    CONF_ADDRESS,
    CONF_CLOUD_DEVICE_ID,
    CONF_EMAIL,
    CONF_LOCK_NAME,
    CONF_PASSWORD,
    DOMAIN,
    POSITION_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class ILockItPositionCoordinator(DataUpdateCoordinator[dict[int, LockPosition]]):
    """Polls the cloud for the latest position of every lock on the account.

    One coordinator is shared by all lock entries that use the same account, so
    we hit the backend once per cycle regardless of how many locks exist.
    """

    def __init__(self, hass: HomeAssistant, cloud: ILockItCloud) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="I LOCK IT positions",
            update_interval=timedelta(seconds=POSITION_UPDATE_INTERVAL),
        )
        self._cloud = cloud

    async def _async_update_data(self) -> dict[int, LockPosition]:
        try:
            return await self._cloud.async_get_positions()
        except CloudError as err:
            raise UpdateFailed(f"cloud position fetch failed: {err}") from err


def _get_position_coordinator(
    hass: HomeAssistant, email: str, password: str
) -> tuple[ILockItPositionCoordinator, bool]:
    """Get (or lazily create) the shared coordinator for an account.

    Returns (coordinator, created) so the caller knows whether to do the
    one-time first refresh.
    """
    store: dict[str, ILockItPositionCoordinator] = hass.data.setdefault(
        DOMAIN, {}
    ).setdefault("_position_coordinators", {})
    coordinator = store.get(email)
    if coordinator is not None:
        return coordinator, False
    cloud = ILockItCloud(hass, email, password)
    coordinator = ILockItPositionCoordinator(hass, cloud)
    store[email] = coordinator
    return coordinator, True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cloud-backed device tracker for one lock."""
    email = entry.data.get(CONF_EMAIL)
    password = entry.data.get(CONF_PASSWORD)
    device_id = entry.data.get(CONF_CLOUD_DEVICE_ID)
    if not email or not password or device_id is None:
        # No cloud credentials/device id stored for this entry → BLE-only setup,
        # nothing to track.
        _LOGGER.debug("No cloud config for %s; skipping device tracker", entry.title)
        return

    coordinator, created = _get_position_coordinator(hass, email, password)
    if created:
        await coordinator.async_config_entry_first_refresh()

    async_add_entities([ILockItTracker(coordinator, entry, int(device_id))])


class ILockItTracker(CoordinatorEntity[ILockItPositionCoordinator], TrackerEntity):
    """Reports the cloud-known location of a single lock."""

    _attr_has_entity_name = True
    _attr_translation_key = "location"
    _attr_name = "Location"

    def __init__(
        self,
        coordinator: ILockItPositionCoordinator,
        entry: ConfigEntry,
        device_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_location"

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

    @property
    def _position(self) -> LockPosition | None:
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def icon(self) -> str:
        pos = self._position
        source = pos.source if pos else "unknown"
        if source == "lock_gps":
            return "mdi:satellite-variant"
        if source == "phone":
            return "mdi:cellphone-marker"
        return "mdi:map-marker-question"

    @property
    def latitude(self) -> float | None:
        pos = self._position
        return pos.latitude if pos else None

    @property
    def longitude(self) -> float | None:
        pos = self._position
        return pos.longitude if pos else None

    @property
    def available(self) -> bool:
        return super().available and self._position is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        pos = self._position
        if pos is None:
            return {}
        return {
            "fix_time": pos.fix_time.isoformat() if pos.fix_time else None,
            "protocol": pos.protocol,
            "satellites": pos.satellites,
            "source": pos.source,
        }
