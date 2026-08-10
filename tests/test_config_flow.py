"""Tests for the config flow — unique_id derivation must stay keyed."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.fints_atruvia import DOMAIN, _entry_unique_id
from custom_components.fints_atruvia.storage import async_get_master_key

_URL = "https://fints2.atruvia.de/cgi-bin/hbciservlet"


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
