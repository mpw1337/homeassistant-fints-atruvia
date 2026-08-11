"""Tests for the config flow — keyed unique_id and FinTS client / PIN lifetime."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fints.models import SEPAAccount
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

from custom_components.fints_atruvia import DOMAIN, _entry_unique_id
from custom_components.fints_atruvia.api import NoTanMechanismError
from custom_components.fints_atruvia.config_flow import FintsBankingConfigFlow
from custom_components.fints_atruvia.storage import async_get_master_key

_URL = "https://fints2.atruvia.de/cgi-bin/hbciservlet"
_IBAN = "DE51120300009876543922"


def _account(iban: str = _IBAN) -> SEPAAccount:
    return SEPAAccount(
        iban=iban,
        bic="GENODEF1XXX",
        accountnumber="9876543922",
        subaccount=None,
        blz="12345678",
    )


def _client_mock() -> MagicMock:
    """A FinTS client that gets through the handshake without a bank."""
    client = MagicMock()
    # None = no SCA challenge, so the flow goes straight to the account picker.
    client.init_system_id.return_value = None
    client.get_accounts.return_value = [_account()]
    return client


async def _flow_to_account_picker(hass, client: MagicMock) -> str:
    """Drive the flow to the account-picker step; return the flow_id.

    At that point ``flow._client`` is the passed-in mock — the state both
    lifetime tests below care about.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.fints_atruvia.config_flow.FinTsAtruviaClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "blz": "12345678",
                "username": "netkey1",
                "password": "hunter2",
                "url": _URL,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "accounts"
    assert not result.get("errors")
    client.get_accounts.assert_called_once()
    return result["flow_id"]


async def test_flow_unique_id_is_keyed_hmac(hass):
    """``async_set_unique_id`` must use the master-key HMAC, not a bare hash.

    The flow is stopped at the FinTS handshake (no network in tests); by then
    the unique_id has already been set, which is all this test cares about.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    client = MagicMock()
    client.init_system_id.side_effect = RuntimeError("no bank in tests")
    with patch(
        "custom_components.fints_atruvia.config_flow.FinTsAtruviaClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "blz": "12345678",
                "username": "netkey1",
                "password": "hunter2",
                "url": _URL,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    progress = hass.config_entries.flow.async_progress()
    assert len(progress) == 1
    unique_id = progress[0]["context"]["unique_id"]

    key = await async_get_master_key(hass)
    assert unique_id == _entry_unique_id(key, "12345678", "netkey1")
    assert "netkey1" not in unique_id
    assert "12345678" not in unique_id
    # Regression guard: not the pre-fix unsalted digest, which anyone holding
    # core.config_entries could have brute-forced back to the login.
    unsalted = hashlib.sha256(b"12345678|netkey1").hexdigest()[:16]
    assert unique_id != unsalted


async def test_flow_shows_dedicated_error_when_bank_offers_no_tan_mechanism(hass):
    """``NoTanMechanismError`` must surface as ``no_tan_mechanism``.

    Before the fix, python-fints' own ``KeyError`` for this case fell through
    the blanket ``except Exception`` and looked identical to a network
    failure — indistinguishable from a wrong URL or a down bank.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    client = MagicMock()
    client.init_system_id.side_effect = NoTanMechanismError(
        "Bank offered no supported two-step TAN mechanism"
    )
    with patch(
        "custom_components.fints_atruvia.config_flow.FinTsAtruviaClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "blz": "12345678",
                "username": "netkey1",
                "password": "hunter2",
                "url": _URL,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_tan_mechanism"}


async def test_abandoned_flow_closes_client(hass):
    """Closing the dialog mid-flow must release the client that holds the PIN.

    ``async_remove`` is HA's hook for a flow that never completed. Without it
    the orphaned flow object keeps the FinTS client — and the PIN python-fints
    stores inside it — alive until garbage collection.
    """
    client = _client_mock()
    flow_id = await _flow_to_account_picker(hass, client)

    # Private accessor on purpose: we need the flow *object* to re-invoke
    # async_remove below, and async_progress() only hands out dicts.
    flow = hass.config_entries.flow._progress[flow_id]
    assert flow._client is client

    hass.config_entries.flow.async_abort(flow_id)
    # close() runs in the executor and is not awaited by the @callback.
    await hass.async_block_till_done(wait_background_tasks=True)

    assert client.close.call_count == 1
    assert flow._client is None

    # HA itself cannot abort twice (the flow is gone from _progress), but the
    # attribute nulling above is what guarantees close() stays single-shot.
    with pytest.raises(UnknownFlow):
        hass.config_entries.flow.async_abort(flow_id)
    flow.async_remove()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert client.close.call_count == 1


async def test_completed_flow_closes_client(hass):
    """``_finish_setup`` must close the client before it creates the entry.

    The last bank access has already happened by then, so the client (and the
    PIN python-fints holds inside it) has no reason to stay alive any longer.
    Asserting the close happened *before* ``async_create_entry`` matters: HA
    also runs ``async_remove`` once the flow is torn down, so a plain
    "close was called" assertion would pass even if ``_finish_setup`` did
    nothing.
    """
    client = _client_mock()
    flow_id = await _flow_to_account_picker(hass, client)

    flow = hass.config_entries.flow._progress[flow_id]

    closes_at_entry_creation: list[int] = []
    original_create_entry = FintsBankingConfigFlow.async_create_entry

    def _spy_create_entry(self, **kwargs):
        closes_at_entry_creation.append(client.close.call_count)
        return original_create_entry(self, **kwargs)

    with (
        patch.object(FintsBankingConfigFlow, "async_create_entry", _spy_create_entry),
        patch(
            "custom_components.fints_atruvia.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"selected_accounts": [_IBAN]}
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert closes_at_entry_creation == [1]
    assert client.close.call_count == 1
    assert flow._client is None
    # Sanity check that the close happened on the real success path: the entry
    # exists and carries only the credential_id, no PIN.
    entry_data = result["data"]
    assert "credential_id" in entry_data
    assert "password" not in entry_data
