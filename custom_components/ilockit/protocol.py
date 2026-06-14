"""I LOCK IT BLE protocol — transport-agnostic core.

Reconstructed from the official Android app (de.app.haveltec.ilockit 4.1.17).
See PROTOCOL.md for the full analysis. This module has NO Home Assistant or
bleak dependency: it consumes notification bytes and produces (characteristic,
payload) write requests, so it can be driven equally by the HA component or by
a standalone aioesphomeapi test harness.

Crypto primitives (verified against the APK):
  * AES/CBC/NoPadding, input zero-padded to a multiple of 16.
  * SHA-256.
  * CRC-16/CCITT-FALSE (init 0xFFFF, poly 0x1021), appended little-endian.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GATT UUIDs (NoBond variant). See ConstantsKt in the APK.
# ---------------------------------------------------------------------------
AUTH_SERVICE_UUID = "0000f00f-1212-efde-1523-785fef13d123"
LEGACY_SERVICE_UUID = "0000f00d-1212-efde-1523-785fef13d123"
# GDIO = General Data In/Out (handshake, plaintext). Note: uppercase BAAA.
GDIO_UUID = "0000baaa-1212-efde-1523-785fef13d123"
# USDIO = User Specific Data In/Out (encrypted commands). Note: uppercase BBBB.
USDIO_UUID = "0000bbbb-1212-efde-1523-785fef13d123"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

LTK_DERIVATION_SUFFIX = b"ILockIt Plus LTK"  # 16 bytes, appended during setup

# Lock-control payload bytes (BleDeviceRepository.lockIt / unlockIt).
CONTROL_UNLOCK = 0x01  # open
CONTROL_LOCK = 0x02  # close
CONTROL_CHAIN_UNLOCK = 0x03
CONTROL_CHAIN_LOCK = 0x04

# --- USDIO command opcodes (verified against the APK, see PROTOCOL.md §7/§12) ---
# Reads use REQUEST_DATA (0x01) + an item byte: [0x01, 0x00, item, 0x00]; the
# lock replies with an encrypted frame whose inner command == item.
REQUEST_DATA_CMD = 0x01
ITEM_LOCK_CONFIG = 0x0F   # reply parsed by NbUsdioHandler.parseLockConfig
ITEM_PRO_LOCK_CONFIG = 0x0D
ITEM_COLOR_CODE = 0x12
ITEM_BATTERY = 0x1B
ITEM_GPS_UID = 0x23

# DEVICE_SETTINGS (0x18) sub-commands (single byte after [0x18, 0x00]).
DEVICE_SETTINGS_CMD = 0x18
DS_RESET = 0x02
DS_ENTER_PAIRING = 0x05   # BleILIManager.nobond…enterPairingMode -> [18 00 05]
DS_STOP_ALARM = 0x0A
DS_ATP_ON = 0x0B          # automatic-open/close (ATP) on
DS_ATP_OFF = 0x0C

# Stand-alone USDIO command opcodes.
CMD_LOCK_CONTROL = 0x11   # + 2-byte counter + control byte
CMD_COLOR_CODE = 0x12
CMD_ALARM = 0x13
CMD_SOUND = 0x14
CMD_DISTANCE = 0x15       # classic distance open/close & automatic control
CMD_SHARING_CODE = 0x16   # disable with FF FF FF
CMD_REMOVE_NOBOND = 0x1A  # unpair this phone (+ authId byte)
CMD_DO_NOT_DISTURB = 0x1D  # payload 0x01=on / 0x00=off
CMD_ACOUSTIC_SIGNAL = 0x24  # "find my lock" beep (NOT supported on Plus NoBond)
CMD_AUTOMATIC_CONTROL_PRO = 0x25


class LockState(IntEnum):
    """Status bytes from GDIO type-14 messages (NbGdioHandler.receivedStatus)."""

    NONE = 0
    LOCKING = 1
    CLOSED = 2
    ERROR_LOCKING_BLOCKED = 3
    ERROR_LOCKING_MOVED = 4
    MOVEMENT_DETECTION_REQUESTED = 5
    UNLOCKING = 10
    OPEN = 11
    ERROR_UNLOCKING_BLOCKED = 12
    UNKNOWN = 15
    ALARM_TRIGGERED = 16
    ALARM_STOPPED = 32
    AUTHORIZED = 48
    AUTHORIZED_ALT = 49
    LOW_BATTERY = 64
    REQUEST_BATTERY = 65
    PAIRING_MODE_FAILURE = 80
    RESET_OK = 83
    GENERAL_ERROR = 0x90
    WRONG_AUTH_ID = 0x96
    AUTH_FAILED = 0x97


# ---------------------------------------------------------------------------
# Crypto primitives
# ---------------------------------------------------------------------------
def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _zero_pad16(data: bytes) -> bytes:
    if len(data) % 16:
        data = data + b"\x00" * (16 - len(data) % 16)
    return data


def aes_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES/CBC/NoPadding with zero padding (matches ble/crc/AES.java)."""
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(_zero_pad16(data)) + enc.finalize()


