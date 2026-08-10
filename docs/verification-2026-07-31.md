# Verification findings — 2026-07-31

Runtime verification of commits `93f9f1c` (encrypted credential store, hashed
unique-IDs, `expose_full_data`, card hardening) and `c847440` (tests).

**Method:** real Home Assistant 2026.3.2 against a synthetic config sandbox
(v1 entry with plaintext credentials + legacy registry rows), with
`fints.client.FinTS3PinTanClient` replaced by an offline fake bank. No real
bank contacted, no real credentials touched. Reproduce with
`.claude/skills/verify/` (`harness/run.sh`).

**Result:** every claim of the two commits held at the surface — migration,
credential encryption, unique-ID hashing, IBAN masking, opt-in bank text,
event dedup, the 90-day SCA/re-auth lifecycle, credential-loss → reauth
recovery, card escaping and the degraded view. Log hygiene verified: PIN,
IBAN, account number and username produce 0 hits in a DEBUG-level log.

The items below are what the run turned up on top of that.

---

## 1. Legacy IBAN survives in the entity registry as `previous_unique_id`

**Severity:** medium (privacy claim not fully met) · **Status:** fixed

Fixed in `custom_components/fints_atruvia/__init__.py` (Task 3, commit
`2107ed6`): `_async_migrate_unique_ids` now calls a new
`_async_clear_previous_unique_ids` right after the registry migration, which
walks the entry's registry rows and clears any `previous_unique_id` still
set.

`er.async_migrate_entries` records the pre-migration value, so after
`_async_migrate_unique_ids` runs, `.storage/core.entity_registry` contains
both forms — permanently, and in every HA backup:

```json
"unique_id":          "01KSANDBOX000000000000FINT_609c6c22bcb49f2c",
"previous_unique_id": "01KSANDBOX000000000000FINT_DE89370400440532013000"
```

The file is mode `0644`, unlike the integration's own `private=True` stores.

`iban_unique_id()` (`custom_components/fints_atruvia/__init__.py:48-57`)
states the registry "no longer contains plaintext account numbers" — on disk
it still does. The API half of the claim does hold: the WebSocket
`config/entity_registry/get` response returns only the hashed `unique_id`.

**Options**
- Clear `previous_unique_id` after migration (extra registry update in
  `_async_migrate_unique_ids`, `__init__.py:60-92`), then re-check that HA
  does not restore it on the next write.
- Or narrow the docstring/`CLAUDE.md` claim to "not exposed via the registry
  API" and document the on-disk residue in `SECURITY.md`.

Either way the residue should be mentioned next to the existing "backups may
still contain the plaintext PIN" warning (`__init__.py:176-181`).

---

## 2. Account picker label leaks the raw account number

**Severity:** medium · **Status:** fixed

Fixed in `custom_components/fints_atruvia/config_flow.py` (Task 4, commit
`83d73ed`): the account-number parenthetical was dropped; labels are now
`Konto …{last4}` with a running `" (N)"` suffix only when two accounts share
the same last four IBAN digits, produced by the new `_account_labels()`
helper.

`custom_components/fints_atruvia/config_flow.py:293`

```python
label=f"Konto …{account.iban[-4:]} ({account.accountnumber})",
```

Rendered live in the reauth dialog as `Konto …3000 (0000123456)`. The comment
directly above it explains that the label is masked *because* it travels over
the WebSocket to the browser — but account number + BLZ (in `entry.data`, and
shown in the same flow) reconstructs the full IBAN, so the mask is undone.
Also contradicts the `Konto …{last4}` convention documented in `CLAUDE.md`.

**Fix:** drop the parenthetical, or replace it with a non-identifying
disambiguator (account type, or `…{accountnumber[-4:]}` only when two IBANs
share the same last four digits). Add a regression test alongside the other
masking tests in `tests/test_pure.py`.

---

## 3. `expose_full_data` has no effect until reload (up to 6 hours)

**Severity:** medium (UX; looks like a broken toggle) · **Status:** fixed

Fixed in `custom_components/fints_atruvia/__init__.py` (Task 3, commit
`2107ed6`): `async_setup_entry` now registers
`entry.async_on_unload(entry.add_update_listener(_async_reload_entry))`,
which reloads the entry (and thus reconnects to the bank) on any options
change.

Toggling the option in the UI writes `entry.options` immediately, but the
sensor attributes only change on the next entity state write. There is no
update listener anywhere in the integration
(`grep add_update_listener` → nothing), and `update_interval` is 6 hours
(`coordinator.py:96`), so after flipping the toggle nothing observably
happens — verified: attributes unchanged 60 s after enabling, correct
immediately after a manual entry reload.

**Fix:** in `async_setup_entry` (`__init__.py:95-106`)

```python
entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
```

