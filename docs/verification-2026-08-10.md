# Verification findings — 2026-08-10

Follow-up round addressing the eight findings in
[`verification-2026-07-31.md`](verification-2026-07-31.md). Findings #1–#7
from that document are now `fixed`; #8 is unchanged (dev-environment only).
This round also turned up and fixed one additional issue not present in the
prior report — see finding #8 below.

**Method:** static code review of `custom_components/fints_atruvia/` plus the
existing and newly added unit tests (`PYTHONPATH=$PWD .venv/bin/pytest -q`).
No real Home Assistant instance was started and no runtime harness was run
for this round — the `.claude/skills/verify/` runtime verification (the
method used for the 2026-07-31 report) is still outstanding for these eight
fixes. Treat the items below as reviewed against the source and against unit
tests, not against a running HA instance.

Eight bugs were found during that review and fixed in this round, on branch
`fix/verification-findings-2026-08-10`:

---

## 1. `TypeError` instead of `ConfigEntryAuthFailed` on TAN-required during restart

**Severity:** high (breaks the documented 90-day SCA/re-auth flow) · **Status:** fixed

`coordinator.py`, in `_async_update_data`'s `except TanRequiredError as e:`
block, `raise ConfigEntryAuthFailed(...) from e.response` tried to chain a
`NeedTANResponse` as the exception cause. `NeedTANResponse` is not a
`BaseException`, so Python raised `TypeError: exception causes must derive
from BaseException` instead of the intended `ConfigEntryAuthFailed` — hit on
the "HA restarts while an SCA confirmation is still pending" path.

**Fix:** changed `from e.response` to `from e` (Task 1, commit `01f8d77`).
Regression test: `test_tan_required_before_first_success_raises_auth_failed`
in `tests/test_coordinator.py`, which triggers the error with a plain
`SimpleNamespace()` response object (not a real `NeedTANResponse`) as the
guard against this exact bug.

---

## 2. Seen-transaction hashes written before the update actually succeeded

**Severity:** medium (silent, permanent event loss) · **Status:** fixed

`coordinator.py`'s `_detect_new_transactions` mutated
`self._seen_hashes[iban]` as its last statement, inside the per-account loop
of `_async_update_data`. If a later account in the same poll raised
`TanRequiredError` or any other error, the loop aborted, but hashes for
accounts already processed in that same poll had already been written. On
the next successful poll, those transactions looked already-seen and their
`fints_atruvia_new_transaction` event never fired.

**Fix:** `_detect_new_transactions` no longer mutates `self._seen_hashes`; it
returns `tuple[list[dict], set[str]]` (events, snapshot). `_async_update_data`
collects per-IBAN snapshots into a local `pending_seen` dict during the loop
and only merges them into `self._seen_hashes` after the whole poll round has
succeeded, right before persisting (Task 1, commit `01f8d77`). Regression
test: `test_no_lost_events_when_later_account_fails` in
`tests/test_coordinator.py`.

---

## 3. `async_shutdown` skipped the base-class shutdown

**Severity:** low · **Status:** fixed

`coordinator.py`'s `async_shutdown` wiped the PIN and closed the FinTS client
without calling `super().async_shutdown()`, so HA's
`DataUpdateCoordinator.async_shutdown()` never ran — scheduled refreshes and
the shutdown listener were not unsubscribed.

**Fix:** added `await super().async_shutdown()` as the first statement (Task
1, commit `01f8d77`). Regression test:
`test_async_shutdown_wipes_pin_closes_client_and_calls_super`, which asserts
`coordinator._shutdown_requested is True` — a flag only the base class sets.

---

## 4. `config_entry` not passed to `DataUpdateCoordinator.__init__`

**Severity:** low (forward-compat) · **Status:** fixed

`FintsBankingCoordinator.__init__` set `self.config_entry = config_entry`
manually instead of passing it to `super().__init__()`. HA's fallback
(reading the entry from a `ContextVar`) still worked today but is scheduled
to break in HA 2026.8 (`frame.report_usage(..., breaks_in_ha_version=
"2026.8")`).

**Fix:** pass `config_entry=config_entry` as a keyword to `super().__init__()`
(Task 1, commit `01f8d77`). Regression test:
`test_config_entry_is_set_via_super_init`.

---

## 5. `FintsStateStore` could not read real v1 state files

**Severity:** high (breaks setup for any pre-encryption install) · **Status:** fixed

`storage.py`'s `FintsStateStore` (version 2, Fernet-encrypted) was documented
as reading legacy v1 (plaintext-hex) state files transparently, but HA's
`Store._async_load_data` re-raises `NotImplementedError` from the default
`_async_migrate_func` whenever the on-disk `version` doesn't match the
store's declared version. Any real installation still holding a v1 state
file from before the Fernet-encryption change would fail during setup. The
existing regression test wrote through the current version-2 store directly,
so it never exercised HA's migration path and passed regardless of the bug.

