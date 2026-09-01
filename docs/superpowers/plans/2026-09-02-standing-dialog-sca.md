# Standing Dialog per Poll + SCA at Dialog Init — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The coordinator polls the bank through one standing FinTS dialog per poll, recognises a SecureGo+ challenge demanded at dialog initialisation, and completes it through the existing re-auth button instead of failing, retrying and spamming the user's phone with pushes.

**Architecture:** `FinTsAtruviaClient` (api.py, synchronous python-fints wrapper) gains `open_dialog()` / `close_dialog()`; `complete_tan` keeps the authenticated dialog open so the follow-up fetch happens inside it; `_sync` stops sending HKEND to a bank-aborted sync dialog. `FintsBankingCoordinator` (coordinator.py) wraps its fetch in open/close, maps a rejected login to `ConfigEntryAuthFailed`, drops the python-fints client on any failure, and reports a first-poll challenge as `{}` + pending 2FA instead of `ConfigEntryAuthFailed`. A pure helper `describe_exception_chain` makes wrapped python-fints errors diagnosable without leaking bank text.

**Tech Stack:** Python 3.14, Home Assistant 2026.3.2 custom integration, python-fints 5.0.0, pytest-homeassistant-custom-component (asyncio_mode = auto), ruff 0.16.2 (`select = ALL`).

**Spec:** `docs/superpowers/specs/2026-09-02-standing-dialog-sca-design.md` — read it first; it holds the production traceback analysis every task below argues from.

## Global Constraints

- Run tests with the project venv only: `PYTHONPATH=$PWD .venv/bin/pytest`. Never use the system `python3` (3.10) — the code uses PEP 758 `except A, B:` syntax that only parses on 3.14.
- Lint before every commit: `uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .` (both must be clean; format with `uvx ruff@0.16.2 format .`). Suppress a rule only with a per-line `# noqa: <code> - reason`; do not touch `.ruff.toml`.
- Log hygiene (CLAUDE.md, SECURITY.md §10): never log python-fints exception messages, tracebacks at ERROR, `%r` of bank objects, IBANs or PINs. Type names and bare numeric response codes only. `_LOGGER.debug(..., exc_info=True)` is the sanctioned place for detail.
- `api.py` imports nothing from `homeassistant` or from the rest of the package.
- User-facing strings (notifications, README, CHANGELOG) are German; code comments/docstrings English.
- Commit timestamps must lie outside Mo–Fr 08:00–17:00 Europe/Berlin and stay chronological after `6efeca7` (Di 2026-09-01 22:54 +0200). Every commit step below carries the exact `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` to use — copy them verbatim. Verify at the end with `TZ=Europe/Berlin git log --format='%ad' --date=format-local:'%a %H:%M' | grep -E '^(Mo|Di|Mi|Do|Fr) (0[89]|1[0-6]):'` (must print nothing).
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Do not push. Do not touch `config/` (real credentials). The verify harness sandbox never dials a real bank.
- Baseline before Task 1: `PYTHONPATH=$PWD .venv/bin/pytest -q` → 105 passed; ruff clean.

---

### Task 1: `describe_exception_chain` — make wrapped python-fints errors diagnosable

**Files:**
- Modify: `custom_components/fints_atruvia/api.py` (module-level helper, after `_hitans_versions`, ~line 181)
- Modify: `custom_components/fints_atruvia/coordinator.py:32` (import) and `:352-359` (generic except)
- Modify: `custom_components/fints_atruvia/config_flow.py` — the three generic `except Exception as exc:` logs in `async_step_init_system_id` (~line 285), `async_step_2fa` (~line 334) and `async_step_accounts` (~line 380)
- Test: `tests/test_api.py` (new section at the end)

**Interfaces:**
- Produces: `describe_exception_chain(exc: BaseException) -> str` in `api.py`, e.g. `"FinTSDialogInitError <- Exception <- FinTSClientPINError"`. Task 5 uses it in the coordinator.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
# ---------------------------------------------------------------------------
# describe_exception_chain
# ---------------------------------------------------------------------------


def test_describe_exception_chain_follows_cause_then_context():
    """The wrapper type alone says nothing; the chain names the real error."""
    try:
        try:
            raise FinTSClientPINError("PIN wrong?")
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
```

Add `describe_exception_chain` to the `from custom_components.fints_atruvia.api import (...)` block at the top of `tests/test_api.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py -q -k describe_exception_chain`
Expected: ImportError / `cannot import name 'describe_exception_chain'`.

- [ ] **Step 3: Implement the helper**

In `custom_components/fints_atruvia/api.py`, directly after the `_hitans_versions` function (before `class _ExtendedFinTSClient`), add:

```python
# Longest exception chain worth printing: wrapper -> python-fints error ->
# blocked-PIN Exception -> original response error is four; six leaves room.
_CHAIN_MAX_DEPTH = 6


def describe_exception_chain(exc: BaseException) -> str:
    """Return the exception type names along ``__cause__`` / ``__context__``.

    python-fints wraps unexpected errors in ``FinTSDialogInitError`` and, once
    it has blocked its ``Password`` object after a PIN-error code, replaces the
    ``FinTSClientPINError`` with a bare ``Exception("Refusing to use PIN after
    block")`` while ending the dialog. The outermost type therefore says
    nothing about what happened; the chain does. Type names only — messages
    may quote bank responses (log hygiene, SECURITY.md §10). ``__cause__``
    wins over ``__context__`` (explicit ``raise ... from``), the walk is
    cycle-safe and capped at ``_CHAIN_MAX_DEPTH`` entries.
    """
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if len(names) >= _CHAIN_MAX_DEPTH:
            break
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(names)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py -q -k describe_exception_chain`
Expected: 5 passed.

- [ ] **Step 5: Use it in the coordinator and the config flow**

`custom_components/fints_atruvia/coordinator.py` line 32 becomes:

```python
from .api import (
    FinTsAtruviaClient,
    InvalidUrlError,
    TanRequiredError,
    describe_exception_chain,
)
```

and in `_async_update_data` the generic handler becomes:

```python
        except Exception as err:
            # Bank responses may carry sensitive content in their string
            # form. Log only the exception type chain; the original exception
            # is still chained via ``raise from`` for debug-level traceback.
            _LOGGER.error(  # noqa: TRY400 - log hygiene: no python-fints traceback (SECURITY.md §10)
                "FinTS update failed: %s", describe_exception_chain(err)
            )
            raise UpdateFailed(_MSG_UPDATE_FAILED) from err
```

`custom_components/fints_atruvia/config_flow.py`: extend its `from .api import (...)` block with `describe_exception_chain`, and replace `type(exc).__name__` with `describe_exception_chain(exc)` in exactly these three log calls (keep their `# noqa` comments):

```python
                "FinTS system-ID init failed: %s", describe_exception_chain(exc)
```
```python
                "TAN challenge failed: %s", describe_exception_chain(exc)
```
```python
                "Account fetch failed: %s", describe_exception_chain(exc)
```

Do not touch the `AuthRejectedError` / `NoTanMechanismError` handlers (they log codes).