with `_async_reload_entry` calling `hass.config_entries.async_reload(entry.entry_id)`.
Note this makes the toggle reconnect to the bank — acceptable, but worth
confirming it does not trigger an extra SCA prompt on a real Atruvia
instance.

---

## 4. Card shows the *oldest* five of the last ten transactions

**Severity:** low · **Status:** fixed

Fixed in `frontend/src/fints-card.js` (Task 5, commit `ef96999`): a new
`sortTransactionsDescending()` helper sorts by date descending (index as
tie-breaker for same-day entries) before `_renderEntity` slices the first
five.

`frontend/src/fints-card.js:149` — `transactions.slice(0, 5)` over the
attribute list, which `sensor.py:122` fills with `transactions[-10:]` in bank
order (chronologically ascending). The section is labelled
"Letzte Transaktionen", but with more than five bookings in the window the
newest ones are hidden. Observed order in the running card:
`29.07, 26.07, 22.07, 11.07, 31.07`.

**Fix:** sort by date descending before slicing (in the card, or in
`sensor.py` when building the attribute).

---

## 5. Card ignores `title:` from the card config

**Severity:** low (cosmetic) · **Status:** fixed

Fixed in `frontend/src/fints-card.js` (Task 5, commit `ef96999`):
`_renderEntity` now honours `config.title` for the first card of a multi-entity
config (escaped via `escapeHtml()`), falling back to `Konto {last4}` for the
rest.

`type: custom:fints-atruvia-card` with `title: "Sparda Sandbox"` rendered the
entity friendly name (`Konto 3000`) instead. Either honour `config.title` in
`_renderEntity` or document that the card takes its heading from the entity.

---

## 6. `_mask_iban` docstring example is wrong

**Severity:** trivial · **Status:** fixed

Fixed in `custom_components/fints_atruvia/sensor.py` (commit `f92246e`,
predates this round): the docstring example now reads
`GB33BUKB20201555555555 -> GB33 **** **** **** **** 5555`, consistent with
the code.

`custom_components/fints_atruvia/sensor.py:18-32` documents
`GB33BUKB20201555555555 -> DE51 **** **** **** 3922`: the example output is
not derived from the example input, and a 22-character IBAN actually produces
four groups, not three — the real output is `DE89 **** **** **** **** 3000`.

---

## 7. Local card build artifact is stale

**Severity:** low (local only) · **Status:** fixed

`frontend/src/fints-card.js`, `custom_components/fints_atruvia/www/fints-atruvia-card.js`
and `config/www/fints-atruvia-card.js` are byte-identical again (same md5)
after Task 5's `npm run build` (commit `ef96999`).

`config/www/fints-atruvia-card.js` (2026-05-13 19:49) predates
`frontend/src/fints-card.js` (2026-05-15 16:30), so the local dev instance
serves a card build without this commit's escaping and degraded-view changes.
The artifact is gitignored, so this is a workstation issue, not a repo one.

**Fix:** `cd frontend && npm run build`.

---

## 8. Dev environment: HA component requirements are missing from the venv

**Severity:** low (environment) · **Status:** informational

`requirements.txt` installs HA core only. With `--skip-pip` the frontend
hangs on "Loading data" — `websocket_api`'s `get_services` imports every base
component and dies on the first missing dependency (`hass_frontend`,
`hassil`, `home_assistant_intents`, `mutagen`, `turbojpeg`, `haffmpeg`,
`pymicro_vad`, `pyspeex_noise`). Note `hassil` must be pinned to the HA-pinned
`3.5.0`; 3.10 no longer has `hassil.fuzzy`.

Side effect worth knowing: while `frontend` fails to import, HA silently
ignores `http.server_port` and binds the default 8123.

The verify skill installs these into a throwaway directory
(`harness/run.sh bootstrap`) rather than into `.venv`.

---

## Verified-good (no action)

- v1 → v2 migration: `credential_id` only in `entry.data`, Fernet blobs at
  mode `0600`, no plaintext PIN anywhere under `.storage/`.
- Unique-ID migration incl. `_income_30d` / `_expense_30d` suffixes and the
  untouched `_reauth_button`.
- Masked IBAN in sensor attributes and event payloads; `transactions`
  attribute absent unless opted in.
- Event dedup: seeded silently on first run, one event per new booking, no
  re-fire across reloads or repeat polls.
- SCA lifecycle incl. pressing the re-auth button *before* confirming in the
  banking app (no crash, state stays consistent), and encrypted FinTS state
  restored on reconnect (`restored_state=yes`).
- Lost master key → `ConfigEntryAuthFailed` → reauth flow → same
  `credential_id` reused, no orphaned blob.
- Card escapes hostile bank text: `<img onerror>` / `<script>` from the bank
  produced no elements and no script execution.
