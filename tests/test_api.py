"""Tests for FinTsAtruviaClient's login bootstrap (system-id sync, SCA, BPD)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.exceptions import FinTSClientPINError
from fints.formals import CUSTOMER_ID_ANONYMOUS
from fints.segments.dialog import HKSYN3

from custom_components.fints_atruvia.api import (
    AuthRejectedError,
    FinTsAtruviaClient,
    NoTanMechanismError,
    _ExtendedFinTSClient,
    _hitans_versions,
    describe_exception_chain,
)

_URL = "https://example.test/fints"


def _client() -> FinTsAtruviaClient:
    return FinTsAtruviaClient(
        blz="12345678",
        login="123456789",
        pin_provider=lambda: "0000",
        url=_URL,
    )


def _challenge() -> NeedTANResponse:
    """A NeedTANResponse without python-fints' constructor (no bank segments)."""
    return NeedTANResponse.__new__(NeedTANResponse)


class _Response:
    """Institute message: carries HISYN (a system id) or not."""

    def __init__(self, system_id: str | None = None) -> None:
        self._system_id = system_id

    def find_segment_first(self, _segment_type):
        if self._system_id is None:
            return None
        return SimpleNamespace(system_id=self._system_id)


class _Dialog:
    """Scripted FinTSDialog: what the bank answers to this dialog's init."""

    def __init__(
        self,
        bank: _Bank,
        *,
        system_id: str | None = None,
        selects: str | None = None,
        challenge: NeedTANResponse | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._bank = bank
        self._system_id = system_id
        self._selects = selects
        self._challenge = challenge
        self._raises = raises
        self.init_segments: tuple | None = None
        self.entered = 0
        self.ended = False

    def init(self, *segments):
        self.init_segments = segments
        if self._raises is not None:
            raise self._raises
        if self._selects is not None:
            # python-fints picks the two-step mechanism from response code 3920
            # while processing the init response.
            self._bank.set_tan_mechanism(self._selects)
        if self._challenge is not None:
            self._bank.init_tan_response = self._challenge
        return _Response(self._system_id)

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.entered -= 1
        if self.entered == 0:
            self.end()

    def end(self):
        self.ended = True


class _Bank:
    """FinTS3PinTanClient stand-in at the level api.py drives it."""

    def __init__(  # noqa: PLR0913 - one knob per scripted bank behaviour
        self,
        *,
        sync_dialogs: list[_Dialog] | None = None,
        standing_dialog: _Dialog | None = None,
        system_id: str = "0",
        accounts: list | None = None,
        bpd_segments: list | None = None,
        send_tan_result=None,
    ) -> None:
        self._sync_dialogs = list(sync_dialogs or [])
        self._standing_template = standing_dialog
        self.system_id = system_id
        self.init_tan_response = None
        self._standing_dialog = None
        segments = ["HITANS"] if bpd_segments is None else bpd_segments
        self.bpd = SimpleNamespace(segments=segments)
        self.bpa = None
        self.bpd_version = 0
        self.observed_codes: list[str] = []
        self.observed_error_codes: list[str] = []
        self._mechanism = "999"
        self._accounts = accounts if accounts is not None else ["acc"]
        self._send_tan_result = send_tan_result
        self.sync_dialogs_opened: list[_Dialog] = []
        self.standing_dialogs_opened: list[_Dialog] = []
        self.get_sepa_accounts_calls = 0
        self.send_tan_calls: list[tuple] = []

    def _new_dialog(self, lazy_init=False):  # noqa: FBT002 - mirrors python-fints' signature
        assert lazy_init, "the sync dialog must be opened lazily (init with HKSYN)"
        dialog = self._sync_dialogs.pop(0)
        self.sync_dialogs_opened.append(dialog)
        return dialog

    def set_tan_mechanism(self, mechanism):
        self._mechanism = mechanism

    def get_current_tan_mechanism(self):
        return self._mechanism

    def __enter__(self):
        assert self._standing_dialog is None, "Cannot double __enter__()"
        dialog = self._standing_template or _Dialog(self)
        # Mirrors python-fints: _standing_dialog is assigned before init()
        # runs, so a raise here still leaves a (half-entered) dialog behind.
        self._standing_dialog = dialog
        self.standing_dialogs_opened.append(dialog)
        dialog.init()
        dialog.__enter__()

    def __exit__(self, *exc):
        self._standing_dialog.__exit__(*exc)
        self._standing_dialog = None

    def get_sepa_accounts(self):
        self.get_sepa_accounts_calls += 1
        return self._accounts

    def send_tan(self, challenge, tan):
        self.send_tan_calls.append((challenge, tan))
        if isinstance(self._send_tan_result, Exception):
            raise self._send_tan_result
        return self._send_tan_result

    def deconstruct(self, including_private=False):  # noqa: FBT002 - mirrors python-fints' signature
        return b"blob-private" if including_private else b"blob"


def _run_init(bank: _Bank):
    client = _client()
    with patch.object(client, "_build_client", return_value=bank):
        return client, client.init_system_id()


# ---------------------------------------------------------------------------
# init_system_id
# ---------------------------------------------------------------------------


def test_init_system_id_one_step_sync_assigns_system_id_and_caches_accounts():
    """SCA-exempt login (3076): HISYN arrives in the plain sync, no TAN needed."""
    bank = _Bank(sync_dialogs=[_Dialog(None, system_id="SYS1")])
    bank._sync_dialogs[0]._bank = bank

    client, result = _run_init(bank)

    assert result is None
    assert bank.system_id == "SYS1"
    sync = bank.sync_dialogs_opened[0]
    assert isinstance(sync.init_segments[0], HKSYN3)
    assert sync.ended
    assert bank._standing_dialog is None
    assert bank.get_sepa_accounts_calls == 1
    assert client.get_accounts() == ["acc"]


def test_sync_rejected_dialog_is_dropped_without_hkend():
    """No HISYN, no challenge: drop the dialog instead of sending HKEND."""
    bank = _Bank()
    rejected = _Dialog(bank)  # answers neither HISYN nor a challenge
    bank._sync_dialogs = [rejected]

    result = FinTsAtruviaClient._sync(bank)

    assert result is None
    assert not rejected.ended
    assert rejected.open is False


def test_init_system_id_repeats_sync_two_step_when_bank_demands_sca():
    """Atruvia fresh install: one-step sync rejected (9075 + 3920), no HISYN.

    python-fints selects the two-step mechanism named in 3920; the sync is
    then repeated two-step, which yields the SecureGo+ challenge. That dialog
    must stay open as the standing dialog so ``complete_tan`` continues it.
    """
    bank = _Bank()
    challenge = _challenge()
    rejected = _Dialog(bank, selects="946")
    two_step = _Dialog(bank, challenge=challenge)
    bank._sync_dialogs = [rejected, two_step]

    _, result = _run_init(bank)

    assert result is challenge
    assert bank.sync_dialogs_opened == [rejected, two_step]
    assert isinstance(rejected.init_segments[0], HKSYN3)
    assert isinstance(two_step.init_segments[0], HKSYN3)
    # The bank aborted the rejected dialog (9800); an HKEND would be refused
    # with 9340, which python-fints mistakes for a PIN error and blocks the PIN.
    assert not rejected.ended
    assert rejected.open is False
    assert not two_step.ended
    assert two_step.entered == 1
    assert bank._standing_dialog is two_step
    assert bank.system_id == "0"
    assert bank.get_sepa_accounts_calls == 0


def test_init_system_id_raises_no_tan_mechanism_when_sync_rejected_without_two_step():
    """Rejected sync but nothing selectable -> diagnosable error with the codes."""
    bank = _Bank(sync_dialogs=[_Dialog(None)])
    bank._sync_dialogs[0]._bank = bank
    bank.observed_codes = ["3920", "9075"]

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(NoTanMechanismError) as exc_info,
    ):
        client.init_system_id()

    assert exc_info.value.codes == ("3920", "9075")
    assert len(bank.sync_dialogs_opened) == 1
    assert not bank.sync_dialogs_opened[0].ended
    assert bank.sync_dialogs_opened[0].open is False
    assert bank.standing_dialogs_opened == []


