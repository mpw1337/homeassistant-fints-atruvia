"""Verify the new-transaction event payload respects the data-disclosure toggle."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from custom_components.fints_atruvia import CONF_EXPOSE_FULL_DATA
from custom_components.fints_atruvia.coordinator import FintsBankingCoordinator
from custom_components.fints_atruvia.sensor import FintsBankingSensor

_FULL_IBAN = "GB33BUKB20201555555555"


def _make_coordinator(expose_full_data: bool) -> FintsBankingCoordinator:
    """Build a coordinator stub that exposes only what _build_event_payload needs."""
    coord = FintsBankingCoordinator.__new__(FintsBankingCoordinator)
    config_entry = MagicMock()
    config_entry.entry_id = "entry123"
    config_entry.options = {CONF_EXPOSE_FULL_DATA: expose_full_data}
    coord.config_entry = config_entry
    return coord


def _make_sensor(expose_full_data: bool, account_data: dict) -> FintsBankingSensor:
    """Build a FintsBankingSensor stub backed by a minimal coordinator."""
    coord = _make_coordinator(expose_full_data=expose_full_data)
    coord.data = {_FULL_IBAN: account_data}
    coord.is_2fa_pending = False
    sensor = FintsBankingSensor.__new__(FintsBankingSensor)
    sensor.coordinator = coord
    sensor._iban = _FULL_IBAN  # noqa: SLF001
    return sensor


def test_event_payload_default_omits_bank_text():
    coord = _make_coordinator(expose_full_data=False)
    txn = {
        "date": "2026-05-01",
        "amount": Decimal("12.34"),
        "currency": "EUR",
        "purpose": "Geheimer Verwendungszweck",
        "creditor": "Sensitiver Empfaenger",
    }
    payload = coord._build_event_payload("GB33BUKB20201555555555", txn, "abc")

    assert payload["iban_masked"].startswith("DE51")
    assert payload["iban_last4"] == "3922"
    assert payload["amount"] == 12.34
    assert "purpose" not in payload
    assert "applicant_name" not in payload
    # Full IBAN must never appear anywhere.
    assert "20201555555555" not in str(payload)


def test_event_payload_opt_in_includes_bank_text():
    coord = _make_coordinator(expose_full_data=True)
    txn = {
        "date": "2026-05-01",
        "amount": Decimal("12.34"),
        "currency": "EUR",
        "purpose": "Miete Mai",
        "creditor": "Landlord GmbH",
    }
    payload = coord._build_event_payload("GB33BUKB20201555555555", txn, "abc")

    assert payload["purpose"] == "Miete Mai"
    assert payload["applicant_name"] == "Landlord GmbH"
    # IBAN stays masked even in opt-in mode.
    assert "20201555555555" not in payload["iban_masked"]


def test_event_payload_iban_short_does_not_crash():
    coord = _make_coordinator(expose_full_data=False)
    txn = {"amount": None}
    payload = coord._build_event_payload("DE", txn, "abc")
    assert payload["iban_last4"] == "DE"
    assert payload["amount"] is None


def test_sensor_attrs_default_omits_transactions():
    account_data = {
        "available_balance": Decimal("100.00"),
        "balance_pending": Decimal("0.00"),
        "pending_amount": Decimal("0.00"),
        "booking_date": "2026-05-01",
        "transactions": [
            {
                "date": "2026-05-01",
                "amount": Decimal("12.34"),
                "currency": "EUR",
                "purpose": "Geheimer Verwendungszweck",
                "creditor": "Sensitiver Empfaenger",
            },
        ],
    }
    sensor = _make_sensor(expose_full_data=False, account_data=account_data)

    attrs = sensor.extra_state_attributes

    assert "transactions" not in attrs
    # Defense-in-depth: no bank-controlled text leaks via any other key either.
    rendered = str(attrs)
    assert "Geheimer Verwendungszweck" not in rendered
    assert "Sensitiver Empfaenger" not in rendered
    # IBAN must be masked in the attribute payload.
    assert attrs["iban"].startswith("DE51")
    assert _FULL_IBAN not in attrs["iban"]


def test_sensor_attrs_opt_in_exposes_transactions():
    account_data = {
        "available_balance": Decimal("100.00"),
        "balance_pending": Decimal("0.00"),
        "pending_amount": Decimal("0.00"),
        "booking_date": "2026-05-01",
        "transactions": [
            {
                "date": "2026-05-01",
                "amount": Decimal("12.34"),
                "currency": "EUR",
                "purpose": "Miete Mai",
                "creditor": "Landlord GmbH",
            },
        ],
    }
    sensor = _make_sensor(expose_full_data=True, account_data=account_data)

    attrs = sensor.extra_state_attributes

    assert "transactions" in attrs
    assert attrs["transactions"][0]["purpose"] == "Miete Mai"
    assert attrs["transactions"][0]["creditor"] == "Landlord GmbH"
    assert attrs["transactions"][0]["amount"] == 12.34
    # IBAN still masked even in opt-in mode.
    assert _FULL_IBAN not in attrs["iban"]
