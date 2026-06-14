"""Fetch I LOCK IT lock credentials from the haveltec Traccar cloud.

Login + GET /api/devices, then extract per-user (authId, hwId/phoneId, ltk) and
the device name/mac. The lock's session key (LTK) is the raw base64-decoded
`ltk` attribute for the server-synchronized (GPS) variant — verified against
hardware. We expose both the raw and the name-decrypted candidate so the
classic-NoBond variant can fall back if needed.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .protocol import decrypt_long_term

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://tracking.ilockit.bike/api"

# Traccar `protocol` field -> where the fix actually came from.
# The GPS locks report their own position over the integrated SIM as one of the
# tracker protocols; "app" is the phone-uploaded fallback recorded at lock time.
_LOCK_GPS_PROTOCOLS = {"topin", "topinzx905"}


class CloudError(Exception):
    """Raised when the cloud login or device fetch fails."""


@dataclass
class CloudLock:
    """Credentials for one lock as stored in the haveltec cloud."""

    name: str
    device_id: int
    mac: str | None
    auth_id: int
    phone_id: str  # hex
    ltk_raw: str  # hex — base64-decode of the cloud `ltk` (GPS variant key)
    ltk_decrypted: str | None  # hex — name-decrypted candidate (classic variant)
    color_code: str | None = None
    firmware: str | None = None


async def fetch_locks(email: str, password: str) -> list[CloudLock]:
    """Log in and return the credentials for every lock on the account."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{BASE_URL}/session",
            data={"email": email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                raise CloudError(f"login failed: HTTP {resp.status}")
            user = await resp.json()
        user_id = str(user.get("id"))

        async with session.get(f"{BASE_URL}/devices") as resp:
            if resp.status != 200:
                raise CloudError(f"devices fetch failed: HTTP {resp.status}")
            devices = await resp.json()

    locks: list[CloudLock] = []
    for d in devices:
        attrs = d.get("attributes", {}) or {}
        name = d.get("name", "")
        ua = attrs.get(user_id, {}) or {}
        ltk_b64 = ua.get("ltk") or attrs.get("ltk")
        auth_id = ua.get("authId", attrs.get("authId"))
        hw_id = ua.get("hwId", attrs.get("hwId"))
        if not (ltk_b64 and auth_id is not None and hw_id and name):
            continue
        try:
            ltk_raw = base64.b64decode(ltk_b64).hex()
        except Exception:  # noqa: BLE001
            continue
        try:
            dec = decrypt_long_term(ltk_b64, name)
            ltk_dec = dec.hex()
        except Exception:  # noqa: BLE001
            ltk_dec = None
        locks.append(
            CloudLock(
                name=name,
                device_id=int(d.get("id", 0)),
                mac=(ua.get("mac") or attrs.get("mac")),
                auth_id=int(auth_id),
                phone_id=hw_id,
                ltk_raw=ltk_raw,
                ltk_decrypted=ltk_dec,
                color_code=attrs.get("colorCode"),
                firmware=str(attrs.get("firmwareVersion")) if attrs.get("firmwareVersion") else None,
            )
        )
    return locks


@dataclass
class LockPosition:
    """A single position fix for a lock, as served by the Traccar cloud."""

    device_id: int
    latitude: float
    longitude: float
    fix_time: datetime | None
    protocol: str | None
    satellites: int | None

    @property
    def source(self) -> str:
        """Where this fix came from: the lock's own GPS/SIM, or the phone."""
        if self.protocol is None:
            return "unknown"
        if self.protocol.lower() in _LOCK_GPS_PROTOCOLS:
            return "lock_gps"
        if self.protocol.lower() == "app":
            return "phone"
        return "unknown"


@dataclass
class LockDeviceData:
    """Cloud-side attributes for a lock (from GET /api/devices).

    All read-only and HTTP-only — never touches BLE, so polling it does not take
    the lock's control slot away from the phone app.
    """

    device_id: int
    firmware: str | None
    color_code: str | None  # printed 6-symbol code, e.g. "221032"
    adv_theft_mode: bool | None  # Advanced Theft Protection armed (None if absent)
    settings_timestamp: str | None


def _parse_device(raw: dict) -> LockDeviceData | None:
    """Turn one Traccar device record into a LockDeviceData (or None)."""
    device_id = raw.get("id")
    if device_id is None:
        return None
    attrs = raw.get("attributes") or {}
    fw = attrs.get("firmwareVersion")
    return LockDeviceData(
        device_id=int(device_id),
        firmware=str(fw) if fw is not None else None,
        color_code=attrs.get("colorCode"),
        adv_theft_mode=attrs.get("advTheftMode"),
        settings_timestamp=attrs.get("settingsTimestamp"),
    )


def _parse_time(raw: dict) -> datetime | None:
    """Best-effort parse of the fix/server time as an aware UTC datetime."""
    value = raw.get("fixTime") or raw.get("deviceTime") or raw.get("serverTime")
    if not value:
        return None
    try:
        # Traccar emits e.g. "2026-06-13T19:35:12.728+0000" or "...Z".
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_position(raw: dict) -> LockPosition | None:
    """Turn one Traccar position record into a LockPosition (or None)."""
    lat = raw.get("latitude")
    lon = raw.get("longitude")
    device_id = raw.get("deviceId")
    if lat is None or lon is None or device_id is None:
        return None
    attrs = raw.get("attributes") or {}
    return LockPosition(
        device_id=int(device_id),
        latitude=float(lat),
        longitude=float(lon),
        fix_time=_parse_time(raw),
        protocol=raw.get("protocol"),
        satellites=attrs.get("sat"),
    )


class ILockItCloud:
    """Authenticated session against the haveltec Traccar cloud for positions.

    READ-ONLY and HTTP-only — this never touches BLE, so polling it does not
    take the lock's control slot away from the phone app.
    """

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        self._session = async_get_clientsession(hass)
        self._email = email
        self._password = password
        self._logged_in = False

    async def _login(self) -> None:
        async with self._session.post(
            f"{BASE_URL}/session",
            data={"email": self._email, "password": self._password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                self._logged_in = False
                raise CloudError(f"login failed: HTTP {resp.status}")
        self._logged_in = True

    async def async_get_positions(self) -> dict[int, LockPosition]:
        """Return the latest known position per device, keyed by device id.

        Re-authenticates once transparently if the JSESSIONID has expired.
        """
        for attempt in range(2):
            if not self._logged_in:
                await self._login()
            async with self._session.get(
                f"{BASE_URL}/positions", headers={"Accept": "application/json"}
            ) as resp:
                if resp.status in (401, 403):
                    # Session expired — drop it and retry the login once.
                    self._logged_in = False
                    if attempt == 0:
                        continue
                    raise CloudError("not authorized for positions")
                if resp.status != 200:
                    raise CloudError(f"positions fetch failed: HTTP {resp.status}")
                records = await resp.json()
            break

        positions: dict[int, LockPosition] = {}
        for raw in records:
            pos = _parse_position(raw)
            if pos is None:
                continue
            # /positions returns the latest per device, but keep the newest if
            # the server ever returns more than one for a device.
            prev = positions.get(pos.device_id)
            if prev is None or (
                pos.fix_time is not None
                and prev.fix_time is not None
                and pos.fix_time >= prev.fix_time
            ):
                positions[pos.device_id] = pos
        return positions

    async def async_get_devices(self) -> dict[int, LockDeviceData]:
        """Return cloud attributes per device, keyed by device id.

        Re-authenticates once transparently if the JSESSIONID has expired.
        """
        records: list[dict] = []
        for attempt in range(2):
            if not self._logged_in:
                await self._login()
            async with self._session.get(
                f"{BASE_URL}/devices", headers={"Accept": "application/json"}
            ) as resp:
                if resp.status in (401, 403):
                    self._logged_in = False
                    if attempt == 0:
                        continue
                    raise CloudError("not authorized for devices")
                if resp.status != 200:
                    raise CloudError(f"devices fetch failed: HTTP {resp.status}")
                records = await resp.json()
            break

        devices: dict[int, LockDeviceData] = {}
        for raw in records:
            dev = _parse_device(raw)
            if dev is not None:
                devices[dev.device_id] = dev
        return devices
