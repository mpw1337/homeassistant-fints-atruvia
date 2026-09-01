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
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from fints.client import SYSTEM_ID_UNASSIGNED, FinTS3PinTanClient, NeedTANResponse
from fints.exceptions import FinTSClientPINError, FinTSClientTemporaryAuthError
from fints.formals import CUSTOMER_ID_ANONYMOUS, SynchronizationMode
from fints.segments.dialog import HISYN4, HKSYN3

if TYPE_CHECKING:
    from collections.abc import Callable

    from fints.models import SEPAAccount

_LOGGER = logging.getLogger(__name__)

_MSG_INSECURE_URL = (
    "FinTS endpoint URL must use https://. Plain http would leak the PIN in transit."
)
_MSG_NO_TAN_MECHANISM = "Bank offered no supported two-step TAN mechanism"
_MSG_NO_SYSTEM_ID = "Bank did not assign a system id after the TAN was confirmed"


class TanRequiredError(Exception):
    """Raised when the bank returns a NeedTANResponse instead of data."""

    def __init__(self, response: NeedTANResponse) -> None:
        """Wrap the bank's TAN challenge so callers can complete it later."""
        super().__init__("Bank requires TAN authentication")
        self.response = response


class InvalidUrlError(ValueError):
    """Raised when a FinTS endpoint URL is not an https:// URL."""


class NoTanMechanismError(Exception):
    """Raised when the bank offers no supported two-step TAN mechanism."""

    def __init__(
        self, message: str = _MSG_NO_TAN_MECHANISM, *, codes: tuple[str, ...] = ()
    ) -> None:
        """Store the bank's numeric response codes observed during bootstrap."""
        super().__init__(message)
        self.codes = codes


class AuthRejectedError(Exception):
    """Raised when the bank rejects authentication during a FinTS dialog."""

    def __init__(self, codes: tuple[str, ...]) -> None:
        """Store the bank's numeric response codes for diagnostics."""
        super().__init__("Bank rejected authentication during dialog initialization")
        self.codes = codes


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


# Only the last four IBAN digits go into error messages. Home Assistant logs
# the __cause__ chain of a failed coordinator update on two loggers at DEBUG
# (helpers/update_coordinator.py and config_entries.py), so anything in a
# raised message here can end up in a home-assistant.log that users paste into
# public issues. Four digits are enough to tell two accounts apart, which is
# the only reason the account is named at all — do not widen this back to the
# full IBAN. This module deliberately imports nothing from homeassistant or
# from the rest of the package (see the module docstring), so the coordinator's
# masking helper is out of reach; the ``…{last4}`` form matches the convention
# the config-flow account picker already uses.
_IBAN_LAST4_LEN = 4
_ACCOUNT_REF_UNKNOWN = "…?"


def _account_ref(account: SEPAAccount) -> str:
    """Return a short non-identifying reference to *account* for error messages."""
    iban = getattr(account, "iban", None) or ""
    if len(iban) < _IBAN_LAST4_LEN:
        # Too short to be an IBAN: echoing it would neither help nor be
        # honest about being a last-four.
        return _ACCOUNT_REF_UNKNOWN
    return f"…{iban[-_IBAN_LAST4_LEN:]}"


def _hitans_versions(client: FinTS3PinTanClient) -> list[int]:
    """Return the sorted HITANS (TAN mechanism parameter) versions in the BPD.

    Diagnostic-only: distinguishes "bank withheld BPD entirely" from "bank
    offered only HITANS versions this integration doesn't support" in the
    no-tan-mechanism debug log. Defensive because the BPD may be empty (no
    dialog has completed yet) or, for an unusual client implementation,
    ``bpd``/``find_segments``/``header`` may not behave as expected — a
    diagnostics-only log line must never crash the guard it's inside of.
    Versions are bare integers (log-hygiene-safe).
    """
    try:
        segments = client.bpd.find_segments("HITANS")
        return sorted({seg.header.version for seg in segments})
    except Exception:  # noqa: BLE001 - diagnostics-only, must never raise
        return []


