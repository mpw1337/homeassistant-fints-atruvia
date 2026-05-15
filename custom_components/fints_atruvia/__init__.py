"""The fints_atruvia integration."""
from __future__ import annotations

import logging
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

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
CONF_EXPOSE_FULL_IBAN = "expose_full_iban"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up fints_atruvia from a config entry."""
    from .coordinator import FintsBankingCoordinator

    coordinator = FintsBankingCoordinator(hass, entry)
    await coordinator.async_init()
    await coordinator.async_load_seen()
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
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

        credential_id = uuid.uuid4().hex
        await FintsCredentialStore(hass, credential_id).save(username, password)

        new_data = {
            CONF_BLZ: legacy.get("blz", ""),
            CONF_URL: legacy.get("url", ""),
            CONF_PRODUCT_ID: legacy.get("product_id") or None,
            CONF_SELECTED_ACCOUNTS: legacy.get("selected_accounts", []),
            CONF_CREDENTIAL_ID: credential_id,
        }
        # async_update_entry serialises via JSON; nothing sensitive left here.
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=2,
        )
        _LOGGER.info(
            "Migrated fints_atruvia entry %s to encrypted credential storage",
            entry.entry_id,
        )
    return True