- [ ] **Step 6: Full suite + lint**

Run: `PYTHONPATH=$PWD .venv/bin/pytest -q && uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .`
Expected: 110 passed, ruff clean. If `format --check` complains, run `uvx ruff@0.16.2 format .` and re-check.

- [ ] **Step 7: Commit**

```bash
git add custom_components/fints_atruvia/api.py custom_components/fints_atruvia/coordinator.py custom_components/fints_atruvia/config_flow.py tests/test_api.py
GIT_AUTHOR_DATE="2026-09-01 23:05:00 +0200" GIT_COMMITTER_DATE="2026-09-01 23:05:00 +0200" \
git commit -m "fix(log): name the exception chain when a FinTS call fails

python-fints wraps unexpected errors (FinTSDialogInitError) and its blocked
Password object replaces a FinTSClientPINError with a bare Exception, so the
outermost type name in our error logs said nothing. Log the chain of type
names instead (type names only, no bank text).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `_sync` — never HKEND a sync dialog the bank has already aborted

**Files:**
- Modify: `custom_components/fints_atruvia/api.py` `_sync` (~lines 359-385)
- Test: `tests/test_api.py` — `test_init_system_id_repeats_sync_two_step_when_bank_demands_sca`, `test_init_system_id_raises_no_tan_mechanism_when_sync_rejected_without_two_step`, `test_init_system_id_no_tan_mechanism_defaults_to_empty_codes_without_attribute`

**Interfaces:**
- Consumes: the tests' `_Dialog` fake (`ended` flag, arbitrary attributes allowed).
- Produces: after a rejected sync the dialog has `open is False` and `ended is False`; after an accepted sync (HISYN) `ended is True`.

- [ ] **Step 1: Update the tests to the new contract**

In `test_init_system_id_repeats_sync_two_step_when_bank_demands_sca` replace

```python
    assert rejected.ended
```
with
```python
    # The bank aborted the rejected dialog (9800); an HKEND would be refused
    # with 9340, which python-fints mistakes for a PIN error and blocks the PIN.
    assert not rejected.ended
    assert rejected.open is False
```

In `test_init_system_id_raises_no_tan_mechanism_when_sync_rejected_without_two_step` replace

```python
    assert bank.sync_dialogs_opened[0].ended
```
with
```python
    assert not bank.sync_dialogs_opened[0].ended
    assert bank.sync_dialogs_opened[0].open is False
```

Add a new test right after `test_init_system_id_one_step_sync_assigns_system_id_and_caches_accounts` (its assertion `assert sync.ended` already covers the accepted path — keep it):

```python
def test_sync_rejected_dialog_is_dropped_without_hkend():
    """No HISYN, no challenge: drop the dialog instead of sending HKEND."""
    bank = _Bank()
    rejected = _Dialog(bank)  # answers neither HISYN nor a challenge
    bank._sync_dialogs = [rejected]

    result = FinTsAtruviaClient._sync(bank)

    assert result is None
    assert not rejected.ended
    assert rejected.open is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py -q -k "sync"`
Expected: the three touched tests fail on `assert not ... .ended` (the current code calls `dialog.end()`).

- [ ] **Step 3: Change `_sync`**

Replace the tail of `_sync` (from `if client.init_tan_response is not None:` to the end of the method) with:

```python
        if client.init_tan_response is not None:
            dialog.__enter__()
            client._standing_dialog = dialog  # noqa: SLF001 - see above
            return client.init_tan_response
        if seg is None:
            # Rejected sync (9075): the bank has already aborted this dialog
            # (9800). An HKEND is refused with 9340, which python-fints lists
            # among the PIN-error codes — it blocks its Password object and
            # every later signature dies with "Refusing to use PIN after
            # block" (surfacing as FinTSDialogInitError from the next
            # dialog.init). Drop the dialog like python-fints does for a 9075
            # on its standing dialog.
            dialog.open = False
            return None
        try:
            dialog.end()
        except Exception:
            _LOGGER.debug("Ending the sync dialog failed", exc_info=True)
        return None
```

Update the `_sync` docstring's last sentence to: `python-fints' own ``_ensure_system_id`` is unusable for SCA banks: it ends the dialog before anyone could answer the challenge. A rejected sync is dropped without HKEND — the bank has aborted it already.`

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py -q`
Expected: all pass (the new test included).

- [ ] **Step 5: Full suite + lint, then commit**

Run: `PYTHONPATH=$PWD .venv/bin/pytest -q && uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .`
Expected: 111 passed, ruff clean.

```bash
git add custom_components/fints_atruvia/api.py tests/test_api.py
GIT_AUTHOR_DATE="2026-09-01 23:20:00 +0200" GIT_COMMITTER_DATE="2026-09-01 23:20:00 +0200" \
git commit -m "fix(api): drop a rejected sync dialog instead of sending HKEND

After a 9075 rejection the bank has already aborted the dialog; the HKEND we
sent anyway came back as 9340, which python-fints treats as a PIN error and
blocks the PIN object. The following two-step sync then failed while signing,
surfacing as an opaque FinTSDialogInitError in the config flow.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `open_dialog()` / `close_dialog()` on `FinTsAtruviaClient`

**Files:**
- Modify: `custom_components/fints_atruvia/api.py` — new public methods after `complete_tan` (before `get_accounts`), and `deconstruct` (~line 308)
- Test: `tests/test_api.py` — `_Bank` fake gets `deconstruct`; new section "open_dialog / close_dialog"

**Interfaces:**
- Consumes: `FinTS3PinTanClient.__enter__/__exit__`, `init_tan_response`, `_standing_dialog` (python-fints), `self._close_standing_dialog()`, `self.close()`, `AuthRejectedError`.
- Produces (used by Task 5):
  - `FinTsAtruviaClient.open_dialog(self) -> NeedTANResponse | None` — idempotent; challenge ⇒ dialog left open; raises `AuthRejectedError` and drops the python-fints client on PIN/temporary-auth errors.
  - `FinTsAtruviaClient.close_dialog(self) -> None` — HKEND if a standing dialog is open, no-op otherwise, never raises.
  - `deconstruct()` additionally updates `self._fints_state`.

- [ ] **Step 1: Extend the `_Bank` fake and write the failing tests**

In `tests/test_api.py` add to `class _Bank` (after `send_tan`):

```python
    def deconstruct(self, including_private=False):  # noqa: FBT002 - mirrors python-fints' signature
        return b"blob-private" if including_private else b"blob"
```

Append a new section (before the `describe_exception_chain` section from Task 1):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py -q -k "open_dialog or close_dialog or deconstruct_refreshes"`
Expected: AttributeError `'FinTsAtruviaClient' object has no attribute 'open_dialog'` etc.; the deconstruct test fails on `_fints_state`.

- [ ] **Step 3: Implement**

In `api.py`, in `deconstruct`, replace

```python
        try:
            return self._client.deconstruct(including_private=True)
        except Exception:
            _LOGGER.debug("Failed to deconstruct FinTS state", exc_info=True)
            return None