def aes_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    data = _zero_pad16(data)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def crc16_ccitt(data: bytes) -> bytes:
    """CRC-16/CCITT-FALSE, returned as 2 bytes little-endian (low byte first).

    Mirrors CRC_CITT.calculate(): big-endian 16-bit value, then reversed.
    """
    crc = 0xFFFF
    for b in data:
        for i in range(8):
            bit = (b >> (7 - i)) & 1
            msb = (crc >> 15) & 1
            crc = (crc << 1) & 0xFFFF
            if bit ^ msb:
                crc ^= 0x1021
    crc &= 0xFFFF
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])  # little-endian


def crc_valid(buf: bytes, length: int) -> bool:
    """Validate trailing CRC over buf[0:length-2] == buf[length-2:length]."""
    length &= 0xFF
    if length < 2 or length > len(buf):
        return False
    return crc16_ccitt(buf[: length - 2]) == buf[length - 2 : length]


def derive_ltk(seed32: bytes) -> bytes:
    """LTK = SHA256(seed(32) || 16 zero bytes || 'ILockIt Plus LTK')."""
    return sha256(seed32 + b"\x00" * 16 + LTK_DERIVATION_SUFFIX)


def _long_term_phrase(lock_name: str) -> bytes:
    """LockUtil.generateLongTermPhrase: SHA-256 iterated 63x over the name."""
    data = lock_name.encode("utf-8")
    for _ in range(63):
        data = sha256(data)
    return data


def decrypt_long_term(b64_ltk: str, lock_name: str) -> bytes:
    """Recover the raw 32-byte LTK from the stored/synced `ltk` attribute.

    LockUtil.decryptLongTerm: AES-CBC-decrypt(base64(ltk), SHA256^63(name), iv=0).
    """
    import base64

    blob = base64.b64decode(b64_ltk)
    return aes_decrypt(blob, _long_term_phrase(lock_name), b"\x00" * 16)


def generate_phone_id() -> bytes:
    """Generate an 8-byte phone id for HA (analogous to the app's androidId[:8]).

    The app uses the first 8 ASCII chars of Settings.Secure.ANDROID_ID. Any stable
    8-byte value works as long as the same value is presented on every connect.
    """
    return os.urandom(8)


# Color code: every lock has a printed 6-symbol color code. The lock returns it
# as 3 bytes (USDIO cmd 0x12); each byte packs two base-10 digits (tens, units),
# each 0-3. Digit->color verified against 4500329's documented code:
# bytes 16 0a 20 = decimal 22/10/32 = "221032" = rot rot blau grün weiß rot.
COLOR_CODE_COLORS = {0: "grün", 1: "blau", 2: "rot", 3: "weiß"}


def decode_color_code(raw: bytes) -> tuple[str, list[str]] | None:
    """Decode the 3 color-code bytes → (6-digit string, [color names]).

    Mirrors ColorCodeUtil.format: each byte rendered as two decimal digits, each
    0-3. Returns None if the bytes are not a valid color code.
    """
    if len(raw) < 3:
        return None
    digits = "".join(f"{b:02d}" for b in raw[:3])
    if len(digits) != 6 or any(c not in "0123" for c in digits):
        return None
    return digits, [COLOR_CODE_COLORS[int(c)] for c in digits]


def color_names(digits: str) -> list[str] | None:
    """Map a printed 6-digit color code ('0'-'3' each) to its color names.

    The cloud serves the color code as this 6-digit string directly (e.g.
    "221032"), so no byte unpacking is needed. Returns None if invalid.
    """
    if not digits or len(digits) != 6 or any(c not in "0123" for c in digits):
        return None
    return [COLOR_CODE_COLORS[int(c)] for c in digits]


