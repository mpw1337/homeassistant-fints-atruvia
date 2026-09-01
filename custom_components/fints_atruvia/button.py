"""Button platform for fints_atruvia."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN
from .coordinator import FintsBankingCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# Shown as a toast when the button is pressed before the SecureGo+ confirmation
# reached the bank (response code 3956). Constant rather than inline because
# ruff's TRY003 wants long exception texts out of the raise site.
_MSG_NOT_CONFIRMED = (
    "Die Bank hat die Freigabe noch nicht erhalten — bitte zuerst in der "
    "SecureGo+ App bestätigen und den Button dann erneut drücken."
)


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
            completed = await self.coordinator.async_complete_reauth()
        except Exception as err:
            _LOGGER.debug("Re-auth completion failed", exc_info=True)
            msg = f"Re-authentication failed ({type(err).__name__})"
            raise HomeAssistantError(msg) from err
        if not completed:
            raise HomeAssistantError(_MSG_NOT_CONFIRMED)