```
with
```python
        try:
            blob = self._client.deconstruct(including_private=True)
        except Exception:
            _LOGGER.debug("Failed to deconstruct FinTS state", exc_info=True)
            return None
        # A client rebuilt after close() (the coordinator does that after a
        # failed poll) must start from this state, not from the blob loaded
        # at construction.
        self._fints_state = blob
        return blob
```

Add after `complete_tan` (before `def get_accounts`):

```python
    def open_dialog(self) -> NeedTANResponse | None:
        """Open the standing dialog for a poll, or report the SCA it demands.

        Idempotent: a dialog left open by :meth:`complete_tan` is reused so
        the fetch after a confirmed challenge runs in the dialog the user
        authenticated — a fresh one could demand SCA all over again.

        Returns the challenge if the bank asks for SCA at dialog
        initialisation (response 3955/0030 to the HKTAN sent with HKIDN);
        the dialog then stays open and the caller MUST answer it through
        :meth:`complete_tan` before any job. python-fints itself only records
        that challenge in ``init_tan_response`` and would send the jobs
        anyway; Atruvia refuses them (9340), which python-fints then mistakes
        for a wrong PIN. Returns ``None`` when the dialog is open and
        authenticated.

        Raises ``AuthRejectedError`` if the bank rejects the login. The
        python-fints client is dropped first: it has blocked its PIN object,
        so the next attempt needs a fresh one (see :meth:`close`).
        """
        client = self._get_client()
        if client._standing_dialog is not None:  # noqa: SLF001 - no public accessor
            return None
        client.init_tan_response = None
        try:
            client.__enter__()
        except (FinTSClientPINError, FinTSClientTemporaryAuthError) as exc:
            codes = tuple(getattr(client, "observed_error_codes", ()))
            self.close()
            raise AuthRejectedError(codes=codes) from exc
        return client.init_tan_response

    def close_dialog(self) -> None:
        """End the standing dialog (HKEND) if one is open; the client stays usable."""
        self._close_standing_dialog()
```

Note `self.close()` calls `_close_standing_dialog()` first, which handles the half-entered client (`_standing_dialog` set, dialog never opened → no HKEND goes out) and then drops `self._client`.

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite + lint, then commit**

Run: `PYTHONPATH=$PWD .venv/bin/pytest -q && uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .`
Expected: 119 passed, ruff clean.

```bash
git add custom_components/fints_atruvia/api.py tests/test_api.py
GIT_AUTHOR_DATE="2026-09-01 23:35:00 +0200" GIT_COMMITTER_DATE="2026-09-01 23:35:00 +0200" \
git commit -m "feat(api): open_dialog/close_dialog for one standing dialog per poll

open_dialog enters the python-fints client once, reports an SCA challenge
demanded at dialog initialisation (init_tan_response) instead of letting
python-fints send jobs into an unauthenticated dialog, and turns a rejected
login into AuthRejectedError while dropping the client with its blocked PIN
object. deconstruct() now also refreshes the stored state blob so a rebuilt
client starts from it.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `complete_tan` and the bootstrap keep the authenticated dialog open

**Files:**
- Modify: `custom_components/fints_atruvia/api.py` — `_open_dialog_and_cache_accounts` (~line 387) and `complete_tan` (~line 500)
- Modify: `custom_components/fints_atruvia/config_flow.py` `_finish_setup` comment (~line 429)
- Test: `tests/test_api.py` — four existing tests change expectations

**Interfaces:**
- Consumes: Task 3's `close_dialog()`.
- Produces: after `complete_tan(...) is None` and after `init_system_id() is None` the python-fints standing dialog is still open; callers end it (`close_dialog()` — coordinator, Task 5; `close()` — config flow, already the case in `_finish_setup`, the retry path and `async_remove`).

- [ ] **Step 1: Update the tests to the new contract**

`test_init_system_id_one_step_sync_assigns_system_id_and_caches_accounts`: replace

```python
    assert bank._standing_dialog is None
    assert bank.get_sepa_accounts_calls == 1
    assert client.get_accounts() == ["acc"]
```
with
```python
    # The account dialog stays open for the caller (config flow closes the
    # client in _finish_setup); a fresh dialog could demand SCA again.
    assert bank._standing_dialog is bank.standing_dialogs_opened[0]
    assert not bank._standing_dialog.ended
    assert bank.get_sepa_accounts_calls == 1
    assert client.get_accounts() == ["acc"]
    client.close_dialog()
    assert bank.standing_dialogs_opened[0].ended
    assert bank._standing_dialog is None
```

`test_init_system_id_skips_sync_when_state_already_has_system_id`: add at the end

```python
    assert bank._standing_dialog is bank.standing_dialogs_opened[0]
```

`test_complete_tan_adopts_system_id_from_sync_dialog_then_opens_fresh_dialog`: replace

```python
    assert bank.standing_dialogs_opened[0].ended
    assert bank._standing_dialog is None
```
with
```python
    assert not bank.standing_dialogs_opened[0].ended
    assert bank._standing_dialog is bank.standing_dialogs_opened[0]
```

`test_complete_tan_with_known_system_id_fetches_accounts_in_same_dialog`: replace

```python
    assert dialog.ended
    assert bank._standing_dialog is None
    assert bank.standing_dialogs_opened == []
```
with
```python
    # Stay in the dialog the user just authenticated: the coordinator fetches
    # balances and transactions here and ends it afterwards.
    assert not dialog.ended
    assert bank._standing_dialog is dialog
    assert bank.standing_dialogs_opened == []
```

Add one new test after `test_complete_tan_with_known_system_id_fetches_accounts_in_same_dialog`:

```python
def test_open_dialog_and_cache_accounts_closes_dialog_when_account_fetch_raises():
    bank = _Bank(system_id="SYS1")
    boom = RuntimeError("HKSPA failed")
    bank.get_sepa_accounts = MagicMock(side_effect=boom)
    client = _client_with_bank(bank)

    with pytest.raises(RuntimeError):
        client._open_dialog_and_cache_accounts(bank)

    assert bank.standing_dialogs_opened[0].ended
    assert bank._standing_dialog is None
```

