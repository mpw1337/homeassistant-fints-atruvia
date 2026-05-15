"""Button platform for fints_atruvia."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import FintsBankingCoordinator
from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up fints_atruvia button entities from a config entry."""
    coordinator: FintsBankingCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([FintsReAuthButton(coordinator)])


class FintsReAuthButton(CoordinatorEntity[FintsBankingCoordinator], ButtonEntity):
    """Button entity to confirm pending 2FA re-authentication."""

    def __init__(self, coordinator: FintsBankingCoordinator) -> None:
        """Initialise the re-authentication button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_reauth_button"
        self._attr_name = "Re-Authentifizierung bestätigen"
        self._attr_icon = "mdi:shield-key"

    @property
    def available(self) -> bool:
        """Return True only when 2FA confirmation is pending."""
        return super().available and self.coordinator.is_2fa_pending

    async def async_press(self) -> None:
        """Handle button press by completing the pending re-authentication."""
        if not self.coordinator.is_2fa_pending:
            return
        try:
            await self.coordinator.async_complete_reauth()
        except Exception as err:
            _LOGGER.debug("Re-auth completion failed", exc_info=True)
            raise HomeAssistantError(
                f"Re-authentication failed ({type(err).__name__})"
            ) from err
