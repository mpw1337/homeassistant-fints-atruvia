"""Encrypted credential and FinTS-state storage for fints_atruvia.

Two .storage files are used so a partial backup leak (one file copied
without the other) cannot recover the PIN:

* ``fints_atruvia_master_key`` — Fernet key (shared across all entries).
* ``fints_atruvia_credentials_<credential_id>`` — Fernet-encrypted blob
  containing username and PIN for one config entry.

Both files are written with HA's ``Store(private=True)`` so the file
permission is 0600. Encryption is defense-in-depth: it protects against
partial-leak scenarios (cloud-sync of a single file, copied-out JSON
during debugging, log captures). It does NOT protect against full-disk
theft — the master key lives next to the data. Encrypt your HA host and
your backups.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

_MASTER_KEY_VERSION = 1
_MASTER_KEY_STORAGE = "fints_atruvia_master_key"

_CRED_VERSION = 1
_CRED_STORAGE_FMT = "fints_atruvia_credentials_{credential_id}"

_FINTS_STATE_VERSION = 2
_FINTS_STATE_STORAGE_FMT = "fints_atruvia_state_{credential_id}"


class CredentialStoreError(Exception):
    """Raised when credentials cannot be loaded or decrypted."""


def _master_store(hass: HomeAssistant) -> Store:
    return Store(hass, _MASTER_KEY_VERSION, _MASTER_KEY_STORAGE, private=True)


async def _get_or_create_master_key(hass: HomeAssistant) -> bytes:
    # HA's Store serialises async_load/async_save through an internal lock,
    # so concurrent first-time setups of two entries cannot generate two
    # different master keys here.
    store = _master_store(hass)
    data = await store.async_load()
    if isinstance(data, dict) and isinstance(data.get("key"), str):
        return data["key"].encode("ascii")
    key = Fernet.generate_key()
    await store.async_save({"key": key.decode("ascii")})
    _LOGGER.debug("Generated new fints_atruvia master key")
    return key


class FintsCredentialStore:
    """Persists username/PIN as a Fernet-encrypted blob, keyed by credential_id."""

    def __init__(self, hass: HomeAssistant, credential_id: str) -> None:
        self._hass = hass
        self._credential_id = credential_id
        self._store = Store(
            hass,
            _CRED_VERSION,
            _CRED_STORAGE_FMT.format(credential_id=credential_id),
            private=True,
        )

    async def save(self, username: str, pin: str) -> None:
        """Encrypt and persist the given credentials."""
        key = await _get_or_create_master_key(self._hass)
        fernet = Fernet(key)
        payload = json.dumps({"username": username, "pin": pin}).encode("utf-8")
        ciphertext = fernet.encrypt(payload).decode("ascii")
        await self._store.async_save({"ciphertext": ciphertext})

    async def load(self) -> dict[str, str]:
        """Return {'username', 'pin'} or raise CredentialStoreError."""
        cred_data = await self._store.async_load()
        if not isinstance(cred_data, dict) or "ciphertext" not in cred_data:
            raise CredentialStoreError("No stored credentials for this entry")

        master = await _master_store(self._hass).async_load()
        if not isinstance(master, dict) or "key" not in master:
            raise CredentialStoreError("Master key missing — cannot decrypt")

        try:
            fernet = Fernet(master["key"].encode("ascii"))
            plaintext = fernet.decrypt(cred_data["ciphertext"].encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise CredentialStoreError("Credentials could not be decrypted") from exc

        try:
            decoded = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("Credential blob is malformed") from exc

        if not isinstance(decoded, dict) or "username" not in decoded or "pin" not in decoded:
            raise CredentialStoreError("Credential blob is missing required fields")
        return {"username": str(decoded["username"]), "pin": str(decoded["pin"])}

    async def remove(self) -> None:
        """Delete the credentials file. Master key stays for other entries."""
        await self._store.async_remove()


class FintsStateStore:
    """Persists the python-fints deconstruct() blob (system_id, BPD, UPD).

    The blob does NOT contain a PIN per the python-fints contract, but it
    does contain account numbers, BLZ, BPD and UPD — bank-internal data that
    SECURITY.md classifies as sensitive. Stored Fernet-encrypted with the
    same master key as credentials, file mode 0600 via ``Store(private=True)``.

    v1 (legacy): ``{"blob": "<hex>"}`` plaintext. Read once at load time and
    transparently re-encrypted on the next save.
    v2:          ``{"ciphertext": "<fernet>"}`` encrypted.
    """

    def __init__(self, hass: HomeAssistant, credential_id: str) -> None:
        self._hass = hass
        self._store = Store(
            hass,
            _FINTS_STATE_VERSION,
            _FINTS_STATE_STORAGE_FMT.format(credential_id=credential_id),
            private=True,
        )

    async def load(self) -> bytes | None:
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return None

        if "ciphertext" in data:
            key = await _get_or_create_master_key(self._hass)
            try:
                return Fernet(key).decrypt(data["ciphertext"].encode("ascii"))
            except (InvalidToken, ValueError):
                # Corrupted blob or master-key rotation. Drop it so the next
                # bank dialog rebuilds the state from scratch — that triggers
                # a fresh SCA but is safer than feeding garbage to python-fints.
                _LOGGER.warning("FinTS state blob could not be decrypted, discarding")
                return None

        if "blob" in data:
            # v1 plaintext. Decode and let the caller's next save() re-encrypt.
            try:
                return bytes.fromhex(data["blob"])
            except (TypeError, ValueError):
                return None
        return None

    async def save(self, blob: bytes | None) -> None:
        if blob is None:
            await self._store.async_remove()
            return
        key = await _get_or_create_master_key(self._hass)
        ciphertext = Fernet(key).encrypt(blob).decode("ascii")
        await self._store.async_save({"ciphertext": ciphertext})

    async def remove(self) -> None:
        await self._store.async_remove()


def redact_credentials(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with credentials redacted.

    Used when logging entry contents at debug level.
    """
    redacted = dict(data)
    for key in ("password", "username", "credential_id"):
        if key in redacted:
            redacted[key] = "***"
    return redacted