**Fix:** added `_MigratingStore(Store)`, whose `_async_migrate_func` returns
`old_data` unchanged instead of raising; `FintsStateStore` now builds its
`Store` via this subclass (Task 2, commit `232c696`). New tests using the
`hass_storage` fixture to seed a byte-for-byte real v1 file:
`test_state_store_reads_real_v1_file`,
`test_state_store_migrates_v1_file_on_next_save`,
`test_state_store_rejects_corrupt_v1_hex`, all in `tests/test_storage.py`.

---

## 6. Config-entry `unique_id` stored the bank login in cleartext

**Severity:** medium (credential exposure via `.storage/core.config_entries`, WS API) · **Status:** fixed

`config_flow.py` set the config entry's `unique_id` to `f"{blz}_{username}"`.
Unlike `entry.data` (which only ever holds `credential_id`), this value is
neither encrypted nor masked — it is written in cleartext to
`.storage/core.config_entries` and returned by the `config/config_entries/get`
WebSocket API to any client with read access.

**Fix:** added `_entry_unique_id(key, blz, username)` in `__init__.py` — an
HMAC-SHA256 keyed by the integration's master Fernet key, with a
`entry_unique_id|` domain-separation prefix, truncated to 16 hex chars. A
plain unsalted hash (as first implemented) would have been offline
brute-forceable, since the BLZ is cleartext in the same storage file and
NetKey logins are short; keying the hash with the master Fernet key (already
present on disk for credential encryption) closes that gap. The value stays
reproducible within one install — both call sites run async with `hass`
available and fetch the persistent key first — which is all the dedup for
new entries and the migration of existing ones need. `async_step_user` now sets the unique_id via
`self.async_set_unique_id(_entry_unique_id(...))`; the config-flow `VERSION`
was bumped to 3, with a new v2→v3 migration step in `async_migrate_entry`
that decrypts the stored credential, rehashes the unique_id, and falls back
to leaving the legacy unique_id in place (only bumping the version) if the
credential can't be decrypted (Task 4, commit `83d73ed`). Regression tests in
`tests/test_init.py`: `test_migrate_entry_v1_to_v3_chains_both_steps`,
`test_migrate_entry_v2_to_v3_hashes_unique_id`,
`test_migrate_entry_v2_to_v3_keeps_legacy_unique_id_if_undecryptable`,
`test_migrate_entry_v2_to_v3_survives_unique_id_collision`; pure-function
tests in `tests/test_pure.py`.

---

## 7. FinTS client with a live PIN reference never closed on an abandoned flow

**Severity:** low (resource/PIN-lifetime hygiene) · **Status:** fixed

`config_flow.py` built a `FinTsAtruviaClient` (holding the PIN via its
`pin_provider` callable) during the flow, but if the user abandoned the flow
before `_finish_setup` ran, nothing ever called `client.close()` — the client
and its PIN reference stayed alive for the lifetime of the flow-handler
object.

**Fix:** added `FintsBankingConfigFlow.async_remove()`, HA's designated
flow-teardown hook, which closes `self._client` (if set) via
`hass.async_add_executor_job` and clears the attribute. `_finish_setup` also
closes and clears `self._client` at its own entry point, covering the normal
completion path (Task 4, commit `83d73ed`).

---

## 8. Card header attribute broke out of its quotes on a `"` in the title or IBAN

**Severity:** medium (attribute-context XSS) · **Status:** fixed

`frontend/src/fints-card.js`'s `_renderEntity` interpolated the
user-configured `title:` and the bank-supplied `last4` into
`<ha-card header="${headerText}">` via `escapeHtml()`. `escapeHtml()` only
escapes `&`, `<`, `>` (and NBSP) — the characters the HTML serializer treats
as special in element *text content* — because quotes are only special
inside an attribute value, so it doesn't escape `"` at all. A `"` in the
configured title, or in a bank-supplied IBAN reaching `last4`, could close
the `header="..."` attribute early and inject arbitrary attributes (or
markup) before the string reached `shadowRoot.innerHTML`.

**Fix:** added a dedicated `escapeAttr()` helper (`escapeHtml()`'s output
plus `.replaceAll('"', "&quot;").replaceAll("'", "&#39;")`) and routed both
branches of the `header="${headerText}"` interpolation through it instead of
`escapeHtml()`, in `frontend/src/fints-card.js` and the built
`custom_components/fints_atruvia/www/fints-atruvia-card.js` (commit
`f01b7ed`). The remaining `class="${...}"` interpolations in the same file
were audited and confirmed to carry only fixed local constants, not
untrusted data, so they don't need the same treatment.

---

## Not yet re-verified at runtime

The eight fixes above are covered by unit tests and were reviewed against
the source, but none of them has been exercised against a running Home
Assistant instance yet. In particular, worth confirming with
`.claude/skills/verify/` before the next release:

- The v1→v3 and v2→v3 config-entry migrations against a real
  `.storage/core.config_entries` file (not just `MockConfigEntry`).
- That the new options-update-listener reload (finding #3 in the prior
  report) doesn't trigger an unexpected SCA prompt on a real Atruvia
  instance.
- The card's `title:` and transaction-ordering behaviour rendered in an
  actual dashboard, beyond the `node -e` extraction used in Task 5.