# Longest exception chain worth printing: wrapper -> python-fints error ->
# blocked-PIN Exception -> original response error is four; six leaves room.
_CHAIN_MAX_DEPTH = 6


def describe_exception_chain(exc: BaseException) -> str:
    """Return the exception type names along ``__cause__`` / ``__context__``.

    python-fints wraps unexpected errors in ``FinTSDialogInitError`` and, once
    it has blocked its ``Password`` object after a PIN-error code, replaces the
    ``FinTSClientPINError`` with a bare ``Exception("Refusing to use PIN after
    block")`` while ending the dialog. The outermost type therefore says
    nothing about what happened; the chain does. Type names only — messages
    may quote bank responses (log hygiene, SECURITY.md §10). ``__cause__``
    wins over ``__context__`` (explicit ``raise ... from``), the walk is
    cycle-safe and capped at ``_CHAIN_MAX_DEPTH`` entries.
    """
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if len(names) >= _CHAIN_MAX_DEPTH:
            break
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(names)


class _ExtendedFinTSClient(FinTS3PinTanClient):
    """python-fints subclass that returns the full HISAL segment from get_balance.

    The default ``_get_balance`` callback only extracts the booked balance via
    ``balance_booked.as_mt940_Balance()`` and discards the other HISAL fields
    (available amount, pending balance, credit line, etc.). We override it to
    return the raw segment so :class:`FinTsAtruviaClient.get_balance` can read
    the extended fields the bank delivers.

    It also records the numeric response codes seen during dialog
    bootstrapping (see ``_process_response``) so a rejected authentication
    or a failed TAN-mechanism negotiation can be diagnosed after the fact:
    the bootstrap dialogs run with ``internal_send=True``, so python-fints
    itself never logs them, and the static ``FinTSClientPINError`` message
    that follows a 9xxx code carries no code of its own.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise diagnostic attributes before python-fints can touch them.

        These must be instance attributes set before ``super().__init__``
        runs, since dialog bootstrapping inside the base constructor can
        already trigger ``_process_response`` calls. A class-attribute list
        would be shared across instances/clients.
        """
        self.observed_error_codes: list[str] = []
        self.observed_codes: list[str] = []
        super().__init__(*args, **kwargs)

    def _get_balance(self, command_seg: Any, response: Any) -> Any:  # type: ignore[override]
        for resp in response.response_segments(command_seg, "HISAL"):
            return resp
        return None

    def _process_response(self, dialog: Any, segment: Any, response: Any) -> None:
        """Record diagnostic response codes before delegating to python-fints.

        Recording must happen before the ``super()`` call because that call
        can raise (e.g. ``FinTSClientPINError`` on a 9xxx code) — the codes
        need to survive the raise so callers can attach them to
        ``AuthRejectedError`` or ``NoTanMechanismError``. Only the bare
        numeric code is kept, never ``response.text`` or the response object
        itself (log hygiene: HBCI response text can include account
        numbers).
        """
        code = str(response.code)
        if code not in self.observed_codes:
            self.observed_codes.append(code)
        if code.startswith("9"):
            self.observed_error_codes.append(code)
        super()._process_response(dialog, segment, response)


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

    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per FinTS connection field
        self,
        blz: str,
        login: str,
        pin_provider: Callable[[], str],
        url: str,
        product_id: str | None = None,
        fints_state: bytes | None = None,
    ) -> None:
        """Store the connection parameters; no bank dialog is opened yet."""
        if not url.lower().startswith("https://"):
            raise InvalidUrlError(_MSG_INSECURE_URL)
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
            blob = self._client.deconstruct(including_private=True)
        except Exception:
            _LOGGER.debug("Failed to deconstruct FinTS state", exc_info=True)
            return None
        # A client rebuilt after close() (the coordinator does that after a
        # failed poll) must start from this state, not from the blob loaded
        # at construction.
        self._fints_state = blob
        return blob

    def close(self) -> None:
        """Drop the underlying client. Forces a fresh login (with PIN) next time."""
        self._close_standing_dialog()
        self._client = None
        self._cached_accounts = None

    def _load_bpd_anonymously(self, client: FinTS3PinTanClient) -> None:
        """Fetch the bank parameters (BPD) through an anonymous dialog.

        Atruvia sends the BPD only once a login dialog succeeded, but a
        two-step login dialog cannot be built without the BPD's HITANS
        segments (python-fints needs them to act on the bank's 3920 list of
        permitted mechanisms). The anonymous dialog exists for exactly this:
        no credentials, public parameters only. Best effort — on failure the
        login proceeds with an empty BPD, as it always did.
        """
        if client.bpd.segments:
            return
        anonymous = FinTS3PinTanClient(
            self._blz,
            CUSTOMER_ID_ANONYMOUS,
            None,
            self._url,
            product_id=self._product_id,
        )
        try:
            with anonymous:
                pass
        except Exception as exc:  # noqa: BLE001 - best effort, see docstring
            _LOGGER.debug("Anonymous BPD fetch failed: %s", type(exc).__name__)
            return
        client.bpd = anonymous.bpd
        client.bpa = anonymous.bpa
        client.bpd_version = anonymous.bpd_version

    @staticmethod
    def _sync(client: FinTS3PinTanClient) -> NeedTANResponse | None:
        """Run one HKSYN dialog and adopt the system id if the bank sends it.

        Returns the SCA challenge if the bank demands one for the sync — the
        dialog then stays open as the client's standing dialog so
        ``send_tan`` continues it — and ``None`` otherwise (dialog ended).
        python-fints' own ``_ensure_system_id`` is unusable for SCA banks: it
        ends the dialog before anyone could answer the challenge. A rejected
        sync is dropped without HKEND — the bank has aborted it already.
        """
        client.init_tan_response = None
        # No public API opens a dialog with an extra HKSYN segment.
        dialog = client._new_dialog(lazy_init=True)  # noqa: SLF001
        response = dialog.init(HKSYN3(SynchronizationMode.NEW_SYSTEM_ID))
        seg = response.find_segment_first(HISYN4)
        if seg is not None:
            client.system_id = seg.system_id
        if client.init_tan_response is not None:
            dialog.__enter__()
            client._standing_dialog = dialog  # noqa: SLF001 - see above
            return client.init_tan_response
        if seg is None:
            # Rejected sync (9075): the bank has already aborted this dialog
            # (9800). An HKEND is refused with 9340, which python-fints lists
            # among the PIN-error codes — it blocks its Password object and
            # every later signature dies with "Refusing to use PIN after
            # block" (surfacing as FinTSDialogInitError from the next
            # dialog.init). Drop the dialog like python-fints does for a 9075
            # on its standing dialog.
            dialog.open = False
            return None
        try:
            dialog.end()
        except Exception:
            _LOGGER.debug("Ending the sync dialog failed", exc_info=True)
        return None

    def _open_dialog_and_cache_accounts(
        self, client: FinTS3PinTanClient
    ) -> NeedTANResponse | None:
        """Open the standing dialog and cache the accounts, unless SCA comes first.

        Returns the challenge (dialog left open) or ``None`` (dialog closed).
        """
        client.init_tan_response = None
        client.__enter__()
        if client.init_tan_response is not None:
            return client.init_tan_response
        try:
            self._cached_accounts = client.get_sepa_accounts()
        finally:
            self._close_standing_dialog()
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def init_system_id(self) -> NeedTANResponse | None:
        """Open the first bank dialog for a login without a stored FinTS state.

        A fresh client has no system id, so the bank must assign one (HKSYN).
        Atruvia does that in one of two ways:

        * SCA-exempt logins (response code 3076): the plain one-step sync
          succeeds and HISYN carries the system id straight away.
        * Everyone else: the one-step sync is rejected (9075), response code
          3920 names the permitted two-step mechanisms, and the bank sends
          *neither* HISYN nor the BPD. python-fints selects the first
          supported mechanism from 3920 — provided the BPD is already loaded,
          hence :meth:`_load_bpd_anonymously` first. The sync is repeated
          two-step, which yields the SecureGo+ challenge; the system id then
          arrives with the TAN completion (see :meth:`complete_tan`).

        Returns ``None`` when no SCA was required (accounts cached, dialog
        closed) or the ``NeedTANResponse`` to show the user. In the latter
        case the dialog stays open and the caller MUST call
        :meth:`complete_tan` next.

        Raises ``NoTanMechanismError`` (carrying every response code observed
        so far) if the sync was rejected without a supported two-step
        mechanism to fall back to, and ``AuthRejectedError`` if the bank
        rejected the login itself.
        """
        client = self._build_client()
        self._client = client
        try:
            challenge = None
            if client.system_id == SYSTEM_ID_UNASSIGNED:
                self._load_bpd_anonymously(client)
                challenge = self._sync(client)
                if challenge is None and client.system_id == SYSTEM_ID_UNASSIGNED:
                    if client.get_current_tan_mechanism() in (None, "999"):
                        _LOGGER.debug(
                            "Sync rejected and no two-step TAN mechanism negotiable: "
                            "%d allowed security functions, HITANS versions in "
                            "BPD: %s",
                            len(getattr(client, "allowed_security_functions", ())),
                            _hitans_versions(client),
                        )
                        raise NoTanMechanismError(
                            codes=tuple(getattr(client, "observed_codes", ()))
                        )
                    challenge = self._sync(client)
            if challenge is not None:
                return challenge
            return self._open_dialog_and_cache_accounts(client)
        except (FinTSClientPINError, FinTSClientTemporaryAuthError) as exc:
            self._close_standing_dialog()
            raise AuthRejectedError(
                codes=tuple(getattr(client, "observed_error_codes", ()))
            ) from exc

    def _close_standing_dialog(self) -> None:
        """Close the client's standing dialog if one is open."""
        # python-fints exposes no public accessor for the standing dialog;
        # reading the private attribute is the only way to tell whether
        # __exit__ has anything to close.
        if self._client is None or self._client._standing_dialog is None:  # noqa: SLF001
            return
        try:
            self._client.__exit__(None, None, None)
        except Exception:
            _LOGGER.debug("Error closing FinTS dialog", exc_info=True)

    @staticmethod
    def _adopt_system_id(client: FinTS3PinTanClient, response: Any) -> None:
        """Take the system id from the sync dialog's TAN-completion response."""
        seg = response.find_segment_first(HISYN4)
        if seg is None:
            raise ValueError(_MSG_NO_SYSTEM_ID)
        client.system_id = seg.system_id

    def complete_tan(
        self, tan_response: NeedTANResponse, tan: str = ""
    ) -> NeedTANResponse | None:
        """Answer a pending SCA challenge and finish what it interrupted.

        For SecureGo+ (decoupled) the user confirms in the app and *tan* stays
        ``""``; chipTAN / SMS-TAN users pass the TAN string.

        Returns ``None`` once the login is complete (accounts cached, dialog
        closed). Returns a new ``NeedTANResponse`` when more confirmation is
        needed: the bank has not seen the app confirmation yet (response code
        3956), or — after a sync dialog — the fresh dialog asks for SCA
        again. The caller then shows the prompt again and calls this method
        once more.

        Raises ``AuthRejectedError`` if the bank rejects the authentication.
        """
        client = self._get_client()
        try:
            result = client.send_tan(tan_response, tan)
            if isinstance(result, NeedTANResponse):
                return result
            client.init_tan_response = None
            if client.system_id != SYSTEM_ID_UNASSIGNED:
                self._cached_accounts = client.get_sepa_accounts()
                self._close_standing_dialog()
                return None
            # Sync dialog: Atruvia hands out the system id only with the TAN
            # completion. A sync dialog is not meant to carry business jobs,
            # so end it and open a regular one for the account list.
            self._close_standing_dialog()
            self._adopt_system_id(client, result)
            return self._open_dialog_and_cache_accounts(client)
        except (FinTSClientPINError, FinTSClientTemporaryAuthError) as exc:
            self._close_standing_dialog()
            raise AuthRejectedError(
                codes=tuple(getattr(client, "observed_error_codes", ()))
            ) from exc
        except Exception:
            self._close_standing_dialog()
            raise

    def open_dialog(self) -> NeedTANResponse | None:
        """Open the standing dialog for a poll, or report the SCA it demands.

        Idempotent: a dialog left open by :meth:`complete_tan` is reused so
        the fetch after a confirmed challenge runs in the dialog the user
        authenticated — a fresh one could demand SCA all over again.

        Returns the challenge if the bank asks for SCA at dialog
        initialisation (response 3955/0030 to the HKTAN sent with HKIDN);
        the dialog then stays open and the caller MUST answer it through
        :meth:`complete_tan` before any job. python-fints itself only records
        that challenge in ``init_tan_response`` and would send the jobs
        anyway; Atruvia refuses them (9340), which python-fints then mistakes
        for a wrong PIN. Returns ``None`` when the dialog is open and
        authenticated.

        Raises ``AuthRejectedError`` if the bank rejects the login. The
        python-fints client is dropped first: it has blocked its PIN object,
        so the next attempt needs a fresh one (see :meth:`close`).
        """
        client = self._get_client()
        if client._standing_dialog is not None:  # noqa: SLF001 - no public accessor
            return None
        client.init_tan_response = None
        try:
            client.__enter__()
        except (FinTSClientPINError, FinTSClientTemporaryAuthError) as exc:
            codes = tuple(getattr(client, "observed_error_codes", ()))
            self.close()
            raise AuthRejectedError(codes=codes) from exc
        return client.init_tan_response

    def close_dialog(self) -> None:
        """End the standing dialog (HKEND) if one is open; the client stays usable."""
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
            * ``available_balance`` (Decimal | None): available amount incl.
              dispo
            * ``balance_pending`` (Decimal | None): signed balance incl.
              pending bookings
            * ``pending_amount`` (Decimal | None): difference
              (balance_pending - balance)
            * ``booking_date`` (datetime.date | None): booking date of the
              balance
        """
        segment = self._get_client().get_balance(account)
        if isinstance(segment, NeedTANResponse):
            raise TanRequiredError(segment)
        if segment is None:
            msg = f"No balance data returned for account {_account_ref(account)}"
            raise ValueError(msg)

        balance = _balance_to_signed_decimal(getattr(segment, "balance_booked", None))
        if balance is None:
            msg = (
                "No booked balance in HISAL response for account "
                f"{_account_ref(account)}"
            )
            raise ValueError(msg)

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

        The window is *days* calendar days counted back from today in the
        host's local timezone, which for a self-hosted Home Assistant is also
        the bank's — this module deliberately imports nothing from
        ``homeassistant`` (see the module docstring), so it cannot consult
        HA's configured timezone.

        :param account: SEPAAccount as returned by get_accounts().
        :param days: Number of calendar days to look back (default 30).
        :returns: List of dicts with keys:
                  - date (datetime.date): booking date
                  - amount (Decimal): transaction amount (negative = debit)
                  - currency (str): ISO 4217 currency code
                  - purpose (str): payment purpose / reference text
                  - creditor (str): counterpart name (may be empty string)
        """
        # Local calendar date on purpose, see the docstring: a timezone-aware
        # "today" would require homeassistant.util.dt and break the layering
        # that keeps this blocking wrapper independently testable.
        end_date = datetime.date.today()  # noqa: DTZ011
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
