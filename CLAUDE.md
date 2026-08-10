# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Home Assistant custom integration — `fints_atruvia` — that polls German cooperative banks running on the Atruvia FinTS gateway (Sparda-Bank SW/BW) and exposes balances, 30-day income/expense stats, and a re-auth button. Ships with a standalone Lovelace card under `frontend/`.

User-facing strings and `README.md` / `SECURITY.md` are written in German; keep new UI text consistent with that.

## Commands

Development happens inside a VS Code devcontainer (`.devcontainer.json` → `mcr.microsoft.com/devcontainers/base:debian` with Python 3.14). Outside the container, install with `scripts/setup` (uses `uv pip install --system -r requirements.txt`).

```bash
# Run a local Home Assistant against this repo's custom_components/ + config/
scripts/develop                     # serves http://localhost:8123

# Tests (pytest-homeassistant-custom-component, asyncio_mode = auto)
pytest                              # all
pytest tests/test_pure.py           # one file
pytest tests/test_pure.py::test_mask_iban_for_event_keeps_only_country_and_last4
pytest --cov                        # coverage of custom_components.fints_atruvia

# Lint (ruff target py314, select = ALL — strict)
ruff check .
ruff format .

# Lovelace card build (output: config/www/fints-atruvia-card.js)
cd frontend && npm install && npm run build     # one-shot
cd frontend && npm run watch                    # rebuild on save
```

### Running tests outside the devcontainer

`pyproject.toml` declares `requires-python = ">=3.14"` (matches `manifest.json: "homeassistant": "2026.3.2"` — HA pins Python ≥ 3.14.2). On a host with `uv` but only an older system Python, `uv` will download Python 3.14 automatically:

```bash
uv venv --python 3.14                                  # one-time
uv pip install -r requirements.txt                     # one-time
PYTHONPATH=$PWD .venv/bin/pytest                       # run the suite
```

If `requires-python` ever gets out of sync with the HA pin, `uv pip install -r requirements.txt` will fail with an unsatisfiable-resolver error — bump both together with `manifest.json`.

`config/` is HA's runtime config (gitignored except `configuration.yaml` and `www/.gitkeep`). The custom component lives in `custom_components/fints_atruvia/` and is picked up via `PYTHONPATH=$PWD` injected by `scripts/develop`.

## Architecture

### Layering

```
config_flow.py  ─┐
                 │ creates entry.data { blz, url, product_id,
                 │   selected_accounts, credential_id }    (NO pin/username here)
                 ▼
__init__.py  ──► async_setup_entry  ──► FintsBankingCoordinator (coordinator.py)
                                            │
                                            ├─► FintsCredentialStore (storage.py) — Fernet-decrypt PIN
                                            ├─► FintsStateStore     (storage.py) — Fernet-decrypt FinTS state blob
                                            └─► FinTsAtruviaClient  (api.py)     — sync python-fints wrapper
                                                  ↑ called via hass.async_add_executor_job
                                            │
                                            └─► forwards to sensor.py / button.py (CoordinatorEntity)
```

**`api.py` is synchronous** (python-fints is blocking). Everything that touches it must be wrapped in `hass.async_add_executor_job(...)`. Coordinator already does this; new call sites must too.

### Credential storage (security-critical — don't break this)

Two on-disk files, both `private=True` (mode 0600):

- `.storage/fints_atruvia_master_key` — single Fernet key shared by all entries.
- `.storage/fints_atruvia_credentials_<credential_id>` — Fernet-encrypted `{username, pin}` blob per entry.
- `.storage/fints_atruvia_state_<credential_id>` — Fernet-encrypted python-fints state blob (system_id/BPD/UPD; no PIN by python-fints contract).

