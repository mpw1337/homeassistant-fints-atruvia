"""The fints_atruvia integration."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

DOMAIN = "fints_atruvia"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

# Event fired when a new transaction is detected by the coordinator.
# Payload: {integration_id, iban, date, amount, currency, purpose,
#           applicant_name, transaction_hash}
EVENT_NEW_TRANSACTION = "fints_atruvia_new_transaction"

# Window used when aggregating income/expense statistics.
STATS_WINDOW_DAYS = 30


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up fints_atruvia from a config entry."""
    from .coordinator import FintsBankingCoordinator

    coordinator = FintsBankingCoordinator(hass, entry)
    await coordinator.async_load_seen()
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
