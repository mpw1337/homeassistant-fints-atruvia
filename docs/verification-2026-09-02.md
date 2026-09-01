# Verification findings — 2026-09-02

Runtime verification of the standing-dialog SCA fix (Tasks 1–5 of the
`2026-09-02-standing-dialog-sca` plan, committed up to `3868f2f`): the
coordinator now opens **one** standing FinTS dialog per poll
(`FinTsAtruviaClient.open_dialog()`, python-fints `__enter__`), reports an SCA
challenge raised at dialog init (`init_tan_response`) as pending 2FA instead
of failing the poll, completes that challenge through the existing re-auth
button in the same dialog, and only then fetches data before closing the
dialog (`close_dialog()`). A rejected login at dialog init
(`FinTSClientPINError` from `__enter__`) now raises `ConfigEntryAuthFailed`
directly, with no retries. This task adds a new fake-bank flag, `initsca`, that
models the Sparda-Bank-BW behaviour of demanding SCA on every dialog init, and
runs the resulting lifecycle end to end against a sandboxed Home Assistant.

**Method:** real Home Assistant **2026.3.2** (Python 3.14.4) started against a
synthetic config sandbox under `SB_ROOT` (a session scratch directory),
driven via `.claude/skills/verify/` (`harness/run.sh`) on port 8199.
`fints.client.FinTS3PinTanClient` was replaced by the harness's offline fake
bank for the whole run — **no real bank was contacted, no real credentials
were touched, and the developer's own `config/` directory was never read,
copied or started against.** The component logged at `DEBUG` throughout.
Verification was REST/WebSocket only; the browser/Lovelace card was out of
scope for this task.

Covered: a healthy poll's dialog framing, SCA raised at dialog init (both on
a live poll and across a full HA restart with the state persisted), the
re-auth button completing that challenge inside the same standing dialog, a
rejected login at dialog init driving `ConfigEntryAuthFailed` and the reauth
flow, and log hygiene (PIN/IBAN) across the run.

Not covered:

- The card / browser surface — this task's scope was the coordinator/API
  lifecycle only.
- A real Atruvia gateway's actual 3955/9340 wire format; the fake models the
  documented behaviour (module docstring in `sitecustomize.py`), it does not
  replay a captured trace.
- `tanmode2` / multi-account interaction with `initsca` together — the two
  flags were exercised independently, matching how the brief scoped the
  scenarios.

Baseline values on a healthy poll matched the harness's expectation exactly:
balance `1234.56` EUR, `iban` attribute `DE89 **** **** **** **** 3000`.

---

## Fake-bank change under test

`.claude/skills/verify/harness/fake_fints/sitecustomize.py` gained an
`initsca` flag: while set, `FakeFinTS3PinTanClient.__enter__` records a
`NeedTANResponse` on `init_tan_response` (logging `INIT_SCA`) immediately
after opening the standing dialog, and every data method
(`get_sepa_accounts`, `get_balance`, `get_transactions`) now calls
`_require_authenticated_dialog()` first, which raises `FinTSClientPINError`
(the fake's stand-in for the bank's real 9340) if no standing dialog is open
or the init challenge has not yet been cleared. This turns a regression to
per-call dialogs (the bug the standing-dialog fix addresses) into an
immediately-failing poll instead of a silent pass. `pinerror` was extended
to also apply to `__enter__` (previously only the sync dialog's `.init()`
raised it), covering the coordinator's login path, not just the config flow's.
`DIALOG_OPEN init_sca=yes|no` / `DIALOG_END` bracket every dialog in
`fakebank.log`.

## Scenario 1 — healthy baseline: one dialog per poll

```
$H/run.sh api /api/services/homeassistant/update_entity -X POST \
  -H 'Content-Type: application/json' -d '{"entity_id":"sensor.konto_3000"}'
```

`sensor.konto_3000` = `1234.56`, `2fa_pending: false`. `fakebank.log` for the
setup poll and one forced poll:

```
CONNECT server=https://fints.sandbox.invalid/fints30 user=sandboxuser blz=99999999 product=SANDBOXPRODUCT0000000001 pin_len=15 restored_state=no
DIALOG_OPEN init_sca=no
DIALOG_END
DIALOG_OPEN init_sca=no
DIALOG_END
```