def test_init_system_id_no_tan_mechanism_defaults_to_empty_codes_without_attribute():
    """Exotic client without ``observed_codes``: codes default to ``()``."""
    bank = _Bank(sync_dialogs=[_Dialog(None)])
    bank._sync_dialogs[0]._bank = bank
    del bank.observed_codes

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(NoTanMechanismError) as exc_info,
    ):
        client.init_system_id()

    assert exc_info.value.codes == ()


def test_init_system_id_skips_sync_when_state_already_has_system_id():
    """Restored FinTS state: no HKSYN, straight to the standing dialog."""
    bank = _Bank(system_id="SYS1")

    _, result = _run_init(bank)

    assert result is None
    assert bank.sync_dialogs_opened == []
    assert len(bank.standing_dialogs_opened) == 1
    assert bank.get_sepa_accounts_calls == 1


def test_init_system_id_returns_challenge_from_standing_dialog():
    """System id known, but the fresh dialog itself asks for SCA (0030/3955)."""
    bank = _Bank(system_id="SYS1")
    challenge = _challenge()
    bank._standing_template = _Dialog(bank, challenge=challenge)

    _, result = _run_init(bank)

    assert result is challenge
    assert bank._standing_dialog is bank._standing_template
    assert not bank._standing_template.ended
    assert bank.get_sepa_accounts_calls == 0


