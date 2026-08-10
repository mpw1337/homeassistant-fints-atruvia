"""The fints_atruvia integration."""
from __future__ import annotations

import hashlib
import logging
import uuid

import attr
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .storage import FintsCredentialStore, FintsStateStore

DOMAIN = "fints_atruvia"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

# Event fired when a new transaction is detected by the coordinator.
# Payload: {integration_id, iban, date, amount, currency, purpose,
#           applicant_name, transaction_hash}
EVENT_NEW_TRANSACTION = "fints_atruvia_new_transaction"

# Window used when aggregating income/expense statistics.
STATS_WINDOW_DAYS = 30

# Config entry data keys (post-v2: credentials no longer in entry.data)
CONF_BLZ = "blz"
CONF_URL = "url"
CONF_PRODUCT_ID = "product_id"
CONF_SELECTED_ACCOUNTS = "selected_accounts"
CONF_CREDENTIAL_ID = "credential_id"

# Options-flow toggle (default off). When off, the integration keeps bank
# transaction texts (purpose, counterparty name) out of:
#   - the new-transaction event payload
#   - sensor extra_state_attributes (and therefore the recorder + state API)
# Users who script automations against transaction texts can opt in per entry.
CONF_EXPOSE_FULL_DATA = "expose_full_data"

_LOGGER = logging.getLogger(__name__)

# Stats-sensor suffixes that follow the IBAN portion of a unique_id.
# Order matters: longest match first so ``_income_30d`` doesn't get partially
# matched by a shorter suffix.
_SENSOR_SUFFIXES = ("_income_30d", "_expense_30d")


def iban_unique_id(entry_id: str, iban: str) -> str:
    """Return a stable, IBAN-free identifier for sensor unique_ids.

    Banking IBANs landed in ``.storage/core.entity_registry`` under the old
    naming scheme. We hash them with the entry_id as salt so that, after
    ``_async_migrate_unique_ids`` runs, the registry contains the plaintext
    IBAN in neither ``unique_id`` nor ``previous_unique_id`` — the latter is
    also cleared post-migration, since HA's entity registry otherwise records
    the pre-migration value there. 16 hex chars (~64 bits) is plenty for
    collision resistance inside a single entry. Backups taken before this fix
    shipped may still contain the plaintext IBAN under ``previous_unique_id``.
    """
    return hashlib.sha256(f"{entry_id}|{iban}".encode("utf-8")).hexdigest()[:16]


