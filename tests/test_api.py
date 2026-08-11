"""Tests for FinTsAtruviaClient.init_system_id — TAN-mechanism negotiation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.fints_atruvia.api import FinTsAtruviaClient, NoTanMechanismError


def _client() -> FinTsAtruviaClient:
    return FinTsAtruviaClient(
        blz="12345678",
        login="123456789",
        pin_provider=lambda: "0000",
        url="https://example.test/fints",
    )


def test_init_system_id_raises_when_bank_offers_no_two_step_mechanism():
    """No usable HITANS -> ``get_tan_mechanisms()`` stays ``{}`` -> guard must raise.

    Without the guard, python-fints' own ``is_tan_media_required()`` does
    ``self.get_tan_mechanisms()[self.get_current_tan_mechanism()]`` and raises
    a raw ``KeyError`` instead of a diagnosable, log-hygienic error.
    """
    bank = MagicMock()
    bank.get_current_tan_mechanism.return_value = "999"
    bank.get_tan_mechanisms.return_value = {}
    bank.allowed_security_functions = ["999"]
    bank.selected_tan_medium = None
    bank.is_tan_media_required.side_effect = KeyError("999")

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(NoTanMechanismError),
    ):
        client.init_system_id()

    bank.is_tan_media_required.assert_not_called()
    bank.__enter__.assert_not_called()
