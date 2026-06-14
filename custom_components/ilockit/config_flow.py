"""Config flow for the I LOCK IT integration.

The flow is credential-driven: the user signs in with their haveltec (I LOCK IT)
account and we fetch every lock's BLE credentials (authId / phoneId / LTK) plus
the Traccar device id for cloud location from the backend. The user then picks
which lock to add. A lock can also be discovered passively over Bluetooth, in
which case we still ask for the account once to obtain its credentials.

The BLE MAC address is resolved from the cloud (`mac` attribute) when present,
otherwise from a live Bluetooth advertisement matched by name.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .cloud import CloudError, CloudLock, fetch_locks
from .const import (
    CONF_ADDRESS,
    CONF_AUTH_ID,
    CONF_CLOUD_DEVICE_ID,
    CONF_EMAIL,
    CONF_LOCK_NAME,
    CONF_LTK,
    CONF_PASSWORD,
    CONF_PHONE_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ILockItConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for I LOCK IT."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._email: str | None = None
        self._password: str | None = None
        self._locks: list[CloudLock] = []

    # -- Bluetooth discovery ----------------------------------------------
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a lock discovered over Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_credentials()

    # -- Manual / credentials step ----------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the manual (UI "Add integration") start."""
        return await self.async_step_credentials(user_input)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the haveltec account and fetch the lock credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            try:
                self._locks = await fetch_locks(self._email, self._password)
            except CloudError as err:
                _LOGGER.warning("I LOCK IT cloud login failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error fetching I LOCK IT locks")
                errors["base"] = "unknown"
            else:
                if not self._locks:
                    errors["base"] = "no_locks"
                elif self._discovered_address is not None:
                    return await self._async_create_for_discovered()
                else:
                    return await self.async_step_select()

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=self._email or ""): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": self._discovered_name or ""},
        )

    # -- Lock selection (manual path) -------------------------------------
    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which lock on the account to add."""
        configured = {
            entry.unique_id for entry in self._async_current_entries()
        }
        choices: dict[str, str] = {}
        for lock in self._locks:
            address = self._resolve_address(lock)
            key = address or lock.name
            if key in configured:
                continue
            if address:
                choices[key] = f"{lock.name} ({address})"
            else:
                choices[key] = f"{lock.name} — nicht in Bluetooth-Reichweite"

        if not choices:
            return self.async_abort(reason="all_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input["lock"]
            lock = next(
                (
                    lk
                    for lk in self._locks
                    if (self._resolve_address(lk) or lk.name) == key
                ),
                None,
            )
            if lock is None:
                return self.async_abort(reason="unknown")
            address = self._resolve_address(lock)
            if address is None:
                errors["base"] = "not_in_range"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self._create_entry(lock, address)

        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema({vol.Required("lock"): vol.In(choices)}),
            errors=errors,
        )

    # -- helpers -----------------------------------------------------------
    async def _async_create_for_discovered(self) -> ConfigFlowResult:
        """Finish a Bluetooth-discovered flow by matching it to a cloud lock."""
        lock = self._match_lock(self._discovered_name)
        if lock is None:
            return self.async_abort(reason="no_matching_lock")
        assert self._discovered_address is not None
        return self._create_entry(lock, self._discovered_address)

    def _match_lock(self, name: str | None) -> CloudLock | None:
        """Find the cloud lock whose name matches an advertised BLE name."""
        if not name:
            return None
        lowered = name.lower()
        for lock in self._locks:
            if lock.name.lower() == lowered:
                return lock
        for lock in self._locks:
            if lock.name.lower() in lowered or lowered in lock.name.lower():
                return lock
        return None

    def _resolve_address(self, lock: CloudLock) -> str | None:
        """Best-effort BLE address for a cloud lock: cloud mac, else live advert."""
        if lock.mac:
            return lock.mac.upper()
        lowered = lock.name.lower()
        for info in bluetooth.async_discovered_service_info(self.hass):
            if (info.name or "").lower() == lowered:
                return info.address
        return None

    def _create_entry(self, lock: CloudLock, address: str) -> ConfigFlowResult:
        """Create the config entry for one lock."""
        data = {
            CONF_ADDRESS: address,
            CONF_AUTH_ID: lock.auth_id,
            CONF_PHONE_ID: lock.phone_id,
            CONF_LTK: lock.ltk_raw,
            CONF_LOCK_NAME: lock.name,
            CONF_EMAIL: self._email,
            CONF_PASSWORD: self._password,
            CONF_CLOUD_DEVICE_ID: lock.device_id,
        }
        return self.async_create_entry(title=lock.name, data=data)