`entry.data` holds **only** `credential_id`, never `username`/`password`, and the config entry's `unique_id` contains the login no more either — it's `_entry_unique_id(key, blz, username)`: HMAC-SHA256 over `entry_unique_id|{blz}|{username}` keyed with the install's master Fernet key (`storage.async_get_master_key`), first 16 hex chars. Keyed rather than a plain digest because the BLZ sits in cleartext in the same `core.config_entries` and NetKey logins are short numeric customer IDs, so an unkeyed hash would be brute-forceable offline. Both call sites (`config_flow.async_step_user`, the v2→v3 migration) are async and fetch the key first; losing the master key makes the value unreproducible, which the fail-open migration already handles by keeping the legacy unique_id. Config-flow `VERSION = 3`; `async_migrate_entry` chains v1→v2 (lifts legacy plaintext entries into the encrypted store, idempotent — if a `credential_id` already exists in v1 data from a partial retry, reuse it instead of orphaning the prior blob) and v2→v3 (rehashes the unique_id from the decrypted credential; falls back to keeping the legacy unique_id and only bumping the version if decryption fails). The v1→v2 step also post-checks that `password`/`username` were actually removed from `entry.data` and fails loudly if not.

`FintsStateStore` reads legacy v1 plaintext-hex blobs transparently and re-encrypts on the next save — this relies on `_MigratingStore`, a `Store` subclass whose `_async_migrate_func` returns the old data unchanged instead of the base class's `NotImplementedError`; without it, HA's loader aborts setup for anyone still holding a real v1 state file.

The PIN is supplied to `FinTsAtruviaClient` via a `pin_provider: Callable[[], str]` callable — never pass it as a positional/keyword arg, since that surfaces in tracebacks. python-fints still captures it internally; the wrapper drops its own reference on `close()` and the coordinator wipes `_pin` in `async_shutdown`.

### IBAN handling

IBANs must be masked at every external boundary:

- **Sensor `extra_state_attributes`** → `_mask_iban` (`DE51 **** **** **** 3922`)
- **`fints_atruvia_new_transaction` event payload** → `_mask_iban_for_event` plus `iban_last4`
- **Entity `unique_id`** → `iban_unique_id(entry_id, iban)` (salted SHA-256, first 16 hex chars). Migration of legacy `{entry_id}_{iban}[_suffix]` unique_ids happens automatically in `_async_migrate_unique_ids` at entry setup. The two known stats suffixes (`_income_30d`, `_expense_30d`) must be preserved — see `_SENSOR_SUFFIXES` in `__init__.py`.
- **Config-flow account picker label** → `Konto …{last4}`; full IBAN stays server-side as the option value.

### Bank-text exposure

Transaction `purpose` and `creditor` (counterparty name) are **off by default**. They appear in sensor attributes and event payloads only when the per-entry options-flow toggle `CONF_EXPOSE_FULL_DATA` is true. Don't add code paths that surface these fields unconditionally. The coordinator exposes the toggle via `coordinator.expose_full_data`. `async_setup_entry` registers an options update listener (`entry.add_update_listener(_async_reload_entry)`) that reloads the entry whenever the toggle changes, so it takes effect immediately instead of waiting for the next 6-hour poll.

### 90-day SCA / re-auth lifecycle

Atruvia banks require fresh SecureGo+ confirmation roughly every 90 days. Flow:

1. Coordinator catches `TanRequiredError` mid-update → stashes `_tan_response`, sets `is_2fa_pending = True`, creates a persistent notification, and returns `self.data` (last known good).
2. `button.py` `FintsReAuthButton` becomes `available` while `is_2fa_pending`. Pressing it calls `coordinator.async_complete_reauth()` → `client.complete_tan()` → fresh refresh.
3. If decryption itself fails, `CredentialStoreError` is converted to `ConfigEntryAuthFailed` so HA opens the reauth config-flow (`async_step_reauth_confirm`), which reuses the existing `credential_id`.

### New-transaction events

`fints_atruvia_new_transaction` is fired per detected transaction. Deduplication uses `_transaction_hash` (sha256 of `date|amount|purpose|creditor`) since FinTS/MT940 carries no stable transaction id. Seen hashes are persisted per-IBAN to `.storage/fints_atruvia_seen_transactions_<entry_id>`. **First run after install seeds the set silently** (`_seen_initialised` gate) — never fire events for the historical backfill.

### Lovelace card (`frontend/`)

