"""Tests for FinTsAtruviaClient.init_system_id — TAN-mechanism negotiation."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fints.client import FinTS3PinTanClient
from fints.exceptions import FinTSClientPINError

from custom_components.fints_atruvia.api import (
    AuthRejectedError,
    FinTsAtruviaClient,
    NoTanMechanismError,
    _ExtendedFinTSClient,
    _hitans_versions,
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
    bank.observed_codes = ["3920"]

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(NoTanMechanismError) as exc_info,
    ):
        client.init_system_id()

    assert exc_info.value.codes == ("3920",)
    bank.is_tan_media_required.assert_not_called()
    bank.__enter__.assert_not_called()


def test_init_system_id_no_tan_mechanism_defaults_to_empty_codes_without_attribute():
    """If the mocked client has no ``observed_codes`` at all, codes defaults to ``()``.

    ``getattr(client, "observed_codes", ())`` is the defensive fallback for
    exotic client implementations that don't set the attribute.
    """
    bank = MagicMock(
        spec=[
            "fetch_tan_mechanisms",
            "get_current_tan_mechanism",
            "get_tan_mechanisms",
            "allowed_security_functions",
            "selected_tan_medium",
            "is_tan_media_required",
            "sca_not_required",
            "__enter__",
            "bpd",
        ]
    )
    bank.fetch_tan_mechanisms.return_value = None
    bank.get_current_tan_mechanism.return_value = "999"
    bank.get_tan_mechanisms.return_value = {}
    bank.allowed_security_functions = ["999"]
    bank.selected_tan_medium = None
    bank.is_tan_media_required.side_effect = KeyError("999")
    bank.sca_not_required = False

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(NoTanMechanismError) as exc_info,
    ):
        client.init_system_id()

    assert exc_info.value.codes == ()


def test_ensure_two_step_mechanism_logs_hitans_versions_from_bpd(
    caplog: pytest.LogCaptureFixture,
):
    """The no-tan-mechanism debug log lists the sorted, deduped HITANS versions."""
    seg6 = MagicMock()
    seg6.header.version = 6
    seg7 = MagicMock()
    seg7.header.version = 7
    bank = MagicMock()
    bank.get_current_tan_mechanism.return_value = "999"
    bank.get_tan_mechanisms.return_value = {}
    bank.allowed_security_functions = ["999"]
    bank.sca_not_required = False
    bank.observed_codes = []
    bank.bpd.find_segments.return_value = [seg7, seg6, seg7]

    caplog.set_level(logging.DEBUG, logger="custom_components.fints_atruvia.api")
    with pytest.raises(NoTanMechanismError):
        FinTsAtruviaClient._ensure_two_step_mechanism_or_sca_exempt(bank)

    bank.bpd.find_segments.assert_called_once_with("HITANS")
    assert "[6, 7]" in caplog.text


def test_ensure_two_step_mechanism_logs_empty_hitans_versions_without_crashing(
    caplog: pytest.LogCaptureFixture,
):
    """An empty BPD must not crash the guard; it logs an empty version list."""
    bank = MagicMock()
    bank.get_current_tan_mechanism.return_value = "999"
    bank.get_tan_mechanisms.return_value = {}
    bank.allowed_security_functions = ["999"]
    bank.sca_not_required = False
    bank.observed_codes = []
    bank.bpd.find_segments.return_value = []

    caplog.set_level(logging.DEBUG, logger="custom_components.fints_atruvia.api")
    with pytest.raises(NoTanMechanismError):
        FinTsAtruviaClient._ensure_two_step_mechanism_or_sca_exempt(bank)

    assert "HITANS versions in BPD: []" in caplog.text


def test_hitans_versions_returns_empty_list_when_bpd_access_raises():
    """Exotic client where accessing ``bpd`` itself raises: degrades to ``[]``."""

    class _NoBpd:
        @property
        def bpd(self) -> None:
            msg = "no bpd on this client"
            raise RuntimeError(msg)

    assert _hitans_versions(_NoBpd()) == []


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
    instance.observed_codes = []
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
    assert instance.observed_codes == ["9942"]
    assert instance.sca_not_required is False
    super_mock.assert_called_once_with(dialog, segment, response)


def test_process_response_flags_sca_not_required_without_recording_code():
    """3076 (SCA not required) sets the flag but is not a 9xxx error code."""
    instance = _bare_extended_client()
    response = MagicMock(code="3076")

    with patch.object(FinTS3PinTanClient, "_process_response"):
        instance._process_response(MagicMock(), MagicMock(), response)

    assert instance.observed_error_codes == []
    assert instance.observed_codes == ["3076"]
    assert instance.sca_not_required is True


def test_process_response_records_every_code_once_in_order():
    """observed_codes collects every code seen, in order, without duplicates.

    observed_error_codes stays limited to 9xxx codes (unchanged semantics);
    sca_not_required is set by 3076 as before.
    """
    instance = _bare_extended_client()

    with patch.object(FinTS3PinTanClient, "_process_response"):
        for code in ("3920", "3076", "9075", "3920"):
            instance._process_response(MagicMock(), MagicMock(), MagicMock(code=code))

    assert instance.observed_codes == ["3920", "3076", "9075"]
    assert instance.observed_error_codes == ["9075"]
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