def pack_code(digits: str) -> bytes:
    """Pack a 6-digit color/sharing code ('0'-'3' each) into 3 bytes.

    Reverse of decode_color_code: each byte = tens*10 + units of a digit pair
    (e.g. '221032' -> 22,10,32 = bytes 16 0a 20). Used for color & sharing codes.
    """
    if len(digits) != 6 or any(c not in "0123" for c in digits):
        raise ValueError("code must be 6 characters, each '0'-'3'")
    return bytes(int(digits[i]) * 10 + int(digits[i + 1]) for i in range(0, 6, 2))


def sound_settings_byte(lock_sound: bool, unlock_sound: bool, warn_sound: bool) -> int:
    """Sound config byte 0-7 (mirrors SoundConfigUtil.settingsToByte).

    lock_sound = closing sound, unlock_sound = opening sound (FW>14 only),
    warn_sound = warning sound.
    """
    if unlock_sound:
        if warn_sound:
            return 6 if lock_sound else 7
        return 5 if lock_sound else 4
    if not warn_sound:
        return 2 if lock_sound else 3
    return 0 if lock_sound else 1


# Alarm sensitivity class (AlarmConfigUtil.encodeSensitivityClass): UI level 1-6.
_ALARM_SENS_CLASS = {1: 2, 2: 4, 3: 8, 4: 16, 5: 64, 6: 128}


def alarm_mode_byte(sensitivity: int = 1, mute: bool = False, pre_alarm: bool = False) -> int:
    """Alarm mode byte (mirrors AlarmConfigUtil.settingsToByte).

    sensitivity 1-6 (max 4 on non-Pro), bit0=mute, bit5(0x20)=pre-alarm.
    """
    v = (1 if mute else 0) + _ALARM_SENS_CLASS.get(sensitivity, 2)
    if pre_alarm:
        v += 32
    return v & 0xFF


def compose_auto_mode(auto_open: bool, auto_close: bool) -> int:
    """Automatic-mode byte 0-3 (AutoModeUtil.composeAutoMode): bit1=open, bit0=close."""
    return (2 if auto_open else 0) | (1 if auto_close else 0)


def auto_open_distance_byte(slider: int, pro: bool = False, fw_under15: bool = False) -> int:
    """UI open-distance slider -> device byte (AutoModeUtil.fromAppOpenDistance)."""
    if pro:
        return {0: 40, 1: 50, 2: 60, 3: 70, 4: 75}.get(slider, 80)
    i = slider + (1 if fw_under15 else 0)
    return {1: 5, 2: 1, 3: 2, 4: 3, 5: 4}.get(i, 6)


def auto_close_distance_byte(slider: int) -> int:
    """UI close-distance slider -> device byte (AutoModeUtil.fromAppCloseDistance)."""
    return slider if 1 <= slider <= 4 else 5


# ---------------------------------------------------------------------------
# Message framing (ILockItBleUtil.createILockItMessage)
# ---------------------------------------------------------------------------
def build_usdio_message(auth_id: int, plain: bytes, key: bytes, iv: bytes) -> bytes:
    """Wrap a plaintext command in the encrypted USDIO frame.

    inner = [authId, len(plain)+4] + plain + crc16(inner_so_far)
    wire  = [authId, len(enc)+2]   + AES(inner, key, iv)
    """
    inner = bytes([auth_id & 0xFF, (len(plain) + 4) & 0xFF]) + plain
    inner += crc16_ccitt(inner)
    enc = aes_encrypt(inner, key, iv)
    return bytes([auth_id & 0xFF, (len(enc) + 2) & 0xFF]) + enc


@dataclass
class Credentials:
    """Persistent per-lock credentials, obtained via pairing (setup flow)."""

    auth_id: int
    phone_id: bytes
    ltk: bytes  # 32-byte long-term key (raw, already decrypted)
    serial: bytes | None = None
    counter: int = 0  # monotonic lock-control replay counter


