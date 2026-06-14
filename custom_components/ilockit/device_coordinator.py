"""Cloud device-attribute coordinator for I LOCK IT.

Polls the haveltec Traccar cloud (`GET /api/devices`) for per-lock attributes
that change rarely — firmware version, printed color code and Advanced Theft
Protection state. This is a read-only HTTP poll, completely independent of the
BLE control path, so it never takes the lock's control slot away from the phone
app. One coordinator is shared per account.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud import CloudError, ILockItCloud, LockDeviceData
from .const import DEVICE_UPDATE_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ILockItDeviceCoordinator(DataUpdateCoordinator[dict[int, LockDeviceData]]):
    """Polls the cloud for device attributes of every lock on the account."""

    def __init__(self, hass: HomeAssistant, cloud: ILockItCloud) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="I LOCK IT devices",
            update_interval=timedelta(seconds=DEVICE_UPDATE_INTERVAL),
        )
        self._cloud = cloud

    async def _async_update_data(self) -> dict[int, LockDeviceData]:
        try:
            return await self._cloud.async_get_devices()
        except CloudError as err:
            raise UpdateFailed(f"cloud device fetch failed: {err}") from err


def get_device_coordinator(
    hass: HomeAssistant, email: str, password: str
) -> tuple[ILockItDeviceCoordinator, bool]:
    """Get (or lazily create) the shared device coordinator for an account.

    Returns (coordinator, created) so the caller knows whether to do the
    one-time first refresh.
    """
    store: dict[str, ILockItDeviceCoordinator] = hass.data.setdefault(
        DOMAIN, {}
    ).setdefault("_device_coordinators", {})
    coordinator = store.get(email)
    if coordinator is not None:
        return coordinator, False
    cloud = ILockItCloud(hass, email, password)
    coordinator = ILockItDeviceCoordinator(hass, cloud)
    store[email] = coordinator
    return coordinator, True
