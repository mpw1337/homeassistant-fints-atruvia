"""Offline fake bank for fints_atruvia verification runs.

Replaces ``fints.client.FinTS3PinTanClient`` at the library boundary, so
``custom_components/fints_atruvia/api.py`` still subclasses it and parses the
HISAL / MT940-shaped objects unchanged. No socket is opened to a real bank and
no real credentials are involved.

Activated by putting this directory on PYTHONPATH (Python auto-imports
``sitecustomize``) together with ``FAKE_FINTS=1``.

Environment:
  FAKE_FINTS=1          enable the patch
  FAKE_FINTS_LOG=PATH   append a line per bank connect / TAN submission
  FAKE_FINTS_FLAGDIR=D  runtime switches (create/remove files, no restart):
                          D/extra1, D/extra2  extra bookings -> new-txn events
                          D/xss               hostile purpose/creditor text
                          D/tanmode           bank demands SCA on every call
                          D/tanmode2          SCA for the *second* account only
                          D/nohisal           get_balance answers without a HISAL
                          D/nobooked          HISAL without a booked balance
                          D/nomech            bank offers no two-step TAN mechanism (empty BPD/3920)
  FAKE_FINTS_TAN=1      same as the tanmode flag, from startup
"""
import datetime
import os
from decimal import Decimal

try:
    import fints.client as _fc
    from fints.models import SEPAAccount
except ImportError:  # not a HA process
    _fc = None

IBAN = "DE89370400440532013000"   # selected in the sandbox entry
IBAN2 = "DE02120300000000202051"  # exists at the bank, must NOT be exposed


def _flag(name):
    d = os.environ.get("FAKE_FINTS_FLAGDIR")
    return bool(d and os.path.exists(os.path.join(d, name)))


def _tan_mode():
    return os.environ.get("FAKE_FINTS_TAN") == "1" or _flag("tanmode")


def _tan_mode_for(account):
    """SCA decision for one account.

    ``tanmode`` fails every call. ``tanmode2`` fails only IBAN2, which is the
    only way to make a multi-account poll fail *after* the first account was
    already processed — needed to verify that the coordinator does not commit
    seen-transaction hashes for accounts handled earlier in a poll that then
    aborts.
    """
    if _tan_mode():
        return True
    return _flag("tanmode2") and getattr(account, "iban", None) == IBAN2


def _log(line):
    with open(os.environ.get("FAKE_FINTS_LOG", "/dev/null"), "a") as fh:
        fh.write(line + "\n")


class _Amount:
    def __init__(self, amount, currency="EUR"):
        self.amount = Decimal(str(amount))
        self.currency = currency


class _CodeField:
    def __init__(self, value):
        self.value = value


class _Balance:
    """Shaped like fints Balance2 (HISAL6/7)."""

    def __init__(self, amount, credit_debit="C"):
        self.credit_debit = _CodeField(credit_debit)
        self.amount = _Amount(abs(Decimal(str(amount))))


class _Timestamp:
    def __init__(self, date):
        self.date = date


class _Hisal:
    def __init__(self):
        self.balance_booked = _Balance("1234.56", "C")
        self.balance_pending = _Balance("1200.06", "C")
        self.available_amount = _Amount("2234.56")
        self.currency = ""  # forces the Balance2 inner-currency fallback path
        self.booking_timestamp = _Timestamp(datetime.date.today())


class _Txn:
    def __init__(self, day_offset, amount, purpose, applicant):
        self.data = {
            "date": datetime.date.today() - datetime.timedelta(days=day_offset),
            "amount": _Amount(amount),
            "transaction_details": purpose,
            "applicant_name": applicant,
        }


