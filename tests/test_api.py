"""Tests for FinTsAtruviaClient.init_system_id — TAN-mechanism negotiation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fints.client import FinTS3PinTanClient
from fints.exceptions import FinTSClientPINError

from custom_components.fints_atruvia.api import (
    AuthRejectedError,
    FinTsAtruviaClient,
    NoTanMechanismError,
    _ExtendedFinTSClient,
)


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
    bank.sca_not_required = False

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(NoTanMechanismError),
    ):
        client.init_system_id()

    bank.is_tan_media_required.assert_not_called()
    bank.__enter__.assert_not_called()


def test_init_system_id_allows_one_step_when_bank_signals_sca_exemption():
    """3076 (SCA not required) -> guard must not raise, one-step init proceeds.

    ``is_tan_media_required()`` is skipped entirely for the SCA-exempt path
    since python-fints raises a raw ``KeyError`` for it with mechanism
    "999" (same underlying quirk as the guard above).
    """
    bank = MagicMock()
    bank.get_current_tan_mechanism.return_value = "999"
    bank.get_tan_mechanisms.return_value = {}
    bank.allowed_security_functions = ["999"]
    bank.selected_tan_medium = None
    bank.sca_not_required = True
    bank.init_tan_response = None
    bank.get_sepa_accounts.return_value = []

    client = _client()
    with patch.object(client, "_build_client", return_value=bank):
        result = client.init_system_id()

    assert result is None
    bank.is_tan_media_required.assert_not_called()
    bank.__enter__.assert_called_once()


def _bare_extended_client() -> _ExtendedFinTSClient:
    """Build an ``_ExtendedFinTSClient`` without running python-fints' __init__.

    ``object.__new__`` skips the real constructor (no network/bank setup),
    so the diagnostic attributes it would normally set have to be
    initialised manually here, mirroring what ``__init__`` does.
    """
    instance = object.__new__(_ExtendedFinTSClient)
    instance.observed_error_codes = []
    instance.sca_not_required = False
    return instance


def test_process_response_records_9xxx_code_and_delegates():
    """A 9xxx code is appended to observed_error_codes and super() still runs."""
    instance = _bare_extended_client()
    dialog = MagicMock()
    segment = MagicMock()
    response = MagicMock(code="9942")

    with patch.object(FinTS3PinTanClient, "_process_response") as super_mock:
        instance._process_response(dialog, segment, response)

    assert instance.observed_error_codes == ["9942"]
    assert instance.sca_not_required is False
    super_mock.assert_called_once_with(dialog, segment, response)


def test_process_response_flags_sca_not_required_without_recording_code():
    """3076 (SCA not required) sets the flag but is not a 9xxx error code."""
    instance = _bare_extended_client()
    response = MagicMock(code="3076")

    with patch.object(FinTS3PinTanClient, "_process_response"):
        instance._process_response(MagicMock(), MagicMock(), response)

    assert instance.observed_error_codes == []
    assert instance.sca_not_required is True


def test_process_response_ignores_unrelated_code():
    """A code that is neither 9xxx nor 3076 leaves both attributes untouched."""
    instance = _bare_extended_client()
    response = MagicMock(code="3920")

    with patch.object(FinTS3PinTanClient, "_process_response"):
        instance._process_response(MagicMock(), MagicMock(), response)

    assert instance.observed_error_codes == []
    assert instance.sca_not_required is False


def test_init_system_id_raises_auth_rejected_error_with_observed_codes():
    """A PIN error during dialog init surfaces as AuthRejectedError with codes."""
    pin_error = FinTSClientPINError("Error during dialog initialization, PIN wrong?")
    bank = MagicMock()
    bank.observed_error_codes = ["9942"]
    bank.fetch_tan_mechanisms.side_effect = pin_error

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(AuthRejectedError) as exc_info,
    ):
        client.init_system_id()

    assert exc_info.value.codes == ("9942",)
    assert exc_info.value.__cause__ is pin_error


def test_complete_tan_raises_auth_rejected_error_and_still_closes_dialog():
    """A PIN error from send_tan surfaces as AuthRejectedError; close path runs too.

    ``_close_standing_dialog`` reads the private ``_standing_dialog``
    attribute directly (python-fints exposes no public accessor), so the
    fake client sets it to ``None`` to exercise that no-op branch without
    crashing.
    """
    pin_error = FinTSClientPINError("Error during dialog initialization, PIN wrong?")
    fake_client = MagicMock()
    fake_client._standing_dialog = None
    fake_client.observed_error_codes = ["9942"]
    fake_client.send_tan.side_effect = pin_error

    client = _client()
    client._client = fake_client
    with pytest.raises(AuthRejectedError) as exc_info:
        client.complete_tan(tan_response=MagicMock(), tan="")

    assert exc_info.value.codes == ("9942",)
    assert exc_info.value.__cause__ is pin_error
    fake_client.__exit__.assert_not_called()
