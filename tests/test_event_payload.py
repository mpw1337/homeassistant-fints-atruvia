"""Verify the new-transaction event payload respects the data-disclosure toggle."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from custom_components.fints_atruvia import CONF_EXPOSE_FULL_DATA
from custom_components.fints_atruvia.coordinator import FintsBankingCoordinator


def _make_coordinator(expose_full_data: bool) -> FintsBankingCoordinator:
    """Build a coordinator stub that exposes only what _build_event_payload needs."""
    coord = FintsBankingCoordinator.__new__(FintsBankingCoordinator)
    config_entry = MagicMock()
    config_entry.entry_id = "entry123"
    config_entry.options = {CONF_EXPOSE_FULL_DATA: expose_full_data}
    coord.config_entry = config_entry
    return coord


def test_event_payload_default_omits_bank_text():
    coord = _make_coordinator(expose_full_data=False)
    txn = {
        "date": "2026-05-01",
        "amount": Decimal("12.34"),
        "currency": "EUR",
        "purpose": "Geheimer Verwendungszweck",
        "creditor": "Sensitiver Empfaenger",
    }
    payload = coord._build_event_payload("DE51550905000000233922", txn, "abc")

    assert payload["iban_masked"].startswith("DE51")
    assert payload["iban_last4"] == "3922"
    assert payload["amount"] == 12.34
    assert "purpose" not in payload
    assert "applicant_name" not in payload
    # Full IBAN must never appear anywhere.
    assert "550905000000233922" not in str(payload)


def test_event_payload_opt_in_includes_bank_text():
    coord = _make_coordinator(expose_full_data=True)
    txn = {
        "date": "2026-05-01",
        "amount": Decimal("12.34"),
        "currency": "EUR",
        "purpose": "Miete Mai",
        "creditor": "Landlord GmbH",
    }
    payload = coord._build_event_payload("DE51550905000000233922", txn, "abc")

    assert payload["purpose"] == "Miete Mai"
    assert payload["applicant_name"] == "Landlord GmbH"
    # IBAN stays masked even in opt-in mode.
    assert "550905000000233922" not in payload["iban_masked"]


def test_event_payload_iban_short_does_not_crash():
    coord = _make_coordinator(expose_full_data=False)
    txn = {"amount": None}
    payload = coord._build_event_payload("DE", txn, "abc")
    assert payload["iban_last4"] == "DE"
    assert payload["amount"] is None