def _transactions():
    # income 2500.00 / expense 150.99 / count 4 with no flags set
    txns = [
        _Txn(2, "-42.99", "SANDBOX-PURPOSE-GROCERIES-4711", "REWE Markt GmbH"),
        _Txn(5, "-19.90", "SANDBOX-PURPOSE-STREAMING-ABO", "Netflix International"),
        _Txn(9, "2500.00", "SANDBOX-PURPOSE-GEHALT-JULI", "Muster GmbH"),
        _Txn(20, "-88.10", "SANDBOX-PURPOSE-STROMABSCHLAG", "Stadtwerke Sandbox"),
    ]
    if _flag("extra1"):
        txns.append(_Txn(0, "-7.35", "SANDBOX-PURPOSE-BAECKEREI-NEU", "Bäckerei Neu"))
    if _flag("extra2"):
        txns.append(_Txn(0, "-13.37", "SANDBOX-PURPOSE-ZWEITE-NEUE", "Kiosk Zwei"))
    if _flag("xss"):
        txns.append(_Txn(
            1, "-1.23",
            '<img src=x onerror="window.__XSS__=1">PWNED',
            "<script>window.__XSS2__=1</script>Evil GmbH",
        ))
    return txns


class FakeFinTS3PinTanClient:
    """Stand-in for fints.client.FinTS3PinTanClient."""

    def __init__(self, bank_identifier=None, user_id=None, pin=None, server=None,
                 product_id=None, from_data=None, **kwargs):
        self.bank_identifier = bank_identifier
        self.user_id = user_id
        self.server = server
        self.product_id = product_id
        self.from_data = from_data
        self._standing_dialog = None
        self.init_tan_response = None
        self.selected_tan_medium = None
        self._mech = "999"
        # pin_len only — never log the PIN itself, not even a fake one
        _log(f"CONNECT server={server} user={user_id} blz={bank_identifier} "
             f"product={product_id} pin_len={len(pin) if pin else 0} "
             f"restored_state={'yes' if from_data else 'no'}")

    # --- dialog / TAN plumbing ------------------------------------------
    def fetch_tan_mechanisms(self):
        return {} if _flag("nomech") else {"942": "SecureGo plus"}

    def get_tan_mechanisms(self):
        return {} if _flag("nomech") else {"942": "SecureGo plus"}

    def get_current_tan_mechanism(self):
        return self._mech

    def set_tan_mechanism(self, mech):
        self._mech = mech

    def is_tan_media_required(self):
        # Mirrors fints/client.py: looks the current mechanism up in the BPD
        # segment 3920 (here, get_tan_mechanisms()) before answering. Under
        # ``nomech`` with ``_mech`` still "999" this raises KeyError('999')
        # exactly like the real library — the Task-1 guard in api.py must
        # keep this line from ever being reached.
        mech = self.get_tan_mechanisms()[self._mech]
        return False

    def get_tan_media(self, *a, **kw):
        return None, []

    def __enter__(self):
        self._standing_dialog = object()
        return self

    def __exit__(self, *exc):
        self._standing_dialog = None
        return False

    def send_tan(self, response, tan=""):
        _log(f"SEND_TAN tan={'<empty>' if tan == '' else '<value>'}")

    def deconstruct(self, including_private=False):
        return b"FAKE-FINTS-STATE-BLOB-system_id=SANDBOX123"

    # --- data -----------------------------------------------------------
    def _need_tan(self):
        r = _fc.NeedTANResponse.__new__(_fc.NeedTANResponse)
        r.challenge = "Bitte in SecureGo plus bestaetigen"
        return r

    def get_sepa_accounts(self):
        return [
            SEPAAccount(iban=IBAN, bic="GENODEF1S02", accountnumber="0000123456",
                        subaccount=None, blz="99999999"),
            SEPAAccount(iban=IBAN2, bic="GENODEF1S02", accountnumber="0000654321",
                        subaccount=None, blz="99999999"),
        ]

    def get_balance(self, account):
        """Answer a balance query, or one of the two shapes api.py rejects.

        ``nohisal`` drops the HISAL segment entirely and ``nobooked`` keeps it
        but without a booked balance — the two cases behind the ValueErrors in
        ``FinTsAtruviaClient.get_balance``, which are otherwise unreachable
        from the outside.
        """
        if _tan_mode_for(account):
            return self._need_tan()
        if _flag("nohisal"):
            return None
        hisal = _Hisal()
        if _flag("nobooked"):
            hisal.balance_booked = None
        return hisal

    def get_transactions(self, account, start_date=None, end_date=None):
        return self._need_tan() if _tan_mode_for(account) else _transactions()


if _fc is not None and os.environ.get("FAKE_FINTS") == "1":
    _fc.FinTS3PinTanClient = FakeFinTS3PinTanClient
