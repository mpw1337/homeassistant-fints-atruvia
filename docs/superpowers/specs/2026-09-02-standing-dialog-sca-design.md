# Standing dialog per poll + SCA at dialog init — design note

Date: 2026-09-02. Status: agreed with the maintainer, to be implemented as v0.6.3.

## Symptom (production, 2026-09-02)

Adding a Sparda-Bank BW login (BLZ 60090800) succeeded in the config flow, but the
coordinator's first poll and every retry failed with
`FinTS update failed: Exception`, the entry sat in `setup_retry`, and the user
received a SecureGo+ push for every retry (5, 10, 20, 40, 80 s …). Confirming in the
app changed nothing.

The Sparda-Bank SW login (BLZ 55090500) on the same instance works, because the bank
answers every one of its dialogs with 3076 ("SCA not necessary").

## Root cause (from the production traceback and the python-fints 5.0.0 source)

1. `FintsBankingCoordinator._async_update_data` calls `get_accounts`,
   `get_balance`, `get_transactions` as independent python-fints calls. Without a
   standing dialog python-fints opens **a new dialog per call** (`with
   self._get_dialog()`), i.e. ten dialogs per poll for five accounts.
2. For a login without SCA exemption the bank answers the dialog initialisation
   (HKIDN + HKVVB + HKTAN v7 process 4) with **3955** — a decoupled SecureGo+
   challenge, i.e. a push. python-fints only records it in
   `client.init_tan_response`; `_send_with_possible_retry` never looks at it and
   sends the job (HKSPA) anyway.
3. The bank refuses the job: **9340 "Auftrag abgelehnt"**, 9800 "Dialog abgebrochen",
   9050. python-fints' `_process_response` lists 9340 among the PIN-error codes, so it
   calls `pin.block()` and raises `FinTSClientPINError`.
4. Leaving `with dialog:` sends HKEND, which has to be signed with the now blocked
   `Password` object → `Exception("Refusing to use PIN after block")`
   (`fints/utils.py` `Password.__str__`). That bare `Exception` replaces the
   `FinTSClientPINError`; the coordinator logs only `type(err).__name__`.
5. HA retries setup with backoff → new client → new dialog → new push. Nobody ever
   sends HKTAN process S for the challenge, so the app confirmation is ignored.

The same "blocked PIN → bare Exception" mechanism explains the
`FinTSDialogInitError` seen at 11:00 for the SW login: after the rejected one-step
sync (9075) `_sync` sends HKEND to an already aborted dialog, the bank answers 9340,
python-fints blocks the PIN, `_sync` swallows the `FinTSClientPINError` from
`dialog.end()`, and the following two-step sync dies inside `dialog.init` when
signing — which `dialog.init` wraps into `FinTSDialogInitError`.

The config flow does not have this bug: `_open_dialog_and_cache_accounts` enters
the client (one standing dialog), checks `init_tan_response` and hands the
challenge to the 2fa step.

## Design

### api.py (`FinTsAtruviaClient`)

- **`open_dialog() -> NeedTANResponse | None`** — enter the python-fints client
  (one standing dialog). Idempotent: if a standing dialog is already open (left
  open by `complete_tan`), return `None` without touching the bank. Returns the
  init challenge when the bank demands SCA at dialog initialisation (dialog stays
  open for `complete_tan`), else `None`. `FinTSClientPINError` /
  `FinTSClientTemporaryAuthError` → drop the python-fints client (`close()`, so the
  blocked PIN object goes away) and raise `AuthRejectedError(codes=…)`.
- **`close_dialog()`** — public wrapper for `_close_standing_dialog()`: one HKEND
  per poll, client stays usable.
- **`complete_tan`** with a known system id no longer ends the dialog after caching
  the accounts. The dialog the user just authenticated is the one to fetch data
  in — a fresh dialog could demand SCA all over again (that is exactly what the BW
  bank did right after the config flow's TAN). `_open_dialog_and_cache_accounts`
  likewise leaves the dialog open (closing it only if the account fetch raises).
  Callers end the dialog: the coordinator via `close_dialog()` after the fetch,
  the config flow via `close()` in `_finish_setup` / on retry / on abandon.
- **`_sync`** ends the dialog (HKEND) only when HISYN arrived. A rejected sync
  (no HISYN, no challenge) has already been aborted by the bank (9800); sending
  HKEND gets 9340 and a blocked PIN. Mark `dialog.open = False` instead — the same
  thing python-fints does for a 9075 on its standing dialog.
- **`deconstruct()`** also stores the blob in `self._fints_state`, so a client
  rebuilt after `close()` starts from the latest state.
- **`describe_exception_chain(exc) -> str`** — `"Outer <- Cause <- Context"` type
  names along `__cause__`/`__context__` (depth-capped, cycle-safe). Type names only
  (log hygiene). Used by the coordinator's and the config flow's generic error
  logs, so a wrapped `FinTSClientPINError` or a `requests` error is visible.

### coordinator.py

- One standing dialog per poll: `open_dialog` (via a small blocking helper that
  raises `TanRequiredError` on a challenge) → `get_accounts` → per account
  `get_balance` + `get_transactions` → `close_dialog`.
- `TanRequiredError` (init challenge or job-level challenge): unchanged handling
  (notification, `is_2fa_pending`, dialog stays open), **but** with no previous
  data it now returns `{}` instead of raising `ConfigEntryAuthFailed`. Reason: the
  reauth flow would complete its own TAN, reload the entry, and the first poll would
  demand SCA again → endless reauth loop for a bank without exemption. With `{}`
  the entities exist (sensors `unknown`, `2fa_pending: true`), the button is
  available, and the confirmation completes in the same dialog.
- `AuthRejectedError` → `client.close()` + `ConfigEntryAuthFailed`. A wrong PIN
  must not be retried (Atruvia locks the access after a few failed logins); HA's
  reauth flow asks for new credentials instead.
- Any other exception → `client.close()` (drops a possibly blocked PIN object and
  the dead dialog) + `UpdateFailed`, logging `describe_exception_chain(err)`.
- `async_complete_reauth` runs `await self.async_refresh()` (immediately, in the
  still-open dialog) instead of the debounced `async_request_refresh()`.

### Verify harness (`.claude/skills/verify/harness/fake_fints/sitecustomize.py`)

- New flag `initsca`: `__enter__` sets `init_tan_response` (bank demands SCA at
  dialog init) and logs `INIT_SCA`; every `__enter__`/`__exit__` logs
  `DIALOG_OPEN` / `DIALOG_END`. With the flag set, any data call outside a
  standing dialog raises `FinTSClientPINError` (models the bank's 9340 → python-fints
  PIN error) so a regression to per-call dialogs fails loudly.
- `pinerror` now also applies to `__enter__` (coordinator login rejected).

### Out of scope

- python-fints' misclassification of 9340 as a PIN error (upstream).
- Reauth flow behaviour when the FinTS state store is empty (unchanged).