def test_init_system_id_loads_bpd_anonymously_when_client_has_none():
    """Empty BPD -> anonymous dialog fetches it, copied onto the login client.

    Without the BPD's HITANS segments python-fints cannot act on the bank's
    3920 (the two-step mechanism list) and the two-step sync can't be built.
    """
    bank = _Bank(bpd_segments=[], sync_dialogs=[_Dialog(None, system_id="SYS1")])
    bank._sync_dialogs[0]._bank = bank
    anonymous = MagicMock()
    anonymous.bpd.segments = ["HITANS"]
    anonymous.bpd_version = 203

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        patch(
            "custom_components.fints_atruvia.api.FinTS3PinTanClient",
            return_value=anonymous,
        ) as ctor,
    ):
        client.init_system_id()

    ctor.assert_called_once_with(
        "12345678", CUSTOMER_ID_ANONYMOUS, None, _URL, product_id=client._product_id
    )
    anonymous.__enter__.assert_called_once()
    anonymous.__exit__.assert_called_once()
    assert bank.bpd is anonymous.bpd
    assert bank.bpa is anonymous.bpa
    assert bank.bpd_version == 203


def test_init_system_id_skips_anonymous_fetch_when_bpd_present():
    bank = _Bank(sync_dialogs=[_Dialog(None, system_id="SYS1")])
    bank._sync_dialogs[0]._bank = bank

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        patch("custom_components.fints_atruvia.api.FinTS3PinTanClient") as ctor,
    ):
        client.init_system_id()

    ctor.assert_not_called()


def test_init_system_id_continues_when_anonymous_bpd_fetch_fails(caplog):
    """The anonymous fetch is best effort: on failure the login proceeds."""
    bank = _Bank(bpd_segments=[], sync_dialogs=[_Dialog(None, system_id="SYS1")])
    bank._sync_dialogs[0]._bank = bank
    anonymous = MagicMock()
    anonymous.__enter__.side_effect = ConnectionError("Verbindung zu bank 12345678 weg")

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        patch(
            "custom_components.fints_atruvia.api.FinTS3PinTanClient",
            return_value=anonymous,
        ),
    ):
        result = client.init_system_id()

    assert result is None
    assert bank.bpd.segments == []
    assert "ConnectionError" in caplog.text
    assert "12345678" not in caplog.text


