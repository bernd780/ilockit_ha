"""Connection manager for one I LOCK IT lock over Home Assistant Bluetooth.

Connects on demand through whatever connectable proxy/adapter HA has, runs the
verified authorize handshake (protocol.ILockItSession) and issues lock/unlock and
the other USDIO commands. The lock sleeps and drops idle links, so we connect per
operation rather than holding a persistent connection.

IMPORTANT: every BLE session authorizes with the owner slot (authId), which
evicts the phone app's active control. All BLE actions here are therefore
strictly user-initiated (button/switch/lock presses); there is no background
polling. Battery and lock-state are read for free during the lock/unlock session
we already hold, and otherwise only via the explicit "refresh" action.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import CONNECT_TIMEOUT
from .protocol import (
    GDIO_UUID,
    USDIO_UUID,
    Credentials,
    ILockItSession,
    LockState,
    WriteRequest,
)

_LOGGER = logging.getLogger(__name__)

# States that signal a finished/started transition we can stop waiting on.
_OPENING = {LockState.UNLOCKING, LockState.OPEN}
_CLOSING = {LockState.LOCKING, LockState.CLOSED}


class ILockItDevice:
    """Owns the BLE connection + handshake for a single lock."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        creds: Credentials,
    ) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self._creds = creds
        self._session = ILockItSession(
            creds,
            on_state=self._on_state,
            on_authorized=self._on_authorized,
            on_battery=self._on_battery,
            on_error=self._on_error,
            on_lock_config=self._on_lock_config,
        )
        self._client: BleakClientWithServiceCache | None = None
        self._op_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._authorized = asyncio.Event()
        self._state_changed = asyncio.Event()
        self._listeners: list[CALLBACK_TYPE] = []

        # Public state read from the lock.
        self.lock_state: LockState = LockState.UNKNOWN
        self.battery: int | None = None
        self.alarm_active: bool | None = None
        self.last_error: LockState | None = None

        # Assumed settings state (the lock gives no read-back for these, so we
        # track the last value we wrote and expose them as assumed-state).
        self.alarm_sensitivity: int = 1
        self.dnd: bool = False
        self.auto_open_close: bool = False
        self.sound_closing: bool = False
        self.sound_opening: bool = False
        self.sound_warning: bool = False

    # -- listener plumbing -------------------------------------------------
    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update)

        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _notify_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    # -- session callbacks (run in loop thread) ----------------------------
    @callback
    def _on_state(self, state: LockState) -> None:
        _LOGGER.debug("%s: state -> %s", self.name, state.name)
        self.lock_state = state
        self._state_changed.set()
        self._notify_listeners()

    @callback
    def _on_authorized(self) -> None:
        _LOGGER.debug("%s: authorized", self.name)
        self._authorized.set()

    @callback
    def _on_battery(self, level: int) -> None:
        self.battery = level & 0xFF
        _LOGGER.debug("%s: battery %d%%", self.name, self.battery)
        self._notify_listeners()

    @callback
    def _on_lock_config(self, config: dict) -> None:
        self.alarm_active = config.get("alarm_active")
        # lock_state is delivered separately via _on_state inside the session.
        self._notify_listeners()

    @callback
    def _on_error(self, state: LockState) -> None:
        _LOGGER.warning("%s: lock error %s", self.name, state.name)
        self.last_error = state
        self._authorized.set()  # unblock waiters; caller checks self._authorized state

    # -- notification handling --------------------------------------------
    def _handle_notify(self, char_uuid: str):
        @callback
        def _cb(_char, data: bytearray) -> None:
            payload = bytes(data)
            if char_uuid == GDIO_UUID:
                writes = self._session.on_gdio(payload)
            else:
                writes = self._session.on_usdio(payload)
            if writes:
                self.hass.async_create_task(self._dispatch(writes))

        return _cb

    async def _dispatch(self, writes) -> None:
        async with self._write_lock:
            client = self._client
            if client is None:
                return
            for char_uuid, payload in writes:
                # MTU 23 → split into <=20-byte ATT writes; the lock reassembles.
                for off in range(0, len(payload), 20):
                    await client.write_gatt_char(
                        char_uuid, payload[off : off + 20], response=True
                    )

    # -- connection / auth -------------------------------------------------
    async def _ensure_authorized(self) -> None:
        if self._client is not None and self._client.is_connected and self._session.authorized:
            return
        await self._connect()

    async def _connect(self) -> None:
        ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise ConnectionError(
                f"{self.name}: not in range of a connectable Bluetooth proxy/adapter"
            )
        self._authorized.clear()
        self.last_error = None
        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.name,
            timeout=CONNECT_TIMEOUT,
        )
        self._client = client
        # bleak enables indications automatically for INDICATE characteristics.
        await client.start_notify(GDIO_UUID, self._handle_notify(GDIO_UUID))
        await client.start_notify(USDIO_UUID, self._handle_notify(USDIO_UUID))
        await self._dispatch(self._session.start())
        try:
            await asyncio.wait_for(self._authorized.wait(), timeout=15)
        except TimeoutError as err:
            await self._disconnect()
            raise ConnectionError(f"{self.name}: authorization timed out") from err
        if not self._session.authorized:
            await self._disconnect()
            raise ConnectionError(
                f"{self.name}: authorization failed ({self.last_error})"
            )

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _read_status_in_session(self) -> None:
        """Within an authorized session: read battery and lock-config.

        Updates self.battery / self.lock_state / self.alarm_active via callbacks.
        Best-effort: a dropped terminal indication on a weak link is not an error.
        """
        await self._dispatch([self._session.request_battery()])
        await asyncio.sleep(0.6)
        await self._dispatch([self._session.request_lock_config()])
        await asyncio.sleep(1.5)

    # -- generic command runners ------------------------------------------
    async def _command(
        self, build: Callable[[ILockItSession], WriteRequest], *, settle: float = 2.0
    ) -> None:
        """Connect, authorize, send one USDIO command, settle, disconnect."""
        async with self._op_lock:
            try:
                await self._ensure_authorized()
                await self._dispatch([build(self._session)])
                await asyncio.sleep(settle)
                self._notify_listeners()
            finally:
                await asyncio.sleep(0.3)
                await self._disconnect()

    async def _command_expect_disconnect(
        self, build: Callable[[ILockItSession], WriteRequest], *, settle: float = 2.0
    ) -> None:
        """Like _command, but the lock is expected to drop the link as the ACK.

        Used for enter-pairing / reset / unpair: the lock terminates the BLE
        connection (HCI 0x13) right after accepting the command — that disconnect
        IS the success signal, not an error.
        """
        async with self._op_lock:
            try:
                await self._ensure_authorized()
                try:
                    await self._dispatch([build(self._session)])
                    await asyncio.sleep(settle)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("%s: expected disconnect: %s", self.name, err)
            finally:
                await self._disconnect()

    # -- public API: lock control -----------------------------------------
    async def async_set_lock(self, do_lock: bool) -> None:
        """Lock (close) or unlock (open) the bike lock."""
        async with self._op_lock:
            try:
                await self._ensure_authorized()
                self._state_changed.clear()
                want = _CLOSING if do_lock else _OPENING
                req = self._session.lock() if do_lock else self._session.unlock()
                await self._dispatch([req])
                # Wait for the lock to report a matching transition.
                for _ in range(15):
                    try:
                        await asyncio.wait_for(self._state_changed.wait(), timeout=1)
                    except TimeoutError:
                        continue
                    self._state_changed.clear()
                    if self.lock_state in want:
                        break
                # Piggyback a battery + lock-config read on this session (free).
                try:
                    await self._read_status_in_session()
                except Exception:  # noqa: BLE001
                    pass
                self._notify_listeners()
            finally:
                # Give the motor a moment, then release the link (battery saver).
                await asyncio.sleep(2)
                await self._disconnect()

    async def async_refresh(self) -> None:
        """Open one BLE session and read battery + lock-config + state."""
        async with self._op_lock:
            try:
                await self._ensure_authorized()
                await self._read_status_in_session()
                self._notify_listeners()
            finally:
                await self._disconnect()

    # -- public API: momentary actions ------------------------------------
    async def async_beep(self) -> None:
        """Emit the acoustic 'find my lock' signal (not on Plus NoBond)."""
        await self._command(lambda s: s.emit_acoustic_signal())

    async def async_stop_alarm(self) -> None:
        await self._command(lambda s: s.stop_alarm())

    # -- public API: settings (assumed-state where no read-back) ----------
    async def async_set_alarm(self, on: bool, sensitivity: int | None = None) -> None:
        sens = self.alarm_sensitivity if sensitivity is None else sensitivity
        await self._command(lambda s: s.set_alarm(on, sensitivity=sens))
        self.alarm_active = on
        self.alarm_sensitivity = sens
        self._notify_listeners()

    async def async_set_sensitivity(self, sensitivity: int) -> None:
        self.alarm_sensitivity = sensitivity
        if self.alarm_active:
            await self.async_set_alarm(True, sensitivity)
        else:
            self._notify_listeners()

    async def async_set_dnd(self, on: bool) -> None:
        await self._command(lambda s: s.set_do_not_disturb(on))
        self.dnd = on
        self._notify_listeners()

    async def async_set_auto(self, on: bool) -> None:
        await self._command(lambda s: s.set_auto_open_close(on))
        self.auto_open_close = on
        self._notify_listeners()

    async def async_set_sound(
        self, closing: bool, opening: bool, warning: bool
    ) -> None:
        await self._command(lambda s: s.set_sound(closing, opening, warning))
        self.sound_closing = closing
        self.sound_opening = opening
        self.sound_warning = warning
        self._notify_listeners()

    # -- public API: recovery / destructive (exposed as services) ---------
    async def async_enter_pairing_mode(self) -> None:
        """Put the lock into pairing/coupling mode; the lock drops the link."""
        await self._command_expect_disconnect(lambda s: s.enter_pairing_mode())

    async def async_reset(self) -> None:
        """Factory-reset the lock. DESTRUCTIVE."""
        await self._command_expect_disconnect(lambda s: s.reset_lock())

    async def async_unpair(self) -> None:
        """Remove this phone's NoBond authorization. DESTRUCTIVE."""
        await self._command_expect_disconnect(lambda s: s.unpair())

    async def async_shutdown(self) -> None:
        await self._disconnect()

    @property
    def available(self) -> bool:
        return (
            bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            is not None
        )