Exactly one `DIALOG_OPEN init_sca=no` … `DIALOG_END` pair per poll, with no
`CONNECT` on the second poll (the coordinator reuses its client across polls).
**Pass** — matches expectation ("was: none — the fake's data methods used to
run without any dialog").

## Scenario 2 — SCA raised at dialog init

```
touch $SB_ROOT/flags/initsca
$H/run.sh api /api/services/homeassistant/update_entity -X POST \
  -H 'Content-Type: application/json' -d '{"entity_id":"sensor.konto_3000"}'
```

`fakebank.log` tail:

```
INIT_SCA
DIALOG_OPEN init_sca=yes
```

No `DIALOG_END` yet — the dialog is left open for `complete_tan`. Sensor
state unchanged (`1234.56`) with `2fa_pending: true`.
`persistent_notification/get` (websocket) lists `fints_atruvia_reauth`
("FinTS Atruvia" / "Sparda-Bank fordert Re-Authentifizierung. Bitte den
Re-Auth-Button in der Integration drücken."). `button.re_authentifizierung_bestatigen`
state is `unknown` (i.e. not `unavailable` — the button is available).
**Pass.**

## Scenario 3 — button confirms the challenge in the same dialog

```
$H/run.sh api /api/services/button/press -X POST \
  -H 'Content-Type: application/json' \
  -d '{"entity_id":"button.re_authentifizierung_bestatigen"}'
```

(flag `initsca` still set). `fakebank.log`:

```
INIT_SCA
DIALOG_OPEN init_sca=yes
SEND_TAN tan=<empty>
DIALOG_END
```

`SEND_TAN` follows `INIT_SCA`/`DIALOG_OPEN` directly — **no second
`DIALOG_OPEN` in between**, i.e. the button confirmation and the subsequent
data fetch ran inside the one dialog that was opened for the challenge.
Sensor recovered to `1234.56` with `2fa_pending: false`, and
`persistent_notification/get` no longer lists `fints_atruvia_reauth`. **Pass.**

## Scenario 4 — restart with the challenge still pending: no retry storm

```
$H/run.sh restart     # stop + start, keeps .storage (flag initsca still present)
```

Config entry state: `loaded` (not `setup_error`/`setup_retry`).
`sensor.konto_3000` = `unknown` with `2fa_pending: true`;
`button.re_authentifizierung_bestatigen` = `unknown` (available).
`fakebank.log` around the restart:

```
CONNECT server=https://fints.sandbox.invalid/fints30 user=sandboxuser blz=99999999 product=SANDBOXPRODUCT0000000001 pin_len=15 restored_state=yes
INIT_SCA
DIALOG_OPEN init_sca=yes
```

Exactly **one** `INIT_SCA` after the restart's `CONNECT` line (checked again
after a 5 s pause — no further `DIALOG_OPEN`/`INIT_SCA` appeared, and
`grep -c "Retrying in" ha.log` was `0`): no retry storm. Pressing the button
afterwards produced the same `SEND_TAN` → `DIALOG_END` sequence as scenario 3
and the sensor returned to `1234.56`. **Pass.**

## Scenario 5 — rejected login on a poll

```
rm $SB_ROOT/flags/initsca
touch $SB_ROOT/flags/pinerror
$H/run.sh api /api/config/config_entries/entry/<entry_id>/reload -X POST
```

Config entry state: `setup_error`, `reason: "Bank rejected the stored
credentials — please re-authenticate."`. `config_entries/flow/progress`
(websocket) lists exactly one flow:

```json
{"handler": "fints_atruvia", "context": {"source": "reauth", ...}, "step_id": "reauth_confirm"}
```

`fakebank.log` shows exactly one `DIALOG_OPEN rejected=9942` line for the
reload (`grep -c` → `1`), and `ha.log` line 145:

```
ERROR (MainThread) [custom_components.fints_atruvia.coordinator] FinTS login rejected by bank (codes: 9942)
```

`grep -c "Retrying in" $SB_ROOT/ha.log` → `0`: no retries. **Pass** — matches
the `AuthRejectedError` → `ConfigEntryAuthFailed` path documented for
`pinerror` in `SKILL.md`.

## Scenario 6 — log hygiene

`grep -c` for the sandbox login PIN, and separately for the selected
account's full IBAN, against `$SB_ROOT/ha.log` both returned `0` hits —
across the entire run (baseline poll, both SCA-at-init dialogs, the restart,
and the rejected-login reload). **Pass.**

---

## Teardown

`$H/run.sh stop`, then `ps aux | grep -i "[h]ass --config"` returned nothing —
no `hass` process left running.

## Regression check

```
PYTHONPATH=$PWD .venv/bin/pytest -q
```

→ `127 passed` (unchanged by this task; the fake bank and `SKILL.md` are the
only files touched, both excluded from the pytest run).

```
uvx ruff@0.16.2 check .
uvx ruff@0.16.2 format --check .
```

→ `All checks passed!` / `19 files already formatted`.

## Result

All six scenarios in the task brief behaved as expected: a healthy poll opens
and closes exactly one dialog, an SCA challenge raised at dialog init is
reported as pending 2FA without failing the poll (including across a full HA
restart, with no retry storm), the re-auth button completes that challenge
inside the same standing dialog with no intervening second dialog, a rejected
login at dialog init produces `setup_error` plus a `reauth_confirm` flow with
no retries, and no PIN or IBAN reached `ha.log` at any point. No regression to
per-call dialogs was observed — the fake bank's new
`_require_authenticated_dialog()` guard (which would turn such a regression
into an immediate `FinTSClientPINError`) never fired unexpectedly during the
run.