def test_init_system_id_raises_auth_rejected_error_with_observed_codes():
    """A PIN error during the sync surfaces as AuthRejectedError with codes."""
    pin_error = FinTSClientPINError("Error during dialog initialization, PIN wrong?")
    bank = _Bank()
    bank._sync_dialogs = [_Dialog(bank, raises=pin_error)]
    bank.observed_error_codes = ["9942"]

    client = _client()
    with (
        patch.object(client, "_build_client", return_value=bank),
        pytest.raises(AuthRejectedError) as exc_info,
    ):
        client.init_system_id()

    assert exc_info.value.codes == ("9942",)
    assert exc_info.value.__cause__ is pin_error


# ---------------------------------------------------------------------------
# complete_tan
# ---------------------------------------------------------------------------


def _client_with_open_dialog(bank: _Bank) -> FinTsAtruviaClient:
    """Wrapper whose bank has the (sync) dialog still open, as after a challenge."""
    dialog = _Dialog(bank)
    dialog.init()
    dialog.__enter__()
    bank._standing_dialog = dialog
    client = _client()
    client._client = bank
    return client


def test_complete_tan_returns_pending_challenge_when_app_not_yet_confirmed():
    """3956: the user pressed Weiter before confirming; dialog must stay open."""
    pending = _challenge()
    bank = _Bank(send_tan_result=pending)
    client = _client_with_open_dialog(bank)
    open_dialog = bank._standing_dialog

    result = client.complete_tan(_challenge(), "")

    assert result is pending
    assert bank._standing_dialog is open_dialog
    assert not open_dialog.ended
    assert bank.get_sepa_accounts_calls == 0


def test_complete_tan_adopts_system_id_from_sync_dialog_then_opens_fresh_dialog():
    """Sync dialog: HISYN arrives with the TAN completion; then a normal dialog."""
    bank = _Bank(send_tan_result=_Response(system_id="SYS1"))
    client = _client_with_open_dialog(bank)
    sync_dialog = bank._standing_dialog
    challenge = _challenge()

    result = client.complete_tan(challenge, "")

    assert result is None
    assert bank.send_tan_calls == [(challenge, "")]
    assert bank.system_id == "SYS1"
    assert sync_dialog.ended
    assert len(bank.standing_dialogs_opened) == 1
    assert bank.standing_dialogs_opened[0].ended
    assert bank._standing_dialog is None
    assert bank.get_sepa_accounts_calls == 1
    assert client.get_accounts() == ["acc"]


def test_complete_tan_returns_second_challenge_from_fresh_dialog():
    """After the sync the fresh dialog may ask for SCA again -> hand it out."""
    bank = _Bank(send_tan_result=_Response(system_id="SYS1"))
    second = _challenge()
    bank._standing_template = _Dialog(bank, challenge=second)
    client = _client_with_open_dialog(bank)
    sync_dialog = bank._standing_dialog

    result = client.complete_tan(_challenge(), "")

    assert result is second
    assert sync_dialog.ended
    assert bank._standing_dialog is bank._standing_template
    assert not bank._standing_template.ended
    assert bank.get_sepa_accounts_calls == 0


def test_complete_tan_with_known_system_id_fetches_accounts_in_same_dialog():
    bank = _Bank(system_id="SYS1", send_tan_result=_Response())
    client = _client_with_open_dialog(bank)
    dialog = bank._standing_dialog

    result = client.complete_tan(_challenge(), "")

    assert result is None
    assert bank.get_sepa_accounts_calls == 1
    assert dialog.ended
    assert bank._standing_dialog is None
    assert bank.standing_dialogs_opened == []


def test_complete_tan_raises_when_bank_assigns_no_system_id_after_tan():
    bank = _Bank(send_tan_result=_Response())
    client = _client_with_open_dialog(bank)
    dialog = bank._standing_dialog

    with pytest.raises(ValueError, match="system id"):
        client.complete_tan(_challenge(), "")

    assert dialog.ended
    assert bank._standing_dialog is None
    assert bank.standing_dialogs_opened == []


def test_complete_tan_raises_auth_rejected_error_and_still_closes_dialog():
    pin_error = FinTSClientPINError("Error during dialog initialization, PIN wrong?")
    bank = _Bank(send_tan_result=pin_error)
    bank.observed_error_codes = ["9942"]
    client = _client_with_open_dialog(bank)
    dialog = bank._standing_dialog

    with pytest.raises(AuthRejectedError) as exc_info:
        client.complete_tan(tan_response=_challenge(), tan="")

    assert exc_info.value.codes == ("9942",)
    assert exc_info.value.__cause__ is pin_error
    assert dialog.ended
    assert bank._standing_dialog is None


