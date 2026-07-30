"""Tests for pure (hass-free) helpers — security-relevant behaviour."""
from __future__ import annotations

from decimal import Decimal

import pytest

from custom_components.fints_atruvia import iban_unique_id
from custom_components.fints_atruvia.config_flow import _validate_https_url
from custom_components.fints_atruvia.coordinator import (
    _compute_stats,
    _mask_iban_for_event,
    _transaction_hash,
)
from custom_components.fints_atruvia.sensor import _mask_iban
from custom_components.fints_atruvia.storage import redact_credentials


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
        # IDN homoglyph (kyrillisches 'а')
        "https://атруvia.de/fints",
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
        {"username": "alice", "password": "hunter2", "credential_id": "abc", "other": "ok"}
    )
    assert redacted["username"] == "***"
    assert redacted["password"] == "***"
    assert redacted["credential_id"] == "***"
    assert redacted["other"] == "ok"


def test_transaction_hash_is_stable_and_unique():
    txn_a = {"date": "2026-05-01", "amount": Decimal("10.00"), "purpose": "X", "creditor": "Y"}
    txn_b = {"date": "2026-05-01", "amount": Decimal("10.00"), "purpose": "X", "creditor": "Y"}
    txn_c = {"date": "2026-05-01", "amount": Decimal("10.01"), "purpose": "X", "creditor": "Y"}
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
