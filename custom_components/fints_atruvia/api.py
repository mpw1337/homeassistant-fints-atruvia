"""FinTS API wrapper for Atruvia banks (Volksbank/Raiffeisenbank/PSD Bank).

This module provides a synchronous wrapper around the python-fints library.
All methods are blocking and intended to be called via hass.async_add_executor_job().
"""
from __future__ import annotations

import datetime
import logging
from decimal import Decimal

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.models import SEPAAccount

_LOGGER = logging.getLogger(__name__)

# Fallback product_id: Atruvia sometimes blocks the python-fints default.
# This zero-padded ID is accepted by a number of Atruvia instances.
_FALLBACK_PRODUCT_ID = "00000000000000000000000000001"


class FinTsAtruviaClient:
    """Synchronous FinTS client tailored for Atruvia-hosted banks.

    Atruvia is the IT service provider for most German cooperative banks
    (Volksbank, Raiffeisenbank, PSD Bank). Their FinTS gateway has a few
    quirks that this wrapper handles:

    * The product_id (Registrierungsnummer) must be a known/accepted value.
    * System-ID synchronisation during the first dialog open may raise
      NeedTANResponse (SecureGo+/pushTAN) or ValueError ('Could not find
      system_id') and needs special handling.
    """

    def __init__(
        self,
        blz: str,
        login: str,
        pin: str,
        url: str,
        product_id: str | None = None,
    ) -> None:
        self._blz = blz
        self._login = login
        self._pin = pin
        self._url = url
        self._product_id = product_id or _FALLBACK_PRODUCT_ID
        self._client: FinTS3PinTanClient | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_client(self) -> FinTS3PinTanClient:
        """Create a fresh FinTS3PinTanClient instance."""
        return FinTS3PinTanClient(
            bank_identifier=self._blz,
            user_id=self._login,
            pin=self._pin,
            server=self._url,
            product_id=self._product_id,
        )

    @property
    def client(self) -> FinTS3PinTanClient:
        """Return the active client, creating one if necessary."""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def init_system_id(self) -> NeedTANResponse | None:
        """Initialise the FinTS system-ID / open the first dialog.

        The first real dialog with Atruvia triggers system-ID synchronisation
        (HKSYN). Depending on the bank's TAN method this may:

        * Succeed silently  → returns None
        * Require a TAN     → returns the NeedTANResponse (caller must call
                              complete_tan() afterwards)

        A ValueError ('Could not find system_id') is retried once because
        some Atruvia instances need a second attempt after the TAN mechanism
        has been negotiated.
        """
        self._client = self._build_client()

        def _try_init() -> NeedTANResponse | None:
            try:
                # get_sepa_accounts() is the lightest call that forces a full
                # dialog open including system-ID synchronisation.
                self._client.get_sepa_accounts()
                return None
            except NeedTANResponse as exc:
                _LOGGER.debug("TAN required during system-ID init: %s", exc)
                return exc

        try:
            return _try_init()
        except ValueError as exc:
            _LOGGER.warning(
                "ValueError during system-ID init, retrying once: %s", exc
            )
            self._client = self._build_client()
            return _try_init()

    def complete_tan(self, tan_response: NeedTANResponse, tan: str = "") -> None:
        """Complete a pending two-factor authentication challenge.

        For push-TAN / SecureGo+ the user confirms in the banking app and
        *tan* stays empty string "".  For chip-TAN or SMS-TAN the user
        provides the numeric TAN string.

        :param tan_response: The NeedTANResponse object returned by a
                             previous call (init_system_id or a data fetch).
        :param tan: The TAN value, or "" for push/decoupled TANs.
        """
        self.client.send_tan(tan_response, tan)

    def get_accounts(self) -> list[SEPAAccount]:
        """Return all SEPA accounts accessible with the configured credentials.

        :returns: List of SEPAAccount namedtuples (iban, bic, accountnumber,
                  subaccount, blz).
        """
        return self.client.get_sepa_accounts()

    def get_balance(self, account: SEPAAccount) -> tuple[Decimal, str]:
        """Fetch the current booked balance for a single account.

        :param account: SEPAAccount as returned by get_accounts().
        :returns: Tuple of (amount: Decimal, currency: str), e.g.
                  (Decimal('1234.56'), 'EUR').
        """
        balance = self.client.get_balance(account)
        # get_balance() returns an mt940.models.Balance object whose .amount
        # attribute is an mt940.models.Amount with .amount (Decimal) and
        # .currency (str).
        amount: Decimal = balance.amount.amount
        currency: str = balance.amount.currency
        return amount, currency

    def get_transactions(
        self, account: SEPAAccount, days: int = 30
    ) -> list[dict]:
        """Fetch recent transactions for a single account.

        :param account: SEPAAccount as returned by get_accounts().
        :param days: Number of calendar days to look back (default 30).
        :returns: List of dicts with keys:
                  - date (datetime.date): booking date
                  - amount (Decimal): transaction amount (negative = debit)
                  - currency (str): ISO 4217 currency code
                  - purpose (str): payment purpose / reference text
                  - creditor (str): counterpart name (may be empty string)
        """
        end_date = datetime.date.today()
        start_date = end_date - datetime.timedelta(days=days)

        raw = self.client.get_transactions(
            account,
            start_date=start_date,
            end_date=end_date,
        )

        result: list[dict] = []
        for txn in raw:
            # Both mt940.models.Transaction and fints.models.Transaction
            # expose their data via a .data dict.
            data: dict = txn.data if hasattr(txn, "data") else {}

            amount_obj = data.get("amount")
            if amount_obj is not None:
                # mt940 Amount object: .amount (Decimal), .currency (str)
                amount_value: Decimal = getattr(amount_obj, "amount", Decimal("0"))
                currency_value: str = getattr(amount_obj, "currency", "")
            else:
                amount_value = Decimal("0")
                currency_value = ""

            # transaction_details holds the structured purpose/reference text
            transaction_details = data.get("transaction_details", "") or ""

            # The counterpart name is stored under "applicant_name" in mt940
            applicant_name = data.get("applicant_name", "") or ""

            result.append(
                {
                    "date": data.get("date"),
                    "amount": amount_value,
                    "currency": currency_value,
                    "purpose": str(transaction_details).strip(),
                    "creditor": str(applicant_name).strip(),
                }
            )

        return result
