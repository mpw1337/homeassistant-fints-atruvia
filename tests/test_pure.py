"""Tests for pure (hass-free) helpers — security-relevant behaviour."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fints.models import SEPAAccount

from custom_components.fints_atruvia import _entry_unique_id, iban_unique_id
from custom_components.fints_atruvia.api import FinTsAtruviaClient
from custom_components.fints_atruvia.config_flow import (
    _account_labels,
    _validate_https_url,
)
from custom_components.fints_atruvia.coordinator import (
    _compute_stats,
    _mask_iban_for_event,
    _transaction_hash,
)
from custom_components.fints_atruvia.sensor import _mask_iban
from custom_components.fints_atruvia.storage import redact_credentials


def _account(iban: str, accountnumber: str = "0000123456") -> SEPAAccount:
    return SEPAAccount(
        iban=iban,
        bic="GENODEF1XXX",
        accountnumber=accountnumber,
        subaccount=None,
        blz="12345678",
    )


# ---------------------------------------------------------------------------
# IBAN masking
# ---------------------------------------------------------------------------


def test_mask_iban_for_event_keeps_only_country_and_last4():
    masked = _mask_iban_for_event("GB33BUKB20201555555555")
    assert masked.startswith("GB33")
    assert masked.endswith("5555")
    assert "BUKB" not in masked
    assert "2020" not in masked


def test_mask_iban_for_event_short_iban_passes_through():
    # Defensive: silly inputs should not crash.
    assert _mask_iban_for_event("DE51") == "DE51"


def test_sensor_mask_iban_replaces_middle_with_stars():
    masked = _mask_iban("GB33BUKB20201555555555")
    assert masked.startswith("GB33")
    assert masked.endswith("5555")
    assert "BUKB" not in masked
    # The full account number must never appear.
    assert "20201555555555" not in masked


# ---------------------------------------------------------------------------
# api.py exception messages
# ---------------------------------------------------------------------------


def _client_returning_balance_segment(segment: object) -> FinTsAtruviaClient:
    """Build a FinTS wrapper whose bank dialog yields *segment* for get_balance."""
    client = FinTsAtruviaClient(
        blz="12345678",
        login="123456789",
        pin_provider=lambda: "0000",
        url="https://example.test/fints",
    )
    # Pre-seed the private client so _get_client() never opens a real dialog.
    client._client = SimpleNamespace(get_balance=lambda _account: segment)
    return client


def test_get_balance_missing_segment_error_hides_the_full_iban():
    # The ValueError text reaches home-assistant.log via HA's own
    # exception-chain logging, so it may only carry the last four digits.
    iban = "GB33BUKB20201555555555"
    client = _client_returning_balance_segment(None)

    with pytest.raises(ValueError, match="No balance data returned") as excinfo:
        client.get_balance(_account(iban))

    assert iban not in str(excinfo.value)
    assert "BUKB20201555555555" not in str(excinfo.value)
    assert "5555" in str(excinfo.value)


def test_get_balance_missing_booked_balance_error_hides_the_full_iban():
    iban = "GB33BUKB20201555555555"
    client = _client_returning_balance_segment(SimpleNamespace(balance_booked=None))

    with pytest.raises(ValueError, match="No booked balance") as excinfo:
        client.get_balance(_account(iban))

    assert "booked balance" in str(excinfo.value)
    assert iban not in str(excinfo.value)
    assert "BUKB20201555555555" not in str(excinfo.value)
    assert "5555" in str(excinfo.value)


@pytest.mark.parametrize("iban", ["", "DE", "abc"])
def test_get_balance_error_short_iban_is_not_echoed_back(iban):
    # Defensive: a garbage/truncated IBAN must neither crash nor be echoed as
    # if its tail were a real last4.
    client = _client_returning_balance_segment(None)

    with pytest.raises(ValueError, match="No balance data returned") as excinfo:
        client.get_balance(_account(iban))

    message = str(excinfo.value)
    assert iban == "" or iban not in message


# ---------------------------------------------------------------------------
# Unique-id hashing
# ---------------------------------------------------------------------------


def test_iban_unique_id_does_not_leak_iban():
    iban = "GB33BUKB20201555555555"
    uid = iban_unique_id("abc123", iban)
    assert iban not in uid
    assert len(uid) == 16
    assert all(c in "0123456789abcdef" for c in uid)


def test_iban_unique_id_is_stable():
    a = iban_unique_id("entry1", "GB33BUKB20201555555555")
    b = iban_unique_id("entry1", "GB33BUKB20201555555555")
    assert a == b


def test_iban_unique_id_differs_across_entries():
    iban = "GB33BUKB20201555555555"
    assert iban_unique_id("entry1", iban) != iban_unique_id("entry2", iban)


def test_iban_unique_id_differs_across_ibans():
    a = iban_unique_id("entry1", "GB33BUKB20201555555555")
    b = iban_unique_id("entry1", "DE51550905000000233923")
    assert a != b


_MASTER_KEY = Fernet.generate_key()
_OTHER_KEY = Fernet.generate_key()


def test_entry_unique_id_does_not_leak_login_or_blz():
    uid = _entry_unique_id(_MASTER_KEY, "12345678", "netkey1")
    assert "netkey1" not in uid
    assert "12345678" not in uid
    assert len(uid) == 16
    assert all(c in "0123456789abcdef" for c in uid)


def test_entry_unique_id_is_stable():
    a = _entry_unique_id(_MASTER_KEY, "12345678", "netkey1")
    b = _entry_unique_id(_MASTER_KEY, "12345678", "netkey1")
    assert a == b


def test_entry_unique_id_differs_across_usernames():
    assert _entry_unique_id(_MASTER_KEY, "12345678", "netkey1") != _entry_unique_id(
        _MASTER_KEY, "12345678", "netkey2"
    )


def test_entry_unique_id_differs_across_blz():
    assert _entry_unique_id(_MASTER_KEY, "12345678", "netkey1") != _entry_unique_id(
        _MASTER_KEY, "87654321", "netkey1"
    )


def test_entry_unique_id_differs_across_keys():
    """The property that defeats offline brute force: blz+login is not enough.

    Without the install's master key, an attacker holding
    ``core.config_entries`` (which carries the cleartext blz) cannot confirm a
    guessed NetKey login against the stored unique_id.
    """
    assert _entry_unique_id(_MASTER_KEY, "12345678", "netkey1") != _entry_unique_id(
        _OTHER_KEY, "12345678", "netkey1"
    )


def test_entry_unique_id_is_hmac_of_domain_separated_message():
    expected = hmac.new(
        _MASTER_KEY,
        b"entry_unique_id|12345678|netkey1",
        hashlib.sha256,
    ).hexdigest()[:16]
    assert _entry_unique_id(_MASTER_KEY, "12345678", "netkey1") == expected


# ---------------------------------------------------------------------------
# Account picker labels
# ---------------------------------------------------------------------------


def test_account_labels_never_expose_account_number():
    accounts = [
        _account("DE51550905000000233922", accountnumber="0000123456"),
        _account("GB33BUKB20201555555555", accountnumber="0000999999"),
    ]
    labels = _account_labels(accounts)
    assert not any("0000123456" in label for label in labels)
    assert not any("0000999999" in label for label in labels)


def test_account_labels_disambiguate_same_last4():
    accounts = [
        _account("DE51550905000001233000", accountnumber="1"),
        _account("DE51550905000009993000", accountnumber="2"),
    ]
    labels = _account_labels(accounts)
    assert len(set(labels)) == len(labels)
    assert all(label.startswith("Konto …3000") for label in labels)


def test_account_labels_unique_last4_stay_plain():
    accounts = [
        _account("DE51550905000000233922", accountnumber="1"),
        _account("GB33BUKB20201555555555", accountnumber="2"),
    ]
    labels = _account_labels(accounts)
    assert labels == ["Konto …3922", "Konto …5555"]


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://bank.de/fints",
        "ftp://bank.de/fints",
        "",
        "not a url",
        "https://",
        # Do not "fix" the ambiguous-character warnings below: the domain
        # spells "atruvia" with Cyrillic lookalikes, and latinising it would
        # make this case pass for the wrong reason.
        # IDN homoglyph (kyrillisches 'а')  # noqa: RUF003 - see above
        "https://атруvia.de/fints",  # noqa: RUF001 - see above
    ],
)
def test_validate_https_url_rejects_unsafe(url):
    assert _validate_https_url(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://fints2.atruvia.de/cgi-bin/hbciservlet",
        "https://example.com/",
        "https://bank.de:8443/path",
    ],
)
def test_validate_https_url_accepts_https(url):
    assert _validate_https_url(url) is None


# ---------------------------------------------------------------------------
# Redaction / hashing helpers
# ---------------------------------------------------------------------------


def test_redact_credentials_strips_sensitive_keys():
    redacted = redact_credentials(
        {
            "username": "alice",
            "password": "hunter2",
            "credential_id": "abc",
            "other": "ok",
        }
    )
    assert redacted["username"] == "***"
    assert redacted["password"] == "***"  # noqa: S105 - the redaction marker, not a credential
    assert redacted["credential_id"] == "***"
    assert redacted["other"] == "ok"


def test_transaction_hash_is_stable_and_unique():
    txn_a = {
        "date": "2026-05-01",
        "amount": Decimal("10.00"),
        "purpose": "X",
        "creditor": "Y",
    }
    txn_b = {
        "date": "2026-05-01",
        "amount": Decimal("10.00"),
        "purpose": "X",
        "creditor": "Y",
    }
    txn_c = {
        "date": "2026-05-01",
        "amount": Decimal("10.01"),
        "purpose": "X",
        "creditor": "Y",
    }
    assert _transaction_hash(txn_a) == _transaction_hash(txn_b)
    assert _transaction_hash(txn_a) != _transaction_hash(txn_c)


def test_compute_stats_sums_income_and_expense():
    transactions = [
        {"amount": Decimal("100.00")},
        {"amount": Decimal("-30.00")},
        {"amount": Decimal("-20.00")},
        {"amount": None},
        {"amount": "garbage"},
    ]
    stats = _compute_stats(transactions)
    assert stats["income_30d"] == Decimal("100.00")
    assert stats["expense_30d"] == Decimal("50.00")
    assert stats["count_30d"] == 5