(`_client_with_bank` is defined in Task 3's section; move that helper up next to `_client_with_open_dialog` if the section order makes it undefined at this point.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py -q`
Expected: the four updated tests and the new one fail (dialogs are currently ended / the new test's expectation of an ended dialog fails only if the finally-close is removed — it passes today; that is fine, it guards the exception path you are about to rewrite).

- [ ] **Step 3: Change `_open_dialog_and_cache_accounts` and `complete_tan`**

Replace `_open_dialog_and_cache_accounts` with:

```python
    def _open_dialog_and_cache_accounts(
        self, client: FinTS3PinTanClient
    ) -> NeedTANResponse | None:
        """Open the standing dialog and cache the accounts, unless SCA comes first.

        Returns the challenge or ``None``; either way the dialog stays open
        for the caller (see :meth:`complete_tan`). Only a failed account
        fetch closes it.
        """
        client.init_tan_response = None
        client.__enter__()
        if client.init_tan_response is not None:
            return client.init_tan_response
        try:
            self._cached_accounts = client.get_sepa_accounts()
        except Exception:
            self._close_standing_dialog()
            raise
        return None
```

In `complete_tan`, replace

```python
            if client.system_id != SYSTEM_ID_UNASSIGNED:
                self._cached_accounts = client.get_sepa_accounts()
                self._close_standing_dialog()
                return None
```
with
```python
            if client.system_id != SYSTEM_ID_UNASSIGNED:
                # Stay in this dialog: it is the one the user just
                # authenticated. A fresh dialog can demand SCA all over again
                # (the bank grants the 90-day exemption per client, and not
                # necessarily right away), so the caller fetches its data here
                # and ends the dialog afterwards via close_dialog() / close().
                self._cached_accounts = client.get_sepa_accounts()
                return None
```

Update the `complete_tan` docstring paragraph starting `Returns ``None`` once the login is complete` to:

```
        Returns ``None`` once the login is complete: the accounts are cached
        and the standing dialog is left open and authenticated for the
        caller's jobs (end it with :meth:`close_dialog` or :meth:`close`).
        Returns a new ``NeedTANResponse`` when more confirmation is needed: ...
```
(keep the rest of that paragraph unchanged.)

Update the `init_system_id` docstring sentence `Returns ``None`` when no SCA was required (accounts cached, dialog closed)` to `Returns ``None`` when no SCA was required (accounts cached, dialog left open for the caller)`.

In `config_flow.py` `_finish_setup`, change the leading comment to:

```python
        # The FinTS dialog's last bank access (get_accounts / complete_tan)
        # already happened before this step runs — take the state python-fints
        # built up (system id, TAN mechanism, BPD/UPD) and release the client
        # (ending the dialog it still holds open, and dropping the PIN
        # python-fints holds for it) now, in both the reauth and the new-entry
        # branch below, rather than leaving it referenced on the flow object
        # until HA discards it.
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_api.py tests/test_config_flow.py -q`
Expected: all pass (config-flow tests mock the whole client and are unaffected).

- [ ] **Step 5: Full suite + lint, then commit**

Run: `PYTHONPATH=$PWD .venv/bin/pytest -q && uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .`
Expected: 120 passed, ruff clean.

```bash
git add custom_components/fints_atruvia/api.py custom_components/fints_atruvia/config_flow.py tests/test_api.py
GIT_AUTHOR_DATE="2026-09-01 23:50:00 +0200" GIT_COMMITTER_DATE="2026-09-01 23:50:00 +0200" \
git commit -m "fix(api): keep the authenticated dialog open after complete_tan

A bank that demands SCA at dialog initialisation does so for every new
dialog until it grants the exemption; closing the dialog right after the
TAN and reopening one for the data fetch therefore loops on SecureGo+
prompts. complete_tan and the bootstrap now leave the standing dialog open;
the caller ends it (coordinator: close_dialog, config flow: close).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Coordinator — one dialog per poll, init-SCA via the re-auth button, no retry storm on a rejected login

**Files:**
- Modify: `custom_components/fints_atruvia/coordinator.py` — imports (~line 32), constants (~line 55-61), `_async_update_data` (~line 283-385), `async_complete_reauth` (~line 394-413), new static helper
- Test: `tests/test_coordinator.py` — `_make_coordinator`, `test_tan_required_before_first_success_raises_auth_failed` (rewritten), `test_complete_reauth_*` (refresh method), new tests

**Interfaces:**
- Consumes: `FinTsAtruviaClient.open_dialog() -> NeedTANResponse | None`, `close_dialog()`, `close()`, `AuthRejectedError(codes)`, `describe_exception_chain` (Tasks 1, 3, 4).
- Produces: `FintsBankingCoordinator._open_dialog(client) -> None` (static, blocking, raises `TanRequiredError`); `_async_update_data` returns `{}` on a first-poll challenge; `AuthRejectedError` → `ConfigEntryAuthFailed`; `async_complete_reauth` awaits `self.async_refresh()`.

- [ ] **Step 1: Update the test scaffolding and write the failing tests**

In `tests/test_coordinator.py`:

Imports: extend `from custom_components.fints_atruvia.api import TanRequiredError` to
```python
from custom_components.fints_atruvia.api import AuthRejectedError, TanRequiredError
```
and add `from homeassistant.helpers.update_coordinator import UpdateFailed` next to the `ConfigEntryAuthFailed` import.

In `_make_coordinator`, after `coordinator._client.deconstruct.return_value = None`, add:

```python
    # A MagicMock return value is truthy and would read as an SCA challenge.
    coordinator._client.open_dialog.return_value = None
```

Rewrite `test_tan_required_before_first_success_raises_auth_failed` as:

```python
async def test_tan_required_before_first_success_returns_empty_data(hass):
    """First poll with an SCA challenge: expose the entities, keep 2FA pending.

    Raising ConfigEntryAuthFailed here would send the user through the reauth
    flow, whose own TAN completion reloads the entry — and the first poll would
    demand SCA again for a bank without exemption (endless loop). With ``{}``
    the sensors exist (unknown, ``2fa_pending``), the button is available and
    the confirmation completes in the dialog that is still open.
    """
    coordinator = _make_coordinator(hass, [_IBAN1])
    challenge = SimpleNamespace(name="init challenge")
    coordinator._client.open_dialog.return_value = challenge

    result = await coordinator._async_update_data()

    assert result == {}
    assert coordinator.is_2fa_pending is True
    assert coordinator._tan_response is challenge
    coordinator._client.get_accounts.assert_not_called()
    coordinator._client.close_dialog.assert_not_called()
    coordinator._client.close.assert_not_called()
```

Add new tests after `test_tan_required_with_existing_data_returns_last_good`:

```python
async def test_successful_poll_opens_one_dialog_and_closes_it_after_the_fetch(hass):
    coordinator = _make_coordinator(hass, [_IBAN1])
    coordinator._seen_initialised = True
    coordinator._client.get_accounts.return_value = [_account(_IBAN1)]
    coordinator._client.get_balance.return_value = _balance()
    coordinator._client.get_transactions.return_value = []
    manager = MagicMock()
    manager.attach_mock(coordinator._client.open_dialog, "open_dialog")
    manager.attach_mock(coordinator._client.get_accounts, "get_accounts")
    manager.attach_mock(coordinator._client.close_dialog, "close_dialog")

    result = await coordinator._async_update_data()

    assert _IBAN1 in result
    names = [c[0] for c in manager.mock_calls]
    assert names == ["open_dialog", "get_accounts", "close_dialog"]
    coordinator._client.close.assert_not_called()


async def test_job_level_tan_request_keeps_dialog_open(hass):
    """A challenge from get_balance must not close the dialog complete_tan needs."""
    coordinator = _make_coordinator(hass, [_IBAN1])
    coordinator.data = {"prior": "state"}
    coordinator._client.get_accounts.return_value = [_account(_IBAN1)]
    coordinator._client.get_balance.side_effect = TanRequiredError(SimpleNamespace())

    result = await coordinator._async_update_data()

    assert result is coordinator.data
    assert coordinator.is_2fa_pending is True
    coordinator._client.close_dialog.assert_not_called()
    coordinator._client.close.assert_not_called()


async def test_rejected_login_raises_auth_failed_and_drops_client(hass, caplog):
    """Wrong PIN: no retry loop (the bank locks after a few attempts)."""
    coordinator = _make_coordinator(hass, [_IBAN1])
    coordinator.data = {"prior": "state"}
    coordinator._client.open_dialog.side_effect = AuthRejectedError(codes=("9942",))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    coordinator._client.close.assert_called_once()
    assert "9942" in caplog.text
    assert coordinator.is_2fa_pending is False


async def test_generic_failure_drops_client_and_logs_exception_chain(hass, caplog):
    """python-fints' blocked-PIN Exception must not survive into the next poll."""
    coordinator = _make_coordinator(hass, [_IBAN1])
    blocked = Exception("Refusing to use PIN after block")
    blocked.__cause__ = RuntimeError("PIN wrong?")
    coordinator._client.get_accounts.side_effect = blocked

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    coordinator._client.close.assert_called_once()
    coordinator._client.close_dialog.assert_not_called()
    assert "FinTS update failed: Exception <- RuntimeError" in caplog.text
    assert "Refusing" not in caplog.text
```

In `test_complete_reauth_keeps_pending_when_bank_still_waits` and `test_complete_reauth_clears_pending_and_refreshes_on_success` replace every `async_request_refresh` with `async_refresh` (the mock assignment and the assertions), e.g.:

```python
    coordinator.async_refresh = AsyncMock()
    ...
    coordinator.async_refresh.assert_not_awaited()
```
and
```python
    coordinator.async_refresh.assert_awaited_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_coordinator.py -q`
Expected: the rewritten first-poll test fails (ConfigEntryAuthFailed raised), the four new tests fail (`open_dialog`/`close_dialog`/`close` not called, `AuthRejectedError` falls into `UpdateFailed`), the two reauth tests fail on `async_refresh`.

- [ ] **Step 3: Implement the coordinator changes**

Imports (`coordinator.py` ~line 32):

```python
from .api import (
    AuthRejectedError,
    FinTsAtruviaClient,
    InvalidUrlError,
    TanRequiredError,
    describe_exception_chain,
)
```

Constants: remove `_MSG_INITIAL_TAN_REQUIRED` and add

```python
_MSG_AUTH_REJECTED = "Bank rejected the stored credentials — please re-authenticate."
```

Add a static helper right above `_async_update_data`:

```python
    @staticmethod
    def _open_dialog(client: FinTsAtruviaClient) -> None:
        """Open the poll's standing dialog (blocking; run in the executor).

        Raises ``TanRequiredError`` when the bank demands SCA at dialog
        initialisation, so the challenge takes the same path as one raised
        by a job: notification, ``is_2fa_pending``, re-auth button. The
        dialog stays open for ``complete_tan``.
        """
        challenge = client.open_dialog()
        if challenge is not None:
            raise TanRequiredError(challenge)
```

Replace `_async_update_data` from `if self._client is None:` down to (and including) the generic `except Exception as err:` block with:

```python
        if self._client is None:
            raise ConfigEntryAuthFailed(_MSG_CLIENT_NOT_INITIALISED)
        client = self._client

        try:
            # One standing dialog per poll. Without it python-fints opens a
            # dialog per call, and a bank that demands SCA at dialog init
            # (3955) gets its challenge ignored and its jobs refused (9340).
            await self.hass.async_add_executor_job(self._open_dialog, client)
            accounts = await self.hass.async_add_executor_job(client.get_accounts)
            for iban in self._selected_accounts:
                account = next((a for a in accounts if a.iban == iban), None)
                if account is None:
                    # Masked: this fires at WARNING (default log level) for a
                    # perfectly ordinary cause — a closed or renumbered
                    # account — so the full IBAN must not land in the log.
                    _LOGGER.warning(
                        "Account %s not found at bank, skipping",
                        _mask_iban_for_event(iban),
                    )
                    continue

                balance_data = await self.hass.async_add_executor_job(
                    client.get_balance, account
                )
                transactions = await self.hass.async_add_executor_job(
                    client.get_transactions, account, STATS_WINDOW_DAYS
                )

                stats = _compute_stats(transactions)
                events, seen_snapshot = self._detect_new_transactions(
                    iban, transactions
                )
                new_events.extend(events)
                pending_seen[iban] = seen_snapshot

                result[iban] = {
                    "iban": iban,
                    "balance": balance_data["balance"],
                    "currency": balance_data["currency"],
                    "available_balance": balance_data["available_balance"],
                    "balance_pending": balance_data["balance_pending"],
                    "pending_amount": balance_data["pending_amount"],
                    "booking_date": balance_data["booking_date"],
                    "transactions": transactions,
                    "stats": stats,
                }
            # One HKEND per poll; also ends the dialog complete_tan left open.
            await self.hass.async_add_executor_job(client.close_dialog)
        except TanRequiredError as e:
            # Dialog stays open: complete_tan continues it after the user
            # confirmed in the app and pressed the button.
            self._tan_response = e.response
            self.is_2fa_pending = True
            pn_async_create(
                self.hass,
                title="FinTS Atruvia",
                message=(
                    "Sparda-Bank fordert Re-Authentifizierung. "
                    "Bitte den Re-Auth-Button in der Integration drücken."
                ),
                notification_id="fints_atruvia_reauth",
            )
            if self.data is None:
                # First poll (fresh setup or restart) and the bank wants SCA
                # first: expose the entities anyway so the re-auth button is
                # there to press. Sensors report unknown with 2fa_pending
                # until the confirmation went through. Raising
                # ConfigEntryAuthFailed here would loop: the reauth flow's
                # own TAN reloads the entry, whose first poll asks again.
                return {}
            # Intentionally return last known complete data rather than a
            # partial result. Fresh data for already-processed IBANs is
            # discarded to avoid partial state.
            return self.data
        except AuthRejectedError as err:
            # Wrong or changed PIN: never retry — Atruvia locks the access
            # after a few failed logins. The wrapper already dropped the
            # python-fints client (blocked PIN object); hand over to HA's
            # reauth flow for new credentials.
            await self.hass.async_add_executor_job(client.close)
            _LOGGER.error(  # noqa: TRY400 - log hygiene: bare response codes only, no bank text (SECURITY.md §10)
                "FinTS login rejected by bank (codes: %s)",
                ",".join(err.codes) or "none",
            )
            raise ConfigEntryAuthFailed(_MSG_AUTH_REJECTED) from err
        except CredentialStoreError as err:
            # Decryption failed — typically because master key file was lost.
            # Surface via reauth so the user can re-enter the PIN.
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            # Drop the python-fints client: after a mid-dialog rejection its
            # PIN object may be blocked and the dialog is dead either way. The
            # next poll rebuilds it from the stored FinTS state. Log only the
            # exception type chain (bank text may hide in messages); the
            # original exception is still chained via ``raise from``.
            await self.hass.async_add_executor_job(client.close)
            _LOGGER.error(  # noqa: TRY400 - log hygiene: no python-fints traceback (SECURITY.md §10)
                "FinTS update failed: %s", describe_exception_chain(err)
            )
            raise UpdateFailed(_MSG_UPDATE_FAILED) from err
```

Leave the success tail (`was_uninitialised = ...` through `return result`) unchanged.

In `async_complete_reauth` replace `await self.async_request_refresh()` with `await self.async_refresh()` and extend the docstring:

```python
        """Complete pending re-authentication after the user confirms SecureGo+.

        Returns ``False`` if the bank still waits for the app confirmation
        (``complete_tan`` handed back a follow-up challenge); the 2FA state
        then stays pending and the button can be pressed again. On success
        the refresh runs immediately, inside the dialog the user just
        authenticated — a debounced request could outlive it.
        """
```

Update the `async_init` comment block that says `rebuilding the client on every poll — at the cost of repeated SCA prompts` to end with: `rebuilding the client on every poll — which for a bank without SCA exemption means a SecureGo+ prompt per poll.` (wording only).

- [ ] **Step 4: Run the coordinator tests**

Run: `PYTHONPATH=$PWD .venv/bin/pytest tests/test_coordinator.py -q`
Expected: all pass, including the untouched event-loss tests (they exercise `get_balance` raising `TanRequiredError` inside the loop).

- [ ] **Step 5: Full suite + lint, then commit**

Run: `PYTHONPATH=$PWD .venv/bin/pytest -q && uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .`
Expected: 124 passed, ruff clean. If ruff flags `PLR0915` (too many statements) on `_async_update_data`, add `# noqa: PLR0915 - one poll = open, fetch, close, four error mappings` to the `async def _async_update_data(self) -> dict:` line rather than splitting the method.

```bash
git add custom_components/fints_atruvia/coordinator.py tests/test_coordinator.py
GIT_AUTHOR_DATE="2026-09-02 00:10:00 +0200" GIT_COMMITTER_DATE="2026-09-02 00:10:00 +0200" \
git commit -m "fix(coordinator): one standing dialog per poll, SCA at init via button

The coordinator let python-fints open a dialog per call. For a login the
bank does not exempt from SCA every dialog init came back with a SecureGo+
challenge (3955) that python-fints only recorded before sending the jobs
anyway; the bank refused them (9340), python-fints blocked the PIN, the
dialog teardown died with a bare Exception, HA retried with backoff and each
retry pushed the user's phone again.

- open one dialog per poll (open_dialog/close_dialog), report an init
  challenge as TanRequiredError -> notification + re-auth button, and run
  the refresh after complete_tan inside the still-authenticated dialog
- first poll with a challenge returns {} instead of ConfigEntryAuthFailed:
  the reauth flow would reload into the same challenge
- AuthRejectedError -> ConfigEntryAuthFailed (no retry storm on a wrong
  PIN), any other failure drops the python-fints client with its possibly
  blocked PIN object

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Verify harness — model SCA at dialog init, run the lifecycle against the sandbox

**Files:**
- Modify: `.claude/skills/verify/harness/fake_fints/sitecustomize.py` — module docstring flag list, `FakeFinTS3PinTanClient.__enter__/__exit__`, data methods
- Modify: `.claude/skills/verify/SKILL.md` — flag table, "Flows worth driving"
- Create: `docs/verification-2026-09-02.md`

**Interfaces:**
- Consumes: the coordinator behaviour from Task 5.
- Produces: flags `initsca` (bank demands SCA at dialog init) and `pinerror` applying to `__enter__`; `fakebank.log` lines `DIALOG_OPEN init_sca=yes|no`, `DIALOG_END`, `INIT_SCA`.

- [ ] **Step 1: Extend the fake bank**

In `sitecustomize.py`, module docstring flag list — add after the `pinerror` line:

```
                          D/initsca           bank demands SCA at every dialog init (3955) while set —
                                              Sparda BW in production; data calls outside a standing
                                              dialog raise FinTSClientPINError (models 9340 -> PIN error)
```
and extend the `pinerror` description to `bank rejects dialog init with code 9942 (wrong PIN/locked access) — config flow and coordinator (__enter__)`.

Replace `__enter__` / `__exit__` of `FakeFinTS3PinTanClient` with:

```python
    def __enter__(self):
        if _flag("pinerror"):
            self.observed_error_codes.append("9942")
            _log("DIALOG_OPEN rejected=9942")
            msg = "Anmeldung abgelehnt (Fake-Bank pinerror flag)"
            raise FinTSClientPINError(msg)
        dialog = _Dialog(self)
        dialog.open = True
        dialog.__enter__()
        self._standing_dialog = dialog
        init_sca = _flag("initsca")
        if init_sca:
            # 3955 at dialog init: the challenge is recorded, nothing else —
            # exactly what the real python-fints does.
            self.init_tan_response = self._need_tan()
            _log("INIT_SCA")
        _log(f"DIALOG_OPEN init_sca={'yes' if init_sca else 'no'}")
        return self

    def __exit__(self, *exc):
        self._standing_dialog.__exit__(*exc)
        self._standing_dialog = None
        _log("DIALOG_END")
        return False

    def _require_authenticated_dialog(self):
        """Model the bank refusing jobs outside an authenticated dialog.

        With ``initsca`` set, python-fints without a standing dialog would
        open one per call, get the 3955 challenge, send the job anyway and
        receive 9340 — which it classifies as a PIN error. Raise that here so
        a regression to per-call dialogs fails loudly instead of returning
        data.
        """
        if _flag("initsca") and (
            self._standing_dialog is None or self.init_tan_response is not None
        ):
            self.observed_error_codes.append("9340")
            msg = "Auftrag abgelehnt (Fake-Bank initsca: dialog not authenticated)"
            raise FinTSClientPINError(msg)
```

Add `self.observed_error_codes = []` and `self.observed_codes = []` to `__init__` if they are not already there (they are — keep them), and call `self._require_authenticated_dialog()` as the first line of `get_sepa_accounts`, `get_balance` and `get_transactions`.

`send_tan` must clear the init challenge like the real library's dialog continuation does — the wrapper sets `client.init_tan_response = None` itself after a successful `send_tan`, so no change is needed there; just make sure `send_tan` keeps returning `_Response()` when `system_id` is known.

- [ ] **Step 2: Document the flags in SKILL.md**

Flag table: add

```
| `initsca` | bank demands SCA at every dialog init (3955) while set — the Sparda BW behaviour; with a standing dialog the coordinator reports the challenge (notification + button) and, after the button, fetches in the same dialog. Any data call outside an authenticated dialog raises `FinTSClientPINError` (the real bank's 9340), so a regression to per-call dialogs fails the poll |
```
and change the `pinerror` row to `bank rejects dialog init with code 9942 → \`FinTSClientPINError\` → \`AuthRejectedError\` in \`api.py\` → config-flow error key \`invalid_auth\`; on a coordinator poll → \`ConfigEntryAuthFailed\`, entry \`setup_error\` + reauth flow, no retries`.

"Flows worth driving": add after the **SCA lifecycle** bullet:

```
- **SCA at dialog init** — `touch flags/initsca`, poll: `fakebank.log` shows
  `DIALOG_OPEN init_sca=yes` + `INIT_SCA`, persistent notification, button
  available, sensor keeps its value with `2fa_pending: true`. Press the button
  **with the flag still set**: `SEND_TAN` follows, then the poll succeeds and
  `DIALOG_END` appears with **no** further `DIALOG_OPEN` in between (same
  dialog). Then restart HA with the flag set: entry `loaded` (not
  `setup_error`/`setup_retry`), sensors `unknown`, button available, one
  `INIT_SCA` only — no retry storm; press the button → values appear.
- **Rejected login on a poll** — `touch flags/pinerror`, reload the entry:
  `setup_error`, a `reauth_confirm` flow, exactly one `DIALOG_OPEN
  rejected=9942` (no retries).
```

- [ ] **Step 3: Run the harness**

```bash
H=.claude/skills/verify/harness
export SB_ROOT=/tmp/claude-1000/-home-markus-projects-ha-development/a6635f88-42be-4c31-ba1c-6202687a7207/scratchpad/fints-verify
$H/run.sh bootstrap        # skip if $SB_ROOT/extra already exists and is populated
$H/run.sh up
```

Then, following `.claude/skills/verify/SKILL.md` ("Drive it"), collect evidence for:

1. Healthy baseline: `sensor.konto_3000` = `1234.56`; `fakebank.log` shows per poll exactly one `DIALOG_OPEN init_sca=no` … `DIALOG_END` pair (was: none — the fake's data methods used to run without any dialog).
2. `touch $SB_ROOT/flags/initsca`, force a poll: notification `fints_atruvia_reauth` exists, `button.re_authentifizierung_bestatigen` state ≠ `unavailable`, sensor still `1234.56` with attribute `2fa_pending: true`; log: `INIT_SCA`, no `DIALOG_END` yet.
3. Press the button (flag still set): `SEND_TAN`, then sensor refreshed, notification gone, log order `INIT_SCA → SEND_TAN → DIALOG_END`, no second `DIALOG_OPEN` between them.
4. `run.sh stop`, `run.sh up` again with the flag still present (or restart HA the way `run.sh` supports): entry state `loaded`, sensor `unknown`, `2fa_pending: true`, button available, exactly one `INIT_SCA`; press → values.
5. `rm flags/initsca; touch flags/pinerror`, reload the entry via `/api/config/config_entries/entry/<id>/reload`: state `setup_error`, one flow with `step_id: reauth_confirm`, exactly one `DIALOG_OPEN rejected=9942` line, `ha.log` shows `FinTS login rejected by bank (codes: 9942)` and no `Retrying in`.
6. Log hygiene: `grep -c SandboxPIN12345 $SB_ROOT/ha.log` → 0, `grep -c DE89370400440532013000 $SB_ROOT/ha.log` → 0.

Write findings (commands, observed values, log excerpts with the PIN/IBAN absent) to `docs/verification-2026-09-02.md`, same structure as `docs/verification-2026-08-11.md`. Finish with `$H/run.sh stop` and confirm no `hass` process is left (`pgrep -fa 'hass --config' || echo none`).

- [ ] **Step 4: Regression check + lint, then commit**

Run: `PYTHONPATH=$PWD .venv/bin/pytest -q && uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .`
Expected: 124 passed, ruff clean (`.claude` is excluded from ruff; `docs/*.md` too).

```bash
git add .claude/skills/verify/harness/fake_fints/sitecustomize.py .claude/skills/verify/SKILL.md docs/verification-2026-09-02.md
GIT_AUTHOR_DATE="2026-09-02 00:30:00 +0200" GIT_COMMITTER_DATE="2026-09-02 00:30:00 +0200" \
git commit -m "test(verify): model SCA at dialog init in the fake bank, record run

New flag initsca makes the fake bank demand SCA on every dialog init and
refuse data calls outside an authenticated dialog (the real 9340 -> PIN
error path); pinerror now also applies to a coordinator login. Evidence
for the standing-dialog lifecycle in docs/verification-2026-09-02.md.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Docs, changelog and release bump to v0.6.3

**Files:**
- Modify: `CLAUDE.md` section "### 90-day SCA / re-auth lifecycle" and the `_sync` sentence in "### First login / system-id sync (config flow)"
- Modify: `README.md` section "## 90-Tage Re-Authentifizierung"
- Modify: `CHANGELOG.md` (new `[0.6.3]` block above `[0.6.2]`)
- Modify: `custom_components/fints_atruvia/manifest.json` (`"version": "0.6.3"`)

- [ ] **Step 1: CLAUDE.md**

Replace the "### 90-day SCA / re-auth lifecycle" section body with:

```markdown
Atruvia banks require SecureGo+ confirmation roughly every 90 days — and for a login the bank has not (yet) exempted, at **every dialog initialisation**. The coordinator therefore works in **one standing dialog per poll**: `client.open_dialog()` → `get_accounts` → per account `get_balance` / `get_transactions` → `client.close_dialog()`. Never call the python-fints data methods outside that dialog: python-fints would open a dialog per call, only *record* an init challenge (3955 → `init_tan_response`) and send the job anyway; Atruvia refuses it with 9340, which python-fints misclassifies as a PIN error, blocks its `Password` object, and the HKEND on the way out dies with a bare `Exception("Refusing to use PIN after block")` (that is what a bare `Exception` in the coordinator's error log means — `describe_exception_chain` names the chain). Flow:

1. `open_dialog()` returns the init challenge, or a job returns a `NeedTANResponse` → `TanRequiredError`. Coordinator stashes `_tan_response`, sets `is_2fa_pending = True`, creates a persistent notification and returns `self.data` (last known good) — or `{}` on the very first poll, so the entities and the button exist. Do **not** raise `ConfigEntryAuthFailed` here: the reauth flow's own TAN would reload the entry into the same challenge, forever. The dialog stays open.
2. `button.py` `FintsReAuthButton` becomes `available` while `is_2fa_pending`. Pressing it calls `coordinator.async_complete_reauth()` → `client.complete_tan()` (answers the challenge **in the open dialog** and leaves it open) → `await self.async_refresh()` immediately, so the fetch runs in the dialog the user just authenticated; `open_dialog()` is idempotent for that. If `complete_tan` hands back a follow-up challenge (3956), the coordinator keeps `is_2fa_pending`, stores the new challenge and returns `False`; the button raises a `HomeAssistantError` toast so the user confirms in the app and presses again.
3. `AuthRejectedError` from `open_dialog()` (9942 & co. at login) → `client.close()` + `ConfigEntryAuthFailed`: a wrong PIN must not be retried (Atruvia locks the access after a few failed logins); HA's reauth config-flow (`async_step_reauth_confirm`) asks for new credentials and reuses the existing `credential_id`. `CredentialStoreError` (decryption failed) takes the same exit. Any other failure also calls `client.close()` so a blocked PIN object never survives into the next poll.
```

In "### First login / system-id sync (config flow)" append to the paragraph: `_sync` ends its dialog (HKEND) only when HISYN arrived; a rejected sync is dropped with `dialog.open = False` — the bank has aborted it already and an HKEND would come back as 9340 with the PIN-block consequences above. `complete_tan` and `init_system_id` leave the account dialog **open**; `_finish_setup` (and the retry / abandon paths) end it through `client.close()`.

- [ ] **Step 2: README.md**

Replace the "## 90-Tage Re-Authentifizierung" section with:

```markdown
## 90-Tage Re-Authentifizierung

Atruvia-Banken erfordern alle ~90 Tage eine erneute Bestätigung via SecureGo+.
Manche Zugänge verlangen die Freigabe zunächst bei **jedem** Abruf, bis die Bank
die 90-Tage-Befreiung erteilt hat — auch direkt nach der Einrichtung. Der Ablauf
ist in beiden Fällen derselbe:

1. Home Assistant sendet eine **persistente Benachrichtigung**; die Sensoren
   behalten ihren letzten Wert (bzw. bleiben nach einem Neustart zunächst
   `unbekannt`) und zeigen `2fa_pending: true`
2. **SecureGo+ App** öffnen und die Anfrage bestätigen
3. In HA den Button **"Re-Authentifizierung bestätigen"** drücken — der Abruf
   läuft dann sofort in der gerade freigegebenen Sitzung

Pro Abruf öffnet die Integration genau einen FinTS-Dialog, es kommt also höchstens
eine Freigabe-Anfrage pro Abruf (alle 6 Stunden). Lehnt die Bank die gespeicherte
PIN ab, wird nicht erneut versucht (Sperrgefahr nach drei Fehlversuchen), sondern
HA fragt die Zugangsdaten neu ab.
```

- [ ] **Step 3: CHANGELOG.md**

Insert above `## [0.6.2] - 2026-09-01`:

```markdown
## [0.6.3] - 2026-09-02

Bugfix-Release für Zugänge, bei denen die Bank die SecureGo+-Freigabe bei jedem
Dialog verlangt (Sparda-Bank BW in der Praxis): Der Coordinator lief nach der
Einrichtung in eine Endlosschleife aus Fehlversuchen, und jeder Versuch löste eine
neue Push-Anfrage auf dem Handy aus, deren Bestätigung wirkungslos blieb.

### Behoben

- **Freigabe-Anfrage beim Dialogstart wurde ignoriert.** python-fints öffnete pro
  Abruf-Aufruf einen eigenen Bank-Dialog, merkte sich die Freigabe-Anforderung
  (3955) nur und schickte den Auftrag trotzdem. Die Bank lehnte ab (9340),
  python-fints wertete das als PIN-Fehler und sperrte die PIN, der Dialog-Abbau
  scheiterte mit einer nackten `Exception`, HA wiederholte das Setup mit
  Backoff — und jeder Versuch war ein neuer Push. Die Integration arbeitet jetzt
  in **einem** Dialog pro Abruf, erkennt die Freigabe-Anforderung beim Start und
  meldet sie wie die 90-Tage-Re-Authentifizierung: Benachrichtigung, Button,
  nach dem Drücken läuft der Abruf **in derselben freigegebenen Sitzung** weiter.
- **Erster Abruf mit Freigabe-Pflicht** (direkt nach der Einrichtung oder nach
  einem Neustart) legt die Entitäten jetzt an (Sensoren `unbekannt`,
  `2fa_pending: true`) statt in den Reauth-Flow zu springen, der für diese
  Zugänge in dieselbe Anforderung zurückgeführt hätte.
- **Abgelehnte PIN beim Abruf** führt nicht mehr in eine Wiederholungsschleife
  (Sperrgefahr), sondern in HAs Reauth-Flow zur Neueingabe der Zugangsdaten.
- **Fehlgeschlagene Ersteinrichtung mit `FinTSDialogInitError`.** Nach dem von der
  Bank abgelehnten Einschritt-Sync schickte die Integration ein HKEND in den
  bereits abgebrochenen Dialog; die Antwort 9340 sperrte die PIN in python-fints
  und der Zweischritt-Sync scheiterte beim Signieren. Ein abgelehnter Sync wird
  jetzt ohne HKEND verworfen.

### Intern

- `FinTsAtruviaClient.open_dialog()` / `close_dialog()`; `complete_tan` und
  `init_system_id` lassen den authentifizierten Dialog für den Aufrufer offen.
- Fehlerlogs nennen die Kette der Exception-Typen (`describe_exception_chain`),
  weiterhin ohne Banktext.
- Fake-Bank des Verify-Harness: Flag `initsca`, `pinerror` auch beim Abruf.
```

- [ ] **Step 4: manifest.json**

Change `"version": "0.6.2"` to `"version": "0.6.3"`. `hacs.json`, `requirements.txt` and `pyproject.toml` stay (the HA/Python pins do not change).

- [ ] **Step 5: Final checks**

```bash
PYTHONPATH=$PWD .venv/bin/pytest -q
uvx ruff@0.16.2 check . && uvx ruff@0.16.2 format --check .
python3 -c "import json;print(json.load(open('custom_components/fints_atruvia/manifest.json'))['version'])"   # 0.6.3
grep -c "0.6.3" CHANGELOG.md   # >= 1
```
Expected: 124 passed, ruff clean, `0.6.3`.

- [ ] **Step 6: Commit and verify the timestamp policy over all new commits**

```bash
git add CLAUDE.md README.md CHANGELOG.md custom_components/fints_atruvia/manifest.json
GIT_AUTHOR_DATE="2026-09-02 00:50:00 +0200" GIT_COMMITTER_DATE="2026-09-02 00:50:00 +0200" \
git commit -m "chore(release): v0.6.3

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"

TZ=Europe/Berlin git log --format='%h %ad %s' --date=format-local:'%a %Y-%m-%d %H:%M' -8
TZ=Europe/Berlin git log --format='%ad' --date=format-local:'%a %H:%M' | grep -E '^(Mo|Di|Mi|Do|Fr) (0[89]|1[0-6]):' || echo "timestamp policy OK"
```
Expected: the seven new commits listed from 2026-09-01 23:05 to 2026-09-02 00:50 in order, and `timestamp policy OK`.

---

## Self-review notes

- Spec coverage: `open_dialog`/`close_dialog` (T3), `complete_tan` keeps dialog open (T4), `_sync` no HKEND (T2), `deconstruct` refresh (T3), `describe_exception_chain` (T1), coordinator one dialog + `{}` first poll + `AuthRejectedError` → `ConfigEntryAuthFailed` + `close()` on failure + `async_refresh` (T5), fake bank `initsca`/`pinerror` + SKILL.md (T6), docs/changelog/version (T7).
- Names used across tasks: `open_dialog`, `close_dialog`, `close`, `describe_exception_chain`, `AuthRejectedError(codes=...)`, `_open_dialog(client)` (static on the coordinator), `_client_with_bank` (test helper, defined in T3, used in T4 — T4 says to move it up if needed).
- Test counts assume the baseline of 105 and the additions listed (T1 +5, T2 +1, T3 +8, T4 +1, T5 +4 = 124). If a count differs by one, check the test list rather than the code.
