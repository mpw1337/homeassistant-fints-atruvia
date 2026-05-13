"""Data update coordinator for fints_atruvia."""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal

from homeassistant.components.persistent_notification import (
    async_create as pn_async_create,
    async_dismiss as pn_async_dismiss,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import DOMAIN, EVENT_NEW_TRANSACTION, STATS_WINDOW_DAYS
from .api import FinTsAtruviaClient, TanRequiredError

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_KEY_FMT = "fints_atruvia_seen_transactions_{entry_id}"


def _transaction_hash(txn: dict) -> str:
    """Build a stable identifier for a transaction.

    FinTS / MT940 does not give us a unique transaction id, so we hash the
    fields that together are practically unique within the polled window:
    date, signed amount, purpose text, counterparty name.
    """
    payload = "|".join(
        str(txn.get(field, "")) for field in ("date", "amount", "purpose", "creditor")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compute_stats(transactions: list[dict]) -> dict:
    """Aggregate income / expense / count over a transaction list."""
    income = Decimal("0")
    expense = Decimal("0")
    for txn in transactions:
        amount = txn.get("amount")
        if amount is None:
            continue
        try:
            value = Decimal(amount)
        except Exception:  # noqa: BLE001
            continue
        if value > 0:
            income += value
        elif value < 0:
            expense += -value
    return {
        "income_30d": income,
        "expense_30d": expense,
        "count_30d": len(transactions),
    }


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
        self.config_entry = config_entry
        data = config_entry.data
        self._client = FinTsAtruviaClient(
            blz=data["blz"],
            login=data["username"],
            pin=data["password"],
            url=data["url"],
            product_id=data.get("product_id") or None,
        )
        self._selected_accounts: list[str] = data.get("selected_accounts", [])
        self._tan_response = None
        self.is_2fa_pending: bool = False
        # Persisted set of transaction hashes per IBAN, prevents event flood
        # across restarts. ``_seen_initialised`` distinguishes a fresh install
        # (first update should seed the set without firing events) from a
        # later run.
        self._seen_hashes: dict[str, set[str]] = {}
        self._seen_initialised: bool = False
        self._store: Store = Store(
            hass,
            _STORAGE_VERSION,
            _STORAGE_KEY_FMT.format(entry_id=config_entry.entry_id),
        )

    async def async_load_seen(self) -> None:
        """Load the persisted set of seen transaction hashes once at setup."""
        stored = await self._store.async_load()
        if stored is None:
            self._seen_initialised = False
            self._seen_hashes = {}
        else:
            self._seen_initialised = True
            self._seen_hashes = {
                iban: set(hashes) for iban, hashes in stored.items()
            }

    async def _async_save_seen(self) -> None:
        """Persist the current seen-hashes snapshot to .storage."""
        await self._store.async_save(
            {iban: sorted(hashes) for iban, hashes in self._seen_hashes.items()}
        )

    def _detect_new_transactions(
        self, iban: str, transactions: list[dict]
    ) -> list[dict]:
        """Compare current transactions against last seen and return event payloads.

        Also resets the per-IBAN seen-hashes set to the current snapshot so
        hashes that fall out of the 30-day window are pruned.
        """
        current_by_hash: dict[str, dict] = {
            _transaction_hash(t): t for t in transactions
        }
        previous_known = self._seen_hashes.get(iban, set())

        if self._seen_initialised:
            new_hashes = set(current_by_hash) - previous_known
            events = [
                self._build_event_payload(iban, current_by_hash[h], h)
                for h in new_hashes
            ]
        else:
            # First ever run: seed without firing any events.
            events = []

        # Snapshot the seen-hashes set to current visibility window.
        self._seen_hashes[iban] = set(current_by_hash.keys())
        return events

    def _build_event_payload(self, iban: str, txn: dict, txn_hash: str) -> dict:
        amount = txn.get("amount")
        return {
            "integration_id": self.config_entry.entry_id,
            "iban": iban,
            "date": txn.get("date"),
            "amount": float(amount) if amount is not None else None,
            "currency": txn.get("currency"),
            "purpose": txn.get("purpose"),
            "applicant_name": txn.get("creditor"),
            "transaction_hash": txn_hash,
        }

    async def _async_update_data(self) -> dict:
        """Fetch data for all selected accounts, compute stats, emit events."""
        result: dict = {}
        new_events: list[dict] = []
        try:
            accounts = await self.hass.async_add_executor_job(
                self._client.get_accounts
            )
            for iban in self._selected_accounts:
                account = next((a for a in accounts if a.iban == iban), None)
                if account is None:
                    _LOGGER.warning("Account %s not found at bank, skipping", iban)
                    continue

                balance_data = await self.hass.async_add_executor_job(
                    self._client.get_balance, account
                )
                transactions = await self.hass.async_add_executor_job(
                    self._client.get_transactions, account, STATS_WINDOW_DAYS
                )

                stats = _compute_stats(transactions)
                new_events.extend(self._detect_new_transactions(iban, transactions))

                result[iban] = {
                    "iban": iban,
                    "balance": balance_data["balance"],
                    "currency": balance_data["currency"],
                    "available_balance": balance_data["available_balance"],
                    "balance_pending": balance_data["balance_pending"],
                    "pending_amount": balance_data["pending_amount"],
                    "booking_date": balance_data["booking_date"],
                    "transactions": transactions,
                    "stats": stats,
                }
        except TanRequiredError as e:
            self._tan_response = e.response
            self.is_2fa_pending = True
            pn_async_create(
                self.hass,
                title="FinTS Atruvia",
                message=(
                    "Sparda-Bank fordert Re-Authentifizierung. "
                    "Bitte den Re-Auth-Button in der Integration drücken."
                ),
                notification_id="fints_atruvia_reauth",
            )
            if self.data is None:
                raise ConfigEntryAuthFailed("Bank requires initial authentication (TAN)") from e.response
            # Intentionally return last known complete data rather than partial result.
            # Fresh data for already-processed IBANs is discarded to avoid partial state.
            return self.data
        except Exception as err:
            _LOGGER.exception("FinTS update failed")
            raise UpdateFailed(f"Error communicating with bank: {err}") from err

        # Update succeeded for all accounts: mark seen-set as initialised, fire
        # events, and persist. Persistence happens after firing so the receiving
        # automations have observed the events when state is checkpointed.
        was_uninitialised = not self._seen_initialised
        self._seen_initialised = True
        if not was_uninitialised:
            for payload in new_events:
                self.hass.bus.async_fire(EVENT_NEW_TRANSACTION, payload)
        await self._async_save_seen()

        # Clear any pending 2FA state.
        self.is_2fa_pending = False
        self._tan_response = None
        pn_async_dismiss(self.hass, "fints_atruvia_reauth")
        return result

    async def async_complete_reauth(self, tan: str = "") -> None:
        """Complete pending re-authentication after the user confirms SecureGo+."""
        if self._tan_response is None:
            return
        await self.hass.async_add_executor_job(
            self._client.complete_tan, self._tan_response, tan
        )
        self._tan_response = None
        self.is_2fa_pending = False
        pn_async_dismiss(self.hass, "fints_atruvia_reauth")
        await self.async_request_refresh()