async def _async_migrate_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Migrate legacy ``{entry_id}_{iban}[_suffix]`` unique_ids to a hashed form."""
    entry_id = entry.entry_id
    prefix = f"{entry_id}_"

    @callback
    def _migrate(reg_entry: er.RegistryEntry) -> dict | None:
        uid = reg_entry.unique_id
        if not uid.startswith(prefix):
            return None
        remainder = uid[len(prefix):]
        suffix = ""
        for known in _SENSOR_SUFFIXES:
            if remainder.endswith(known):
                suffix = known
                remainder = remainder[: -len(known)]
                break
        # The reauth button uses ``{entry_id}_reauth_button`` and has no IBAN.
        if remainder == "reauth_button":
            return None
        # Already migrated (16 lowercase hex chars).
        if len(remainder) == 16 and all(c in "0123456789abcdef" for c in remainder):
            return None
        # Anything else with a country-code-like prefix is treated as a legacy
        # IBAN and rewritten.
        if len(remainder) < 4 or not remainder[:2].isalpha():
            return None
        new_uid = f"{entry_id}_{iban_unique_id(entry_id, remainder)}{suffix}"
        return {"new_unique_id": new_uid}

    await er.async_migrate_entries(hass, entry_id, _migrate)
    _async_clear_previous_unique_ids(hass, entry_id)


def _async_clear_previous_unique_ids(hass: HomeAssistant, entry_id: str) -> None:
    """
    Scrub the pre-migration IBAN that HA stashes in previous_unique_id.

    ``er.async_migrate_entries`` (via ``EntityRegistry._async_update_entity``)
    unconditionally sets ``new_values["previous_unique_id"] = old.unique_id``,
    so the legacy plaintext-IBAN unique_id survives on disk even after the
    rehash above. There is no public API to suppress or clear that field —
    ``async_update_entity`` doesn't accept it, since HA derives it internally.
    Best effort: this must never abort entry setup.
    """
    try:
        registry = er.async_get(hass)
        changed = False
        for reg_entry in list(
            registry.entities.get_entries_for_config_entry_id(entry_id)
        ):
            if reg_entry.previous_unique_id is None:
                continue
            # No public API clears previous_unique_id; HA derives it inside
            # _async_update_entity. Writing through the items container
            # keeps the registry indexes consistent
            # (BaseRegistryItems.__setitem__ re-indexes).
            registry.entities[reg_entry.entity_id] = attr.evolve(
                reg_entry, previous_unique_id=None
            )
            changed = True
        if changed:
            registry.async_schedule_save()
    except Exception as exc:  # noqa: BLE001 - defensive, must not break setup
        _LOGGER.debug(
            "Could not clear previous_unique_id for entry %s: %s",
            entry_id,
            type(exc).__name__,
        )


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so option changes take effect immediately."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up fints_atruvia from a config entry."""
    from .coordinator import FintsBankingCoordinator
    from .frontend import async_register_card

    await async_register_card(hass)
    await _async_migrate_unique_ids(hass, entry)
    coordinator = FintsBankingCoordinator(hass, entry)
    await coordinator.async_init()
    await coordinator.async_load_seen()
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up encrypted credential and FinTS state files for this entry."""
    credential_id = entry.data.get(CONF_CREDENTIAL_ID)
    if not credential_id:
        return
    await FintsCredentialStore(hass, credential_id).remove()
    await FintsStateStore(hass, credential_id).remove()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current data layout.

    v1 (legacy): username and password lived in ``entry.data`` as cleartext.
    v2:          credentials moved to an encrypted store, ``entry.data``
                 keeps only a ``credential_id`` reference.

    Idempotent: if a credential_id already exists (partial v1→v2 retry), we
    reuse it instead of orphaning the previously encrypted blob. We also
    verify after the update that no plaintext password leaked through.
    """
    if entry.version == 1:
        legacy = entry.data
        username = legacy.get("username")
        password = legacy.get("password")
        if not username or not password:
            _LOGGER.error(
                "Cannot migrate entry %s: missing credentials in v1 data",
                entry.entry_id,
            )
            return False

        # If a previous migration attempt got as far as writing credentials
        # but not as far as updating the entry, reuse the existing id so the
        # old encrypted blob isn't orphaned.
        credential_id = legacy.get(CONF_CREDENTIAL_ID) or uuid.uuid4().hex
        await FintsCredentialStore(hass, credential_id).save(username, password)

        new_data = {
            CONF_BLZ: legacy.get("blz", ""),
            CONF_URL: legacy.get("url", ""),
            CONF_PRODUCT_ID: legacy.get("product_id") or None,
            CONF_SELECTED_ACCOUNTS: legacy.get("selected_accounts", []),
            CONF_CREDENTIAL_ID: credential_id,
        }
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=2,
        )
        # Defensive post-check: HA serialises the entry on update, so by the
        # time we return here the password field must be gone from entry.data.
        if "password" in entry.data or "username" in entry.data:
            _LOGGER.error(
                "Migration of entry %s did not clear plaintext credentials",
                entry.entry_id,
            )
            return False
        _LOGGER.info(
            "Migrated fints_atruvia entry %s to encrypted credential storage. "
            "Existing HA backups may still contain the plaintext PIN — "
            "delete or re-encrypt them.",
            entry.entry_id,
        )
    return True
