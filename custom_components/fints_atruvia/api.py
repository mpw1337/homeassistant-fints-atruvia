"""FinTS API wrapper for Atruvia banks (Volksbank/Raiffeisenbank/PSD Bank).

This module provides a synchronous wrapper around the python-fints library.
All methods are blocking and intended to be called via hass.async_add_executor_job().

Security model
--------------
The PIN is supplied lazily via a ``pin_provider`` callable rather than stored
as an instance attribute. python-fints itself still holds the PIN internally
once a client is constructed (we can't change that), but our wrapper keeps
PIN exposure tightly scoped to the lifetime of an active FinTS dialog: when
``close()`` is called the underlying client is dropped and the only remaining
reference is the wrapper's bound provider (a closure that re-reads from the
encrypted store on demand).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.models import SEPAAccount

_LOGGER = logging.getLogger(__name__)


class TanRequiredError(Exception):
    """Raised when the bank returns a NeedTANResponse instead of data."""

    def __init__(self, response: NeedTANResponse) -> None:
        super().__init__("Bank requires TAN authentication")
        self.response = response


class InvalidUrlError(ValueError):
    """Raised when a FinTS endpoint URL is not an https:// URL."""


def _safe_decimal(value: Any) -> Decimal | None:
    """Best-effort conversion of *value* to Decimal, returning None on failure.

    Banks sometimes deliver optional HISAL fields as datagroups whose inner
    amount is an empty string or otherwise non-numeric. Falling back to None
    keeps the coordinator alive instead of crashing the whole update.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if text == "":
        return None
    try:
        return Decimal(text)
    except InvalidOperation, ValueError:
        return None


def _balance_to_signed_decimal(balance_obj: Any) -> Decimal | None:
    """Convert a Balance1/Balance2 object into a signed Decimal.

    Balance1 (HISAL5): credit_debit + direct ``amount`` (Decimal) + ``currency``.
    Balance2 (HISAL6/7): credit_debit + ``amount`` (Amount1 with .amount + .currency).
    """
    if balance_obj is None:
        return None

    amount_attr = getattr(balance_obj, "amount", None)
    if amount_attr is None:
        return None

    # Balance2 wraps amount in an Amount1 (which has its own .amount attribute).
    inner = amount_attr.amount if hasattr(amount_attr, "amount") else amount_attr
    raw = _safe_decimal(inner)
    if raw is None:
        return None

    cd_field = getattr(balance_obj, "credit_debit", None)
    cd_value = getattr(
        cd_field, "value", cd_field
    )  # CodeField has .value, may already be str
    return -raw if str(cd_value) == "D" else raw


def _amount_obj_to_decimal(amount_obj: Any) -> Decimal | None:
    """Extract the numeric value from an Amount1 datagroup."""
    if amount_obj is None:
        return None
    return _safe_decimal(getattr(amount_obj, "amount", None))


def _extract_booking_date(hisal_segment: Any) -> datetime.date | None:
    """Return the booking date from a HISAL segment.

    HISAL5 uses ``booking_date`` (a date), HISAL6/7 use ``booking_timestamp``
    (a Timestamp1 with ``.date``).
    """
    timestamp = getattr(hisal_segment, "booking_timestamp", None)
    if timestamp is not None and hasattr(timestamp, "date"):
        return timestamp.date
    return getattr(hisal_segment, "booking_date", None)


class _ExtendedFinTSClient(FinTS3PinTanClient):
    """python-fints subclass that returns the full HISAL segment from get_balance.

    The default ``_get_balance`` callback only extracts the booked balance via
    ``balance_booked.as_mt940_Balance()`` and discards the other HISAL fields
    (available amount, pending balance, credit line, etc.). We override it to
    return the raw segment so :class:`FinTsAtruviaClient.get_balance` can read
    the extended fields the bank delivers.
    """

    def _get_balance(self, command_seg, response):  # type: ignore[override]
        for resp in response.response_segments(command_seg, "HISAL"):
            return resp
        return None


# Fallback product_id: Atruvia sometimes blocks the python-fints default.
# This zero-padded ID is accepted by a number of Atruvia instances.
_FALLBACK_PRODUCT_ID = (
    "6151256F3D4F9975B877BD4A2"  # exactly 25 chars as required by FinTS
)


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
        pin_provider: Callable[[], str],
        url: str,
        product_id: str | None = None,
        fints_state: bytes | None = None,
    ) -> None:
        if not url.lower().startswith("https://"):
            raise InvalidUrlError(
                "FinTS endpoint URL must use https://. Plain http would "
                "leak the PIN in transit."
            )
        self._blz = blz
        self._login = login
        # Lazy callable rather than a stored string: lets the coordinator
        # decrypt the PIN on demand and keep it out of long-lived attributes.
        self._pin_provider = pin_provider
        self._url = url
        self._product_id = product_id or _FALLBACK_PRODUCT_ID
        self._fints_state = fints_state
        self._client: FinTS3PinTanClient | None = None
        self._cached_accounts: list[SEPAAccount] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_client(self) -> FinTS3PinTanClient:
        """Create a fresh FinTS3PinTanClient instance.

        The PIN is requested from the provider only here and immediately
        passed to python-fints. We don't retain it in our scope.
        """
        pin = self._pin_provider()
        try:
            return _ExtendedFinTSClient(
                bank_identifier=self._blz,
                user_id=self._login,
                pin=pin,
                server=self._url,
                product_id=self._product_id,
                from_data=self._fints_state,
            )
        finally:
            del pin

    def _get_client(self) -> FinTS3PinTanClient:
        """Return the active client, creating one if necessary."""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def deconstruct(self) -> bytes | None:
        """Return a serialised FinTS state blob for persistence.

        Per python-fints contract this blob contains system_id, BPD, UPD,
        and (with including_private=True) account numbers — but NOT the PIN.
        Restoring this state on the next connect avoids re-triggering SCA
        for every dialog.
        """
        if self._client is None:
            return None
        try:
            return self._client.deconstruct(including_private=True)
        except Exception:
            _LOGGER.debug("Failed to deconstruct FinTS state", exc_info=True)
            return None

    def close(self) -> None:
        """Drop the underlying client. Forces a fresh login (with PIN) next time."""
        self._close_standing_dialog()
        self._client = None
        self._cached_accounts = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def init_system_id(self) -> NeedTANResponse | None:
        """Initialise the FinTS system-ID / open the first dialog.

        Follows the python-fints quickstart pattern for PSD2/SCA banks:

        1. ``fetch_tan_mechanisms()`` to discover available TAN methods. For
           SCA-enforced Atruvia instances this raises ``ValueError`` (the
           bank withholds HISYN4 until SCA), but populates
           ``allowed_security_functions`` and the BPD as a side effect.
        2. Pick a two-step mechanism so the subsequent signed dialog uses
           SCA-compatible authentication.
        3. Pick a TAN medium if the bank requires one.
        4. Open a standing dialog. python-fints captures any SCA challenge
           in ``client.init_tan_response``.

        Returns ``None`` if no SCA was required (accounts cached, dialog
        closed) or a ``NeedTANResponse`` if SCA is pending. In the latter
        case the standing dialog stays open and the caller MUST call
        ``complete_tan()`` to finish setup.
        """
        client = self._build_client()
        self._client = client

        try:
            client.fetch_tan_mechanisms()
        except ValueError as exc:
            _LOGGER.debug(
                "fetch_tan_mechanisms raised ValueError (expected for SCA banks): %s",
                exc,
            )

        # If only the one-step mechanism is selected, upgrade to the first
        # available two-step mechanism so the signed dialog can perform SCA.
        if client.get_current_tan_mechanism() in (None, "999"):
            for sec_func in client.get_tan_mechanisms():
                if sec_func != "999":
                    client.set_tan_mechanism(sec_func)
                    break

        if client.selected_tan_medium is None and client.is_tan_media_required():
            _, media = client.get_tan_media()
            if media:
                client.set_tan_medium(media[0])
            else:
                # Workaround for banks that signal "no medium needed" via
                # empty list (see minimal_interactive_cli_bootstrap).
                client.selected_tan_medium = ""

        client.__enter__()
        keep_open = False
        try:
            if client.init_tan_response is not None:
                _LOGGER.debug(
                    "SCA challenge during system-ID init: %s",
                    client.init_tan_response,
                )
                keep_open = True
                return client.init_tan_response
            self._cached_accounts = client.get_sepa_accounts()
            return None
        finally:
            if not keep_open:
                self._close_standing_dialog()

    def _close_standing_dialog(self) -> None:
        """Close the client's standing dialog if one is open."""
        if self._client is None or self._client._standing_dialog is None:
            return
        try:
            self._client.__exit__(None, None, None)
        except Exception:
            _LOGGER.debug("Error closing FinTS dialog", exc_info=True)

    def complete_tan(self, tan_response: NeedTANResponse, tan: str = "") -> None:
        """Complete a pending two-factor authentication challenge.

        For push-TAN / SecureGo+ the user confirms in the banking app and
        *tan* stays empty string "".  For chip-TAN or SMS-TAN the user
        provides the numeric TAN string.

        :param tan_response: The NeedTANResponse object returned by a
                             previous call (init_system_id or a data fetch).
        :param tan: The TAN value, or "" for push/decoupled TANs.
        """
        client = self._get_client()
        try:
            client.send_tan(tan_response, tan)
            # After SCA, system-ID is assigned. Cache accounts while the
            # dialog is still open so the next caller does not re-open one.
            self._cached_accounts = client.get_sepa_accounts()
        finally:
            self._close_standing_dialog()

    def get_accounts(self) -> list[SEPAAccount]:
        """Return all SEPA accounts accessible with the configured credentials.

        :returns: List of SEPAAccount namedtuples (iban, bic, accountnumber,
                  subaccount, blz).
        """
        if self._cached_accounts is not None:
            return self._cached_accounts
        return self._get_client().get_sepa_accounts()

    def get_balance(self, account: SEPAAccount) -> dict:
        """Fetch the booked balance plus optional extended fields for a single account.

        Uses :class:`_ExtendedFinTSClient` to access the full HISAL segment.
        Fields that the bank does not deliver come back as ``None``.

        :param account: SEPAAccount as returned by get_accounts().
        :returns: Dict with the following keys:

            * ``balance`` (Decimal): signed booked balance
            * ``currency`` (str): ISO 4217 currency code
            * ``available_balance`` (Decimal | None): available amount incl. dispo
            * ``balance_pending`` (Decimal | None): signed balance incl. pending bookings
            * ``pending_amount`` (Decimal | None): difference (balance_pending - balance)
            * ``booking_date`` (datetime.date | None): booking date of the balance
        """
        segment = self._get_client().get_balance(account)
        if isinstance(segment, NeedTANResponse):
            raise TanRequiredError(segment)
        if segment is None:
            raise ValueError(f"No balance data returned for account {account.iban}")

        balance = _balance_to_signed_decimal(getattr(segment, "balance_booked", None))
        if balance is None:
            raise ValueError(
                f"No booked balance in HISAL response for account {account.iban}"
            )

        balance_pending = _balance_to_signed_decimal(
            getattr(segment, "balance_pending", None)
        )
        available_balance = _amount_obj_to_decimal(
            getattr(segment, "available_amount", None)
        )

        currency: str = getattr(segment, "currency", "") or ""
        if not currency:
            # Balance2 stores currency on the inner Amount1.
            booked = getattr(segment, "balance_booked", None)
            inner_amount = (
                getattr(booked, "amount", None) if booked is not None else None
            )
            currency = getattr(inner_amount, "currency", "") or ""

        pending_amount = (
            balance_pending - balance
            if balance_pending is not None and balance is not None
            else None
        )

        return {
            "balance": balance,
            "currency": currency,
            "available_balance": available_balance,
            "balance_pending": balance_pending,
            "pending_amount": pending_amount,
            "booking_date": _extract_booking_date(segment),
        }

    def get_transactions(self, account: SEPAAccount, days: int = 30) -> list[dict]:
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

        raw = self._get_client().get_transactions(
            account,
            start_date=start_date,
            end_date=end_date,
        )
        if isinstance(raw, NeedTANResponse):
            raise TanRequiredError(raw)

        result: list[dict] = []
        for txn in raw:
            # Both mt940.models.Transaction and fints.models.Transaction
            # expose their data via a .data dict.
            if not hasattr(txn, "data"):
                # Avoid %r on the transaction object — its repr may include
                # purpose / counterparty text that we don't want in logs.
                _LOGGER.warning("Skipping malformed transaction (no .data attribute)")
                continue

            data: dict = txn.data

            amount_obj = data.get("amount")
            if amount_obj is not None:
                # mt940 Amount object: .amount (Decimal), .currency (str).
                # Use _safe_decimal so empty/garbled amounts fall back to 0
                # instead of contaminating the transactions list with strings.
                amount_value = _safe_decimal(
                    getattr(amount_obj, "amount", None)
                ) or Decimal(0)
                currency_value: str = getattr(amount_obj, "currency", "") or ""
            else:
                amount_value = Decimal(0)
                currency_value = ""

            # transaction_details holds the structured purpose/reference text
            transaction_details = data.get("transaction_details", "") or ""

            # The counterpart name is stored under "applicant_name" in mt940
            applicant_name = data.get("applicant_name", "") or ""

            raw_date = data.get("date")
            result.append(
                {
                    "date": raw_date.isoformat() if raw_date else None,
                    "amount": amount_value,
                    "currency": currency_value,
                    "purpose": str(transaction_details).strip(),
                    "creditor": str(applicant_name).strip(),
                }
            )

        return result
