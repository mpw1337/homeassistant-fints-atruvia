"""Data update coordinator for fints_atruvia."""
from __future__ import annotations

import logging
from datetime import timedelta

from fints.client import NeedTANResponse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FinTsAtruviaClient
from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


class FintsBankingCoordinator(DataUpdateCoordinator[dict]):
    """Coordinator that polls the bank every 6 hours and handles the 90-day re-auth lifecycle."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=6),
        )
        data = config_entry.data
        self._client = FinTsAtruviaClient(
            blz=data["blz"],
            login=data["username"],
            pin=data["password"],
            url=data["url"],
            product_id=data.get("product_id") or None,
        )
        self._selected_accounts: list[str] = data.get("selected_accounts", [])
        self._tan_response: NeedTANResponse | None = None
        self.is_2fa_pending: bool = False

    async def _async_update_data(self) -> dict:
        """Fetch data for all selected accounts from the bank."""
        result: dict = {}
        try:
            for iban in self._selected_accounts:
                # Resolve the SEPAAccount object for this IBAN
                accounts = await self.hass.async_add_executor_job(
                    self._client.get_accounts
                )
                account = next(
                    (a for a in accounts if a.iban == iban),
                    None,
                )
                if account is None:
                    _LOGGER.warning("Account %s not found at bank, skipping", iban)
                    continue

                balance, currency = await self.hass.async_add_executor_job(
                    self._client.get_balance, account
                )
                transactions = await self.hass.async_add_executor_job(
                    self._client.get_transactions, account, 30
                )
                result[iban] = {
                    "balance": balance,
                    "currency": currency,
                    "transactions": transactions,
                }
        except NeedTANResponse as exc:
            self._tan_response = exc
            self.is_2fa_pending = True
            self.hass.components.persistent_notification.async_create(
                message=(
                    "Sparda-Bank fordert Re-Authentifizierung. "
                    "Bitte den Re-Auth-Button in der Integration drücken."
                ),
                notification_id="fints_atruvia_reauth",
            )
            # Return last known good data so sensors stay available
            return self.data if self.data is not None else {}
        except Exception as err:
            raise UpdateFailed(f"Error communicating with bank: {err}") from err

        # Successful update — clear any pending 2FA state
        self.is_2fa_pending = False
        self._tan_response = None
        return result

    async def async_complete_reauth(self) -> None:
        """Complete pending re-authentication after the user confirms SecureGo+."""
        if self._tan_response is None:
            return
        await self.hass.async_add_executor_job(
            self._client.complete_tan, self._tan_response, ""
        )
        self._tan_response = None
        self.is_2fa_pending = False
        await self.async_request_refresh()
