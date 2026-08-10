"""Data update coordinator for fints_atruvia."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal

from homeassistant.components.persistent_notification import (
    async_create as pn_async_create,
)
from homeassistant.components.persistent_notification import (
    async_dismiss as pn_async_dismiss,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import (
    CONF_BLZ,
    CONF_CREDENTIAL_ID,
    CONF_EXPOSE_FULL_DATA,
    CONF_PRODUCT_ID,
    CONF_SELECTED_ACCOUNTS,
    CONF_URL,
    DOMAIN,
    EVENT_NEW_TRANSACTION,
    STATS_WINDOW_DAYS,
)
from .api import FinTsAtruviaClient, InvalidUrlError, TanRequiredError
from .storage import (
    CredentialStoreError,
    FintsCredentialStore,
    FintsStateStore,
)

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_KEY_FMT = "fints_atruvia_seen_transactions_{entry_id}"


def _mask_iban_for_event(iban: str) -> str:
    """Return a masked IBAN suitable for event payloads."""
    clean = iban.replace(" ", "")
    if len(clean) < 8:
        return clean
    return f"{clean[:4]}{'*' * (len(clean) - 8)}{clean[-4:]}"


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
    income = Decimal(0)
    expense = Decimal(0)
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
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(hours=6),
        )
        data = config_entry.data
        credential_id = data.get(CONF_CREDENTIAL_ID)
        if not credential_id:
            # Cannot recover automatically: the migration must have failed
            # or the entry was created by an older code path. Raising here
            # surfaces to async_setup_entry and triggers a reauth prompt.
            raise ConfigEntryAuthFailed(
                "Credential reference missing — please re-authenticate."
            )
        self._credential_id: str = credential_id
        self._credential_store = FintsCredentialStore(hass, credential_id)
        self._state_store = FintsStateStore(hass, credential_id)
        self._client: FinTsAtruviaClient | None = None
        self._selected_accounts: list[str] = data.get(CONF_SELECTED_ACCOUNTS, [])
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
            private=True,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_init(self) -> None:
        """Decrypt credentials and build the FinTS client.

        Raises ConfigEntryAuthFailed on credential errors so HA opens a
        reauth flow instead of looping update failures.
        """
        data = self.config_entry.data
        try:
            creds = await self._credential_store.load()
        except CredentialStoreError as exc:
            raise ConfigEntryAuthFailed(
                "Credentials could not be loaded — please re-authenticate."
            ) from exc

        # PIN is held in this coordinator instance for the lifetime of the
        # config entry. python-fints captures it internally when the dialog
        # opens, so there is no way to drop it earlier without rebuilding
        # the client on every poll — at the cost of repeated SCA prompts.
        # The pin_provider indirection keeps the PIN out of FinTsAtruviaClient
        # construction args (which show up in tracebacks) and lets us wipe
        # ``_pin`` on unload.
        self._pin: str | None = creds["pin"]

        fints_state = await self._state_store.load()

        try:
            self._client = FinTsAtruviaClient(
                blz=data[CONF_BLZ],
                login=creds["username"],
                pin_provider=lambda: self._pin or "",
                url=data[CONF_URL],
                product_id=data.get(CONF_PRODUCT_ID) or None,
                fints_state=fints_state,
            )
        except InvalidUrlError as exc:
            raise ConfigEntryAuthFailed(f"Bank URL is invalid: {exc}") from exc

    async def async_shutdown(self) -> None:
        """
        Cancel scheduled refreshes, then wipe the in-memory PIN.

        Called from async_unload_entry.
        """
        await super().async_shutdown()
        self._pin = None
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Seen-transactions persistence
    # ------------------------------------------------------------------

    async def async_load_seen(self) -> None:
        """Load the persisted set of seen transaction hashes once at setup."""
        stored = await self._store.async_load()
        if stored is None:
            self._seen_initialised = False
            self._seen_hashes = {}
        else:
            self._seen_initialised = True
            self._seen_hashes = {iban: set(hashes) for iban, hashes in stored.items()}

    async def _async_save_seen(self) -> None:
        """Persist the current seen-hashes snapshot to .storage."""
        await self._store.async_save(
            {iban: sorted(hashes) for iban, hashes in self._seen_hashes.items()}
        )

    def _detect_new_transactions(
        self, iban: str, transactions: list[dict]
    ) -> tuple[list[dict], set[str]]:
        """Compare current transactions against last seen and return event payloads.

        Also returns the current snapshot of hashes for *iban* so callers can
        commit it to ``self._seen_hashes`` once the whole update has
        succeeded — hashes that fall out of the 30-day window are pruned at
        that point. This method itself does not mutate ``self._seen_hashes``,
        so a later account failing mid-update cannot cause events detected
        here to be silently dropped on the next poll.
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

        return events, set(current_by_hash.keys())

    @property
    def expose_full_data(self) -> bool:
        """Whether bank-controlled transaction texts (purpose, counterparty) are exposed.

        Default off — automations only see masked IBAN + amount + date + hash.
        Toggled via the per-entry Options-Flow.
        """
        return bool(self.config_entry.options.get(CONF_EXPOSE_FULL_DATA, False))

    def _build_event_payload(self, iban: str, txn: dict, txn_hash: str) -> dict:
        amount = txn.get("amount")
        # IBAN is masked in the event payload — automations can still
        # distinguish accounts via integration_id + iban_last4, and the
        # full IBAN never leaves the integration in plaintext.
        clean = iban.replace(" ", "")
        payload: dict = {
            "integration_id": self.config_entry.entry_id,
            "iban_masked": _mask_iban_for_event(iban),
            "iban_last4": clean[-4:] if len(clean) >= 4 else clean,
            "date": txn.get("date"),
            "amount": float(amount) if amount is not None else None,
            "currency": txn.get("currency"),
            "transaction_hash": txn_hash,
        }
        if self.expose_full_data:
            payload["purpose"] = txn.get("purpose")
            payload["applicant_name"] = txn.get("creditor")
        return payload

    async def _async_update_data(self) -> dict:
        """Fetch data for all selected accounts, compute stats, emit events."""
        result: dict = {}
        new_events: list[dict] = []
        pending_seen: dict[str, set[str]] = {}
        if self._client is None:
            raise ConfigEntryAuthFailed("FinTS client not initialised")

        try:
            accounts = await self.hass.async_add_executor_job(self._client.get_accounts)
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
                events, seen_snapshot = self._detect_new_transactions(
                    iban, transactions
                )
                new_events.extend(events)
                pending_seen[iban] = seen_snapshot

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
                raise ConfigEntryAuthFailed(
                    "Bank requires initial authentication (TAN)"
                ) from e
            # Intentionally return last known complete data rather than partial result.
            # Fresh data for already-processed IBANs is discarded to avoid partial state.
            return self.data
        except CredentialStoreError as err:
            # Decryption failed — typically because master key file was lost.
            # Surface via reauth so the user can re-enter the PIN.
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            # Bank responses may carry sensitive content in their string
            # form. Log only the exception type; the original exception is
            # still chained via ``raise from`` for debug-level traceback.
            _LOGGER.error("FinTS update failed: %s", type(err).__name__)
            raise UpdateFailed("Error communicating with bank") from err

        # Update succeeded for all accounts: mark seen-set as initialised, fire
        # events, and persist. Persistence happens after firing so the receiving
        # automations have observed the events when state is checkpointed.
        # ``pending_seen`` is only merged into ``self._seen_hashes`` here, once
        # every account in this update round has succeeded — an earlier
        # failure (TanRequiredError / UpdateFailed) must not advance the
        # seen-set for accounts that were already processed, or their events
        # would be lost forever on the next poll.
        was_uninitialised = not self._seen_initialised
        self._seen_initialised = True
        if not was_uninitialised:
            for payload in new_events:
                self.hass.bus.async_fire(EVENT_NEW_TRANSACTION, payload)
        self._seen_hashes.update(pending_seen)
        await self._async_save_seen()

        # Persist updated FinTS state (system_id, BPD/UPD) so future dialogs
        # don't need to re-sync — important for avoiding unnecessary SCA.
        await self._persist_fints_state()

        # Clear any pending 2FA state.
        self.is_2fa_pending = False
        self._tan_response = None
        pn_async_dismiss(self.hass, "fints_atruvia_reauth")
        return result

    async def _persist_fints_state(self) -> None:
        if self._client is None:
            return
        blob = await self.hass.async_add_executor_job(self._client.deconstruct)
        if blob is not None:
            await self._state_store.save(blob)

    async def async_complete_reauth(self, tan: str = "") -> None:
        """Complete pending re-authentication after the user confirms SecureGo+."""
        if self._tan_response is None or self._client is None:
            return
        await self.hass.async_add_executor_job(
            self._client.complete_tan, self._tan_response, tan
        )
        self._tan_response = None
        self.is_2fa_pending = False
        pn_async_dismiss(self.hass, "fints_atruvia_reauth")
        await self.async_request_refresh()