Plain rollup build of `src/fints-card.js` → **two** targets: `../custom_components/fints_atruvia/www/fints-atruvia-card.js` (committed — this is what HACS ships and what `frontend.py` serves under `/fints_atruvia/fints-atruvia-card.js`) and `../config/www/fints-atruvia-card.js` (local dev instance only, gitignored). Rebuild and commit the first one whenever `src/` changes. `frontend.py` registers the Lovelace resource itself, version-stamped from `manifest.json` for cache-busting; the card guards its `customElements.define` so a leftover `/local/` resource can't crash the dashboard. No bundler magic, no TypeScript, no source maps in production (XSS-defense — see `SECURITY.md` §9). All bank-controlled text must go through `escapeHtml()` before reaching `innerHTML`. The card auto-detects whether the `transactions` attribute is present and degrades to balance-only when it isn't. Transactions are sorted newest-first (`sortTransactionsDescending`) before slicing to the five shown per account. The card's `title:` config option is honoured for the first entity in a multi-entity config (escaped, falls back to `Konto {last4}` otherwise). `_render()` diffs the full generated HTML against the last render and skips touching `shadowRoot.innerHTML` when nothing changed, to avoid unnecessary re-renders on every coordinator poll.

## Conventions

- **Ruff `select = ALL`** with a small ignore list (`ANN401`, `D203`, `D212`, `COM812`, `ISC001`, `CPY001`). New code is expected to satisfy the strict set; if a rule needs suppressing, add a `# noqa: <code>` with reason rather than expanding the global ignore. `ruff check .` and `ruff format --check .` are both gated in CI (`.github/workflows/validate.yml`, ruff pinned to a fixed version so a new release can't turn the build red overnight), so both must be clean before a commit lands. Three structural exceptions live in `.ruff.toml` instead of per-line `noqa`, each carrying its justification as a comment there: `extend-exclude = [".claude", "*.md"]` (the verify harness is throwaway scaffolding, not shipped code; and ruff's formatter rewrites fenced python blocks in markdown, which mangles the illustrative fragments in `docs/`), `[lint.pydocstyle] convention = "pep257"` (ignoring `D212` on its own activates its counter-rule `D213`; the convention setting resolves the pair, same as HA core), and a `tests/**` block in `[lint.per-file-ignores]` for the standard pytest patterns (`S101`, `ANN`, `D102`/`D103`/`D401`, `SLF001`, `PLR2004`, `FBT001`, `PT022`, `INP001`). Don't widen any of those; anything else goes in the file with a reason.
- **Log hygiene:** never `_LOGGER.exception(...)` from FinTS / config-flow / button paths — python-fints stringifies HBCI segments which can include account numbers. Use `_LOGGER.error("...: %s", type(exc).__name__)` and let the chained exception carry the detail to `debug`. Avoid `%r` on transaction objects for the same reason.
- **HTTPS-only:** `_validate_https_url` rejects non-https URLs and non-ASCII hostnames (IDN/Punycode block). `FinTsAtruviaClient.__init__` also asserts `https://`. Don't bypass either.
- **Test discipline:** security-sensitive behaviour (IBAN masking, unique-id hashing, URL validation, storage round-trip, event payload shape) has regression tests in `tests/`. Adding a code path that touches any of those without a corresponding test is a regression risk.
- **Manifest pinning:** the minimum HA version lives in `hacs.json` (`homeassistant: "2026.3.2"`) — *not* in `manifest.json`, whose key set is closed (hassfest `CUSTOM_INTEGRATION_MANIFEST_SCHEMA` rejects extras). `requirements.txt` pins the same version for the dev environment, and `pyproject.toml` pins `requires-python = ">=3.14"` so `uv` picks the right interpreter outside the devcontainer. Bump all three together.
- **Translations:** `strings.json`, `translations/de.json` and `translations/en.json` must be kept structurally identical (same keys, German text in all three — this project doesn't localize UI strings, see "What this is"). HA loads translations exclusively from `<integration>/translations/<lang>.json`; there is no fallback to `strings.json` at runtime (that file is read by hassfest only), so a missing `translations/en.json` leaves English-locale instances showing raw keys instead of labels.