# ---------------------------------------------------------------------------
# _ExtendedFinTSClient diagnostics
# ---------------------------------------------------------------------------


def _bare_extended_client() -> _ExtendedFinTSClient:
    """Build an ``_ExtendedFinTSClient`` without running python-fints' __init__.

    ``object.__new__`` skips the real constructor (no network/bank setup),
    so the diagnostic attributes it would normally set have to be
    initialised manually here, mirroring what ``__init__`` does.
    """
    instance = object.__new__(_ExtendedFinTSClient)
    instance.observed_error_codes = []
    instance.observed_codes = []
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
    super_mock.assert_called_once_with(dialog, segment, response)


def test_process_response_records_every_code_once_in_order():
    """observed_codes collects every code seen, in order, without duplicates.

    observed_error_codes stays limited to 9xxx codes.
    """
    instance = _bare_extended_client()

    with patch.object(FinTS3PinTanClient, "_process_response"):
        for code in ("3920", "3076", "9075", "3920"):
            instance._process_response(MagicMock(), MagicMock(), MagicMock(code=code))

    assert instance.observed_codes == ["3920", "3076", "9075"]
    assert instance.observed_error_codes == ["9075"]


def test_hitans_versions_lists_sorted_unique_versions():
    seg6 = MagicMock()
    seg6.header.version = 6
    seg7 = MagicMock()
    seg7.header.version = 7
    bank = MagicMock()
    bank.bpd.find_segments.return_value = [seg7, seg6, seg7]

    assert _hitans_versions(bank) == [6, 7]
    bank.bpd.find_segments.assert_called_once_with("HITANS")


def test_hitans_versions_returns_empty_list_when_bpd_access_raises():
    """Exotic client where accessing ``bpd`` itself raises: degrades to ``[]``."""

    class _NoBpd:
        @property
        def bpd(self) -> None:
            msg = "no bpd on this client"
            raise RuntimeError(msg)

    assert _hitans_versions(_NoBpd()) == []


# ---------------------------------------------------------------------------
# open_dialog / close_dialog
# ---------------------------------------------------------------------------


def _client_with_bank(bank: _Bank) -> FinTsAtruviaClient:
    client = _client()
    client._client = bank
    return client


def test_open_dialog_enters_standing_dialog_and_returns_none():
    bank = _Bank(system_id="SYS1")
    client = _client_with_bank(bank)

    assert client.open_dialog() is None

    assert len(bank.standing_dialogs_opened) == 1
    assert bank._standing_dialog is bank.standing_dialogs_opened[0]
    assert not bank._standing_dialog.ended
    assert bank.get_sepa_accounts_calls == 0


def test_open_dialog_returns_init_challenge_and_keeps_dialog_open():
    """3955 at dialog init: python-fints only records it; we must stop here."""
    bank = _Bank(system_id="SYS1")
    challenge = _challenge()
    bank._standing_template = _Dialog(bank, challenge=challenge)
    client = _client_with_bank(bank)

    assert client.open_dialog() is challenge

    assert bank._standing_dialog is bank._standing_template
    assert not bank._standing_template.ended
    assert bank.get_sepa_accounts_calls == 0


def test_open_dialog_is_idempotent_while_dialog_is_open():
    """After complete_tan the authenticated dialog is reused, not reopened."""
    bank = _Bank(system_id="SYS1")
    client = _client_with_bank(bank)
    client.open_dialog()
    dialog = bank._standing_dialog

    assert client.open_dialog() is None

    assert bank._standing_dialog is dialog
    assert len(bank.standing_dialogs_opened) == 1


def test_open_dialog_clears_stale_init_tan_response_before_entering():
    bank = _Bank(system_id="SYS1")
    bank.init_tan_response = _challenge()  # leftover from an earlier dialog
    client = _client_with_bank(bank)

    assert client.open_dialog() is None