@dataclass
class _RxBuffer:
    """Reassembly + decrypt for the USDIO notification stream."""

    data: bytes = b""
    bytes_left: int = 0

    def feed(self, packet: bytes) -> bytes | None:
        """Return a complete ciphertext frame once fully received, else None."""
        i = 0
        if self.bytes_left == 0:
            self.data = b""
            self.bytes_left = packet[1] - 2  # total length field minus 2
            i = 2
        end = min(len(packet) - 1, self.bytes_left + i - 1)
        while i <= end:
            self.data += bytes([packet[i]])
            self.bytes_left -= 1
            i += 1
        if self.bytes_left == 0:
            frame = self.data
            self.data = b""
            return frame
        if self.bytes_left < 0:
            self.bytes_left = 0
            self.data = b""
        return None


# (characteristic_uuid, payload) tuple to be written by the transport layer.
WriteRequest = tuple[str, bytes]


@dataclass
class ILockItSession:
    """Drives the NoBond *authorize* (normal-operation) handshake.

    Usage:
        sess = ILockItSession(creds)
        for w in sess.start():            # write each to its characteristic
            transport.write(*w)
        # on every GDIO notification:
        for w in sess.on_gdio(data): transport.write(*w)
        # on every USDIO notification:
        for w in sess.on_usdio(data): transport.write(*w)
        # once sess.authorized: sess.lock()/unlock() return WriteRequests.

    Callbacks (optional) fire on state changes.
    """

    creds: Credentials
    on_state: Callable[[LockState], None] | None = None
    on_authorized: Callable[[], None] | None = None
    on_battery: Callable[[int], None] | None = None
    on_error: Callable[[LockState], None] | None = None
    on_lock_config: Callable[[dict], None] | None = None
    on_color_code: Callable[[bytes], None] | None = None
    on_gps_id: Callable[[str], None] | None = None

    authorized: bool = False
    iv: bytes | None = None
    _rx: _RxBuffer = field(default_factory=_RxBuffer)
    # Lock-control replay counter. Starts at 1 each session and is NOT persisted
    # across connections (the app resets it to 1 per session; see NbUsdioHandler).
    _counter: int = 1

    # -- outbound ----------------------------------------------------------
    def start(self) -> list[WriteRequest]:
        """Kick off authorize: request the session IV over GDIO."""
        self.authorized = False
        self.iv = None
        self._counter = 1  # reset replay counter per session
        # 01 00 09 00 + CRC  (REQUEST_DATA, item 9 = "send IV")
        body = bytes([0x01, 0x00, 0x09, 0x00])
        return [(GDIO_UUID, body + crc16_ccitt(body))]

    def _usdio(self, plain: bytes) -> WriteRequest:
        assert self.iv is not None, "IV not yet received"
        return (USDIO_UUID, build_usdio_message(self.creds.auth_id, plain, self.creds.ltk, self.iv))

    def lock(self) -> WriteRequest:
        return self._lock_control(CONTROL_LOCK)

    def unlock(self) -> WriteRequest:
        return self._lock_control(CONTROL_UNLOCK)

    def lock_chain(self) -> WriteRequest:
        return self._lock_control(CONTROL_CHAIN_LOCK)

    def unlock_chain(self) -> WriteRequest:
        return self._lock_control(CONTROL_CHAIN_UNLOCK)

    def _lock_control(self, control: int) -> WriteRequest:
        """NbUsdioHandler.lockUnlock: cmd 0x11 + 2-byte counter + control byte."""
        c = self._counter & 0xFFFF
        plain = bytes([0x11, 0x00, c & 0xFF, (c >> 8) & 0xFF, control & 0xFF])
        req = self._usdio(plain)
        self._counter = (self._counter + 1) & 0xFFFF
        return req

    def request_battery(self) -> WriteRequest:
        # 01 00 1B 00  (REQUEST_DATA + battery item) -> reply cmd 0x1B, payload[0] = %
        return self._usdio(bytes([REQUEST_DATA_CMD, 0x00, ITEM_BATTERY, 0x00]))

    def request_lock_config(self) -> WriteRequest:
        """Ask for the full lock-config packet (lock state, alarm, distances...)."""
        return self._usdio(bytes([REQUEST_DATA_CMD, 0x00, ITEM_LOCK_CONFIG, 0x00]))

    def request_color_code(self) -> WriteRequest:
        """Ask for the lock's 6-digit color/identity code."""
        return self._usdio(bytes([REQUEST_DATA_CMD, 0x00, ITEM_COLOR_CODE, 0x00]))

    def request_gps_id(self) -> WriteRequest:
        """Ask for the GPS module unique id (GPS lock variant only)."""
        return self._usdio(bytes([REQUEST_DATA_CMD, 0x00, ITEM_GPS_UID, 0x00]))

    def stop_alarm(self) -> WriteRequest:
        # DEVICE_SETTINGS cmd 0x18 + 0x0A (STOP_ALARM)
        return self._usdio(bytes([DEVICE_SETTINGS_CMD, 0x00, DS_STOP_ALARM]))

    def enter_pairing_mode(self) -> WriteRequest:
        """Put the lock into pairing/coupling mode (DEVICE_SETTINGS 0x18 + 0x05).

        Requires an authorized session. After this, a new phone can enroll a
        fresh phoneId/LTK. Used to recover a lock whose app authorization was
        evicted (see the test-takes-control note).
        """
        return self._usdio(bytes([DEVICE_SETTINGS_CMD, 0x00, DS_ENTER_PAIRING]))

    def reset_lock(self) -> WriteRequest:
        """Factory-reset the lock (DEVICE_SETTINGS 0x18 + 0x02). Destructive."""
        return self._usdio(bytes([DEVICE_SETTINGS_CMD, 0x00, DS_RESET]))

    def set_auto_open_close(self, enabled: bool) -> WriteRequest:
        """Toggle automatic open/close (ATP): 0x18 + 0x0B on / 0x0C off."""
        sub = DS_ATP_ON if enabled else DS_ATP_OFF
        return self._usdio(bytes([DEVICE_SETTINGS_CMD, 0x00, sub]))

    def emit_acoustic_signal(self) -> WriteRequest:
        """Make the lock beep ("find my lock"). NOT supported on Plus NoBond."""
        return self._usdio(bytes([CMD_ACOUSTIC_SIGNAL, 0x00]))

    def set_do_not_disturb(self, enabled: bool) -> WriteRequest:
        """Enable/disable do-not-disturb (silences alarm pushes): 0x1D + 01/00."""
        return self._usdio(bytes([CMD_DO_NOT_DISTURB, 0x00, 0x01 if enabled else 0x00]))

    def unpair(self) -> WriteRequest:
        """Remove this phone's NoBond authorization from the lock (0x1A + authId)."""
        return self._usdio(bytes([CMD_REMOVE_NOBOND, 0x00, self.creds.auth_id & 0xFF]))

    # -- settings writes (payload formats derived from the app ViewModels) --
    def set_sound(self, closing: bool, opening: bool, warning: bool) -> WriteRequest:
        """Sound settings (0x14, 1 byte). opening sound needs FW > 14."""
        return self._usdio(bytes([CMD_SOUND, 0x00,
                                  sound_settings_byte(closing, opening, warning)]))

    def set_alarm(self, on: bool, sensitivity: int = 1, mute: bool = False,
                  pre_alarm: bool = False) -> WriteRequest:
        """Alarm settings (0x13, 2 bytes [on/off, mode]). sensitivity 1-6 (max 4 non-Pro)."""
        mode = alarm_mode_byte(sensitivity, mute, pre_alarm)
        return self._usdio(bytes([CMD_ALARM, 0x00, 0x01 if on else 0x00, mode]))

    def set_color_code(self, digits: str) -> WriteRequest:
        """Set the 6-symbol color code (0x12, 3 bytes). digits = 6 chars '0'-'3'."""
        return self._usdio(bytes([CMD_COLOR_CODE, 0x00]) + pack_code(digits))

    def set_sharing_code(self, digits: str | None) -> WriteRequest:
        """Set/clear the sharing code (0x16, 3 bytes). digits = 6 chars '0'-'3',
        or None to disable (FF FF FF)."""
        payload = b"\xff\xff\xff" if digits is None else pack_code(digits)
        return self._usdio(bytes([CMD_SHARING_CODE, 0x00]) + payload)

    def set_automatic(self, auto_open: bool, auto_close: bool,
                      open_distance: int, close_distance: int,
                      pro: bool = False) -> WriteRequest:
        """Automatic open/close (classic 0x15 / Pro 0x25, 3 bytes
        [mode, openDistance, closeDistance]).

        mode = compose_auto_mode(auto_open, auto_close) (0-3). The distance args
        are DEVICE bytes — use auto_open_distance_byte()/auto_close_distance_byte()
        to convert from UI slider positions.
        """
        cmd = CMD_AUTOMATIC_CONTROL_PRO if pro else CMD_DISTANCE
        mode = compose_auto_mode(auto_open, auto_close)
        return self._usdio(bytes([cmd, 0x00, mode,
                                  open_distance & 0xFF, close_distance & 0xFF]))

    # -- inbound -----------------------------------------------------------
    def on_gdio(self, data: bytes) -> list[WriteRequest]:
        if not data:
            return []
        msg_type = data[0]
        if msg_type == 9:  # IV (20 bytes): [09, len, iv(16), crc(2)]
            self.iv = data[2 : len(data) - 2]
            _LOGGER.debug("Received IV (%d bytes)", len(self.iv))
            # First encrypted message: request challenge.
            # inner plain handed to build_usdio_message: 01 00 04 00
            return [self._usdio(bytes([0x01, 0x00, 0x04, 0x00]))]
        if msg_type == 14:  # status
            self._handle_status(data[2])
        return []

    def on_usdio(self, packet: bytes) -> list[WriteRequest]:
        frame = self._rx.feed(packet)
        if frame is None:
            return []
        dec = aes_decrypt(frame, self.creds.ltk, self.iv or b"\x00" * 16)
        if not crc_valid(dec, dec[1]):
            _LOGGER.warning("USDIO CRC failed: %s", dec.hex())
            return []
        cmd = dec[2]
        if cmd == 4:  # re-auth challenge on encrypted channel
            return [self._answer_challenge(dec)]
        if cmd == ITEM_BATTERY:  # 0x1B battery level, payload[0] = percent
            if self.on_battery:
                self.on_battery(dec[4])
        elif cmd == ITEM_LOCK_CONFIG:  # 0x0F full lock config
            self._handle_lock_config(dec)
        elif cmd == CMD_COLOR_CODE:  # 0x12 color code (3 bytes)
            if self.on_color_code and len(dec) >= 7:
                self.on_color_code(dec[4:7])
        elif cmd == ITEM_GPS_UID:  # 0x23 GPS unique id (8 bytes, reversed -> hex)
            if self.on_gps_id and len(dec) >= 12:
                self.on_gps_id(dec[4:12][::-1].hex())
        # cmd 0x0d pro-lock-config, 10 ltk-seed (setup) — not handled here.
        return []

    def _handle_lock_config(self, dec: bytes) -> None:
        """Parse the classic lock-config packet (NbUsdioHandler.parseLockConfig)."""
        if len(dec) < 6:
            return
        state_byte = dec[4]
        if state_byte == 11:
            state = LockState.OPEN
        elif state_byte == 15:
            state = LockState.UNKNOWN
        else:
            state = LockState.CLOSED
        config = {
            "lock_state": state,
            "alarm_active": dec[5] != 0,
            "distance_open": dec[16:19].hex() if len(dec) >= 19 else None,
            "distance_close": dec[19:22].hex() if len(dec) >= 22 else None,
            "raw": dec.hex(),
        }
        if self.on_lock_config:
            self.on_lock_config(config)
        if self.on_state:
            self.on_state(state)

    def _answer_challenge(self, dec: bytes) -> WriteRequest:
        """NbUsdioHandler.receivedChallenge: cmd 0x10 + challenge + phoneId."""
        challenge = dec[4:36]  # 32 bytes
        inner = bytes([0x10, 0x00]) + challenge + self.creds.phone_id
        # Manual frame: [authId, 46] + inner + crc, then AES, prefix [authId, 50].
        framed = bytes([self.creds.auth_id, 46]) + inner
        framed += crc16_ccitt(framed)
        enc = aes_encrypt(framed, self.creds.ltk, self.iv)
        return (USDIO_UUID, bytes([self.creds.auth_id, 50]) + enc)

    def _handle_status(self, status: int) -> None:
        try:
            state = LockState(status)
        except ValueError:
            _LOGGER.debug("Unknown status byte 0x%02x", status)
            return
        if state in (LockState.AUTHORIZED, LockState.AUTHORIZED_ALT):
            self.authorized = True
            if self.on_authorized:
                self.on_authorized()
            return
        if state in (LockState.GENERAL_ERROR, LockState.WRONG_AUTH_ID, LockState.AUTH_FAILED):
            if self.on_error:
                self.on_error(state)
            return
        if self.on_state:
            self.on_state(state)
