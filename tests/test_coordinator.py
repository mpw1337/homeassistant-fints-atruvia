"""Tests for the coordinator's reauth, event-loss, and shutdown behaviour."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.fints_atruvia import (
    CONF_BLZ,
    CONF_CREDENTIAL_ID,
    CONF_PRODUCT_ID,
    CONF_SELECTED_ACCOUNTS,
    CONF_URL,
    DOMAIN,
    EVENT_NEW_TRANSACTION,
)
from custom_components.fints_atruvia.api import TanRequiredError
from custom_components.fints_atruvia.coordinator import (
    FintsBankingCoordinator,
    _transaction_hash,
)

_IBAN1 = "DE11500105175407324931"
_IBAN2 = "DE02500105170137075030"


def _make_entry(selected_accounts: list[str]) -> MockConfigEntry:
    """Build a config entry shaped like the one FintsBankingCoordinator expects."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BLZ: "12345678",
            CONF_URL: "https://example.test/fints",
            CONF_PRODUCT_ID: None,
            CONF_SELECTED_ACCOUNTS: selected_accounts,
            CONF_CREDENTIAL_ID: "test-cred-id",
        },
        options={},
    )


def _make_coordinator(hass, selected_accounts: list[str]) -> FintsBankingCoordinator:
    """Build a real coordinator with a mocked FinTS client, skipping async_init."""
    entry = _make_entry(selected_accounts)
    entry.add_to_hass(hass)
    coordinator = FintsBankingCoordinator(hass, entry)
    coordinator._client = MagicMock()
    # Avoid persisting a MagicMock as the FinTS state blob on a successful update.
    coordinator._client.deconstruct.return_value = None
    return coordinator


def _account(iban: str) -> SimpleNamespace:
    return SimpleNamespace(iban=iban)


def _balance() -> dict:
    return {
        "balance": Decimal("100.00"),
        "currency": "EUR",
        "available_balance": Decimal("100.00"),
        "balance_pending": Decimal(0),
        "pending_amount": Decimal(0),
        "booking_date": "2026-08-01",
    }


async def test_tan_required_before_first_success_raises_auth_failed(hass):
    """Fix 1: `raise ... from e.response` must not turn into a TypeError."""
    coordinator = _make_coordinator(hass, [_IBAN1])
    coordinator._client.get_accounts.side_effect = TanRequiredError(SimpleNamespace())

    with pytest.raises(ConfigEntryAuthFailed) as excinfo:
        await coordinator._async_update_data()

    assert isinstance(excinfo.value.__cause__, TanRequiredError)


async def test_tan_required_with_existing_data_returns_last_good(hass):
    """Fix 1 regression: once data exists, a TAN request must return last-known-good."""
    coordinator = _make_coordinator(hass, [_IBAN1])
    coordinator.data = {"stale": "data"}
    coordinator._client.get_accounts.side_effect = TanRequiredError(SimpleNamespace())

    result = await coordinator._async_update_data()

    assert result is coordinator.data
    assert coordinator.is_2fa_pending is True


async def test_no_lost_events_when_later_account_fails(hass):
    """Fix 2: seen-hashes must not advance for accounts whose update never completed."""
    coordinator = _make_coordinator(hass, [_IBAN1, _IBAN2])
    coordinator._seen_initialised = True
    coordinator.data = {"prior": "state"}  # simulate a previously successful update

    new_txn = {
        "date": "2026-08-01",
        "amount": Decimal("42.00"),
        "purpose": "Miete",
        "creditor": "Foo GmbH",
        "currency": "EUR",
    }
    iban2_should_fail = True

    def get_accounts():
        return [_account(_IBAN1), _account(_IBAN2)]

    def get_balance(account):
        if account.iban == _IBAN2 and iban2_should_fail:
            raise TanRequiredError(SimpleNamespace())
        return _balance()

    def get_transactions(account, _days):
        return [new_txn] if account.iban == _IBAN1 else []

    coordinator._client.get_accounts.side_effect = get_accounts
    coordinator._client.get_balance.side_effect = get_balance
    coordinator._client.get_transactions.side_effect = get_transactions

    events = async_capture_events(hass, EVENT_NEW_TRANSACTION)

    # First poll: IBAN1 is processed fine, but IBAN2 aborts with a TanRequiredError.
    result = await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert result is coordinator.data
    assert coordinator.is_2fa_pending is True
    assert _transaction_hash(new_txn) not in coordinator._seen_hashes.get(_IBAN1, set())
    assert events == []

    # Second poll: both accounts succeed now. The transaction discarded above
    # must still be reported as new — it must not have been silently marked seen.
    iban2_should_fail = False
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["transaction_hash"] == _transaction_hash(new_txn)


async def test_missing_account_warning_masks_the_iban(hass, caplog):
    """A selected IBAN missing at the bank is logged at WARNING — masked only."""
    coordinator = _make_coordinator(hass, [_IBAN1])
    coordinator._seen_initialised = True
    coordinator._client.get_accounts.return_value = []

    result = await coordinator._async_update_data()

    assert result == {}
    assert "not found at bank" in caplog.text
    # Full IBAN must not appear anywhere in the log record.
    assert _IBAN1 not in caplog.text
    assert "5010517540732" not in caplog.text
    # ...but the operator still has to be able to tell which account it was.
    assert _IBAN1[-4:] in caplog.text


async def test_async_shutdown_wipes_pin_closes_client_and_calls_super(hass):
    """Fix 3: async_shutdown must wipe the PIN, close the client, and call super()."""
    coordinator = _make_coordinator(hass, [_IBAN1])
    coordinator._pin = "1234"
    mock_client = coordinator._client

    await coordinator.async_shutdown()

    assert coordinator._pin is None
    assert coordinator._client is None
    mock_client.close.assert_called_once()
    # DataUpdateCoordinator.async_shutdown() sets this — proves super() ran.
    assert coordinator._shutdown_requested is True


async def test_config_entry_is_set_via_super_init(hass):
    """Fix 4: config_entry must go through super().__init__(), not an ad hoc set."""
    entry = _make_entry([_IBAN1])
    entry.add_to_hass(hass)

    coordinator = FintsBankingCoordinator(hass, entry)

    assert coordinator.config_entry is entry