def test_open_dialog_raises_auth_rejected_and_drops_client():
    """Wrong PIN at login: report the codes and forget the blocked client."""
    pin_error = FinTSClientPINError("Error during dialog initialization, PIN wrong?")
    bank = _Bank(system_id="SYS1")
    bank._standing_template = _Dialog(bank, raises=pin_error)
    bank.observed_error_codes = ["9942"]
    client = _client_with_bank(bank)

    with pytest.raises(AuthRejectedError) as exc_info:
        client.open_dialog()

    assert exc_info.value.codes == ("9942",)
    assert exc_info.value.__cause__ is pin_error
    assert client._client is None
    assert bank._standing_dialog is None


def test_open_dialog_drops_half_entered_dialog_on_other_errors():
    """A network error during init must not leave a dead standing dialog behind."""
    bank = _Bank(system_id="SYS1")
    boom = ConnectionError("Verbindung weg")
    bank._standing_template = _Dialog(bank, raises=boom)
    client = _client_with_bank(bank)

    with pytest.raises(ConnectionError):
        client.open_dialog()

    assert bank._standing_dialog is None
    assert client._client is bank


def test_close_dialog_ends_standing_dialog_and_keeps_client():
    bank = _Bank(system_id="SYS1")
    client = _client_with_bank(bank)
    client.open_dialog()
    dialog = bank._standing_dialog

    client.close_dialog()

    assert dialog.ended
    assert bank._standing_dialog is None
    assert client._client is bank


def test_close_dialog_is_noop_without_dialog_or_client():
    bank = _Bank(system_id="SYS1")
    client = _client_with_bank(bank)
    client.close_dialog()  # nothing open

    assert bank._standing_dialog is None

    client._client = None
    client.close_dialog()  # nothing built


def test_deconstruct_refreshes_stored_fints_state():
    """A client rebuilt after close() must start from the latest state."""
    bank = _Bank(system_id="SYS1")
    client = _client_with_bank(bank)

    assert client.deconstruct() == b"blob-private"
    assert client._fints_state == b"blob-private"


# ---------------------------------------------------------------------------
# describe_exception_chain
# ---------------------------------------------------------------------------


def test_describe_exception_chain_follows_cause_then_context():
    """The wrapper type alone says nothing; the chain names the real error."""
    try:
        try:
            raise FinTSClientPINError("PIN wrong?")  # noqa: TRY301, TRY003, EM101 - mirrors python-fints
        except FinTSClientPINError:
            raise Exception("Refusing to use PIN after block")  # noqa: TRY002, TRY003, EM101, B904 - mirrors python-fints
    except Exception as outer:  # noqa: BLE001
        wrapped = RuntimeError("Couldn't establish dialog")
        wrapped.__cause__ = outer
        chain = describe_exception_chain(wrapped)

    assert chain == "RuntimeError <- Exception <- FinTSClientPINError"


def test_describe_exception_chain_prefers_explicit_cause_over_context():
    context = ValueError("context")
    cause = KeyError("cause")
    exc = RuntimeError("outer")
    exc.__context__ = context
    exc.__cause__ = cause

    assert describe_exception_chain(exc) == "RuntimeError <- KeyError"


def test_describe_exception_chain_single_exception_is_just_its_name():
    assert describe_exception_chain(ValueError("x")) == "ValueError"


def test_describe_exception_chain_is_cycle_safe_and_depth_capped():
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first  # cycle

    assert describe_exception_chain(first) == "RuntimeError <- RuntimeError"

    head = RuntimeError("0")
    current = head
    for i in range(1, 20):
        nxt = RuntimeError(str(i))
        current.__cause__ = nxt
        current = nxt

    assert len(describe_exception_chain(head).split(" <- ")) == 6


def test_describe_exception_chain_never_includes_messages():
    exc = RuntimeError("DE89370400440532013000 secret text")
    exc.__cause__ = ValueError("1234 pin")

    chain = describe_exception_chain(exc)

    assert "DE89" not in chain
    assert "1234" not in chain
