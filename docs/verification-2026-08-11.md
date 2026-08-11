# Verification findings — 2026-08-11

Runtime verification of the eight fixes documented in
[`verification-2026-08-10.md`](verification-2026-08-10.md), plus the release
metadata and lint changes that landed on `release/v0.5.0` after that document
was written. That round was static review plus unit tests only; this one puts
every one of its claims in front of a running Home Assistant.

**Method:** real Home Assistant **2026.3.2** (Python 3.14.4) started against a
synthetic config sandbox under `SB_ROOT=/tmp/fints-verify`, driven via
`.claude/skills/verify/` (`harness/run.sh`). The sandbox is written from
scratch by `harness/make_sandbox.py`: a **v1** config entry with plaintext
`username`/`password`, the historical cleartext `unique_id`
`99999999_sandboxuser`, and four entity-registry rows carrying legacy
`{entry_id}_{IBAN}` unique_ids. `fints.client.FinTS3PinTanClient` was replaced
by the harness's offline fake bank for the whole run — **no real bank was
contacted, no real credentials were touched, and the developer's own
`config/` directory was never read, copied or started against.** The component
logged at `DEBUG` throughout.

Covered: the v1 → v3 config-entry migration against a real
`.storage/core.config_entries`, the entity unique-ID migration, entry setup
(`__init__`, `coordinator`, `frontend`, `sensor`, `button`), the full config
flow (including abandoning it), the options flow in both directions, the
reauth flow end to end, the SCA/re-auth button lifecycle, new-transaction
events with the disclosure toggle on and off, the Lovelace resource across a
version bump, and the card in Chrome (Playwright, logged in as `dev`).

Not covered:

- Anything that depends on a real Atruvia gateway's *answers*: whether the
  options-toggle reload provokes a fresh SCA prompt on a real instance is
  still open (see "Extra checks", 2).
- Long-horizon behaviour. `update_interval` is 6 hours and cannot be shortened
  without touching production code, so an orphaned refresh timer (fix 3) would
  only become observable hours after a reload.
- Whether `FinTsAtruviaClient.close()` is really called on an abandoned config
  flow (fix 7): the fake bank has no observable for it. See finding 5.
- A second Home Assistant version. Two of the 2026-08-10 findings turn out to
  be version-dependent (fixes 4 and 6 below); both were only checked against
  2026.3.2.

Baseline values on a healthy poll matched the harness's expectation exactly:
balance `1234.56` EUR, Einnahmen 30T `2500.0`, Ausgaben 30T `150.99`,
`count_30d` `4`, `iban` attribute `DE89 **** **** **** **** 3000`.

---

## Re-verification of the eight 2026-08-10 fixes

### 1. `TypeError` instead of `ConfigEntryAuthFailed` on TAN-required during restart

**Severity:** high · **Status:** verified fixed at runtime

`touch flags/tanmode`, then a full Home Assistant **restart** (not a reload),
so `coordinator.data` is `None` on the first refresh. The entry ended up in
`setup_error` with the intended reason and a reauth flow waiting:

```
"state": "setup_error",
"reason": "Bank requires initial authentication (TAN)"

config_entries/flow/progress →
  {"flow_id": "01KZR9ECGBARE4Y5NWJH8NGFD8", "handler": "fints_atruvia",
   "context": {"source": "reauth", "entry_id": "01KSANDBOX000000000000FINT",
               "unique_id": "6f2ac3e59d15a6f3"},
   "step_id": "reauth_confirm"}
```

`ha.log` shows the exception chain the fix intends, with `TanRequiredError` as
the direct cause:

```
WARNING [homeassistant.config_entries] Config entry 'Sandbox Bank' for fints_atruvia
  integration could not authenticate: Bank requires initial authentication (TAN)
  ...
  File ".../custom_components/fints_atruvia/api.py", line 358, in get_balance
    raise TanRequiredError(segment)
custom_components.fints_atruvia.api.TanRequiredError: Bank requires TAN authentication

The above exception was the direct cause of the following exception:
  ...
  File ".../custom_components/fints_atruvia/coordinator.py", line 336, in _async_update_data
    raise ConfigEntryAuthFailed(_MSG_INITIAL_TAN_REQUIRED) from e
homeassistant.exceptions.ConfigEntryAuthFailed: Bank requires initial authentication (TAN)
```

`grep -c TypeError` over every log file of the run: **0 hits**. The fake bank
returns a real `NeedTANResponse` (built via `__new__`), so the pre-fix
`raise ... from e.response` would have produced
`TypeError: exception causes must derive from BaseException` right here.

The reauth flow was then completed against the same entry (flag removed first):
`reauth_confirm` pre-filled `username` with `sandboxuser`, the account step
offered `Konto …3000` / `Konto …2051`, and submitting ended in
`{"type": "abort", "reason": "reauth_successful"}` with the entry back to
`loaded` and the **same** `credential_id` `67e61726d50e46b7bf15804fe4d1459a` in
`entry.data`.

### 2. Seen-transaction hashes written before the update actually succeeded

**Severity:** medium · **Status:** verified fixed at runtime

This needed a poll that fails *after* an earlier account was already
processed, which the fake bank could not do — see finding 5. With the added
`tanmode2` switch (SCA for the second account only) and both accounts
selected:

*Failing poll* (`extra1`, `extra2`, `tanmode2` set) — the two new bookings on
`DE89…3000` were detected, then `DE02…2051` raised `TanRequiredError`:

- events fired during a 22 s subscription to `fints_atruvia_new_transaction`:
  **0**
- `.storage/fints_atruvia_seen_transactions_01KSANDBOX000000000000FINT` was
  **byte-identical** to the pre-poll snapshot (`diff -q` → unchanged), still
  `{'DE89370400440532013000': 4, 'DE02120300000000202051': 4}` — the two new
  hashes were *not* committed
- both sensors kept their last-known-good value `1234.56` with
  `2fa_pending: true` and 4 (not 6) transactions

*Recovery poll* (`tanmode2` removed) — all four events still fired, two per
account:

```
EVENT {"iban_last4": "3000", "amount": -7.35,  "purpose": "SANDBOX-PURPOSE-BAECKEREI-NEU"}
EVENT {"iban_last4": "3000", "amount": -13.37, "purpose": "SANDBOX-PURPOSE-ZWEITE-NEUE"}
EVENT {"iban_last4": "2051", "amount": -7.35,  "purpose": "SANDBOX-PURPOSE-BAECKEREI-NEU"}
EVENT {"iban_last4": "2051", "amount": -13.37, "purpose": "SANDBOX-PURPOSE-ZWEITE-NEUE"}
```

Seen store then `{'DE89370400440532013000': 6, 'DE02120300000000202051': 6}`.
Pre-fix, the `3000` events would have been lost permanently.

### 3. `async_shutdown` skipped the base-class shutdown

**Severity:** low · **Status:** verified as far as runtime allows

Nine entry reloads (six explicit API reloads, two self-reloads from the
options toggle, one from the reauth flow) plus three full restarts produced a
clean log: **0**
`Detected blocking call`, **0** `Task exception was never retrieved`, **0**
lingering-task/timer warnings, **0** errors from the `fints_atruvia` logger,
and the entry back to `loaded` after every reload. Events did not re-fire
across reloads (see "Verified-good").

Limitation, stated plainly: the pre-fix bug's only distinguishable symptom is a
scheduled refresh surviving on an unloaded coordinator, and
`update_interval` is 6 hours. An attempt to force the debounced path (two
rapid `homeassistant.update_entity` calls immediately followed by a reload)
did not discriminate — the reload won the race, HA logged its own
`Forced update failed. Entity sensor.konto_3000 not found.` and nothing
further. So this run confirms reloads are clean; it does not independently
prove the base-class cleanup ran. Note that HA registers
`config_entry.async_on_unload(self.async_shutdown)` inside
`DataUpdateCoordinator.__init__` (`update_coordinator.py:150-151`), so the
override is on the unload path either way — the fix is still the right one.

### 4. `config_entry` not passed to `DataUpdateCoordinator.__init__`

**Severity:** low · **Status:** fix confirmed harmless and effective; **the
proposed probe is not a valid discriminator**, and the stated justification is
wrong for a custom integration

HA 2026.3.2's `helpers/update_coordinator.py:97-110` reads:

```python
if config_entry is UNDEFINED:
    from . import frame
    # It is not planned to enforce this for custom integrations.
    # see https://github.com/home-assistant/core/pull/138161#discussion_r1958184241
    frame.report_usage(
        "relies on ContextVar, but should pass the config entry explicitly.",
        core_behavior=frame.ReportBehavior.ERROR,
        custom_integration_behavior=frame.ReportBehavior.IGNORE,
        breaks_in_ha_version="2026.8",
    )
```

`custom_integration_behavior=ReportBehavior.IGNORE` means a custom integration
gets **no log line at all** on the old code path, so "log free of the
`report_usage` deprecation" proves nothing either way — and HA's own comment
says enforcement for custom integrations "is not planned". The 2026-08-10
claim that the old code "is scheduled to break in HA 2026.8" therefore
overstates the risk; see finding 4.

What the run does confirm: `config_entry=` is accepted by this HA version's
signature (setup succeeded), and the coordinator's `config_entry` resolves to
the sandbox entry — entity unique_ids are scoped with
`01KSANDBOX000000000000FINT`, `expose_full_data` is read live from
`entry.options` (see "Extra checks", 2), and
`async_config_entry_first_refresh` passed its own
`self.config_entry.state` assertion.

### 5. `FintsStateStore` could not read real v1 state files

**Severity:** high · **Status:** verified fixed at runtime

A byte-for-byte v1 file was placed in the sandbox:

```json
{"version": 1, "minor_version": 1,
 "key": "fints_atruvia_state_67e61726d50e46b7bf15804fe4d1459a",
 "data": {"blob": "46414b452d46494e54532d56312d504c41494e544558542d…"}}
```

Reloading the entry produced, in order:

```
INFO [homeassistant.helpers.storage] Migrating fints_atruvia_state_67e61726d50e46b7bf15804fe4d1459a
     storage from 1.1 to 2.1
DEBUG [custom_components.fints_atruvia.coordinator] Finished fetching fints_atruvia data
     in 0.002 seconds (success: True)
```

and `fakebank.log` recorded `restored_state=yes`. That is conclusive: the file
carried no `ciphertext` key, so a truthy `from_data` can only have come from
the `bytes.fromhex(data["blob"])` branch, and HA's loader ran
`_async_migrate_func` without raising. The next save rewrote the file to
`{"ciphertext": "gAAAAABqewaEOeZUbb0x…"}` at mode `0600`. Without
`_MigratingStore` this reload would have aborted setup with
`NotImplementedError`.

### 6. Config-entry `unique_id` stored the bank login in cleartext

**Severity:** medium · **Status:** verified fixed at runtime

After the first start, `.storage/core.config_entries` holds:

```json
"unique_id": "6f2ac3e59d15a6f3",
"version": 3,
"data": {"blz": "99999999", "credential_id": "67e61726d50e46b7bf15804fe4d1459a",
         "product_id": "SANDBOXPRODUCT0000000001",
         "selected_accounts": ["DE89370400440532013000"],
         "url": "https://fints.sandbox.invalid/fints30"}
```

- `grep -r sandboxuser $SB_ROOT/config/.storage` → **0 hits** across all 18
  files present at that point (the pre-migration value was
  `99999999_sandboxuser`)
- `grep -r SandboxPIN12345` → **0 hits**
- `unique_id` is 16 lowercase hex characters
- migration log: `Migrated fints_atruvia entry 01KSANDBOX000000000000FINT to
  encrypted credential storage.`
- `fints_atruvia_master_key`, `fints_atruvia_credentials_<id>`,
  `fints_atruvia_state_<id>` all exist at mode `0600`

API side: `config_entries/get`, `config_entries/get_single` and
`GET /api/config/config_entries/entry` return the hash **not at all** — HA
2026.3.2's `ConfigEntry.as_json_fragment` (`config_entries.py:633-655`) has no
`unique_id` key. `grep -c unique_id` over those responses: 0. The only place
the hash surfaces over the API is a flow's `context.unique_id`
(`6f2ac3e59d15a6f3`, quoted under fix 1). So the exposure the fix closes is
real for `.storage/core.config_entries` and backups, but the 2026-08-10 claim
about the WebSocket API does not hold on this version — see finding 4.

Worth knowing, unchanged by this fix: the reauth form still returns the stored
login to the browser as the `username` default (`_suggested_username`,
observed as `'sandboxuser'`). That is deliberate UX for an admin-only flow, not
data at rest.

### 7. FinTS client with a live PIN reference never closed on an abandoned flow

**Severity:** low · **Status:** teardown path exercised; **the fix itself is
not observable with this harness**

A config flow was driven to the account step with a distinct login
(`blz=88888888`, `username=otheruser`, 18-character PIN) — `fakebank.log`
confirms the flow built a client:

```
CONNECT server=https://fints.sandbox.invalid/fints30 user=otheruser blz=88888888
        product=SANDBOXPRODUCT0000000001 pin_len=18 restored_state=no
```

`DELETE /api/config/config_entries/flow/<id>` returned
`200 {"message":"Flow aborted"}`, `config_entries/flow/progress` then returned
`[]`, no config entry was created, and `ha.log` shows no exception from
`async_remove` and no `Task exception was never retrieved` (which is how a
raising `close()` submitted via `async_add_executor_job` without a result
handler would surface).

The task brief for this round expected `fakebank.log` to "show the client as
ended" (`SKILL.md` never made that claim). It cannot: the fake logs `CONNECT`
and `SEND_TAN` only, and by the time
`close()` runs the standing dialog has already been closed inside
`init_system_id`, so `close()` merely drops references — nothing the fake can
see. Instrumenting the fake with `__del__` would not discriminate either,
because the abandoned flow object becomes garbage regardless of whether
`async_remove` exists. Recorded as a harness limitation (finding 5), not as
evidence of a problem.

### 8. Card header attribute broke out of its quotes on a `"` in the title

**Severity:** medium · **Status:** verified fixed at runtime

A dashboard was created through HA's own APIs with
`title: 'Spar"da><img src=x onerror="window.__XSS_TITLE__=1">'` and opened in
Chrome. Walking the nested shadow roots:

```
raw ha-card tag:
  <ha-card header="Spar&quot;da&gt;&lt;img src=x onerror=&quot;window.__XSS_TITLE__=1&quot;&gt;">
parsed header attribute:  Spar"da><img src=x onerror="window.__XSS_TITLE__=1">
rendered header text:     Spar"da><img src=x onerror="window.__XSS_TITLE__=1">
img elements in card: 0    script elements in card: 0
img elements in document: 0
typeof window.__XSS_TITLE__: "undefined"
```

The `"` is emitted as `&quot;`, so the attribute never terminates early. The
title is also honoured in the header at all (the 2026-07-31 finding 5 fix),
with `Konto 3000` still shown as the account line below it.

The bank-text path was re-checked in the same browser with the `xss` flag: the
hostile `purpose` `<img src=x onerror="window.__XSS__=1">PWNED` is rendered as
escaped text (`&lt;img src=x onerror=…`), `window.__XSS__` and
`window.__XSS2__` are both `undefined`, and no `<img>`/`<script>` element
exists anywhere in the document.

---

## Extra checks for this release

**1. Lovelace resource across the version bump — pass.** The sandbox was
started with `manifest.json` temporarily at `0.4.0`:

```json
{"id": "14b26598c6e84e7d9961a36aaa4d5909",
 "url": "/fints_atruvia/fints-atruvia-card.js?v=0.4.0", "type": "module"}
```

`manifest.json` was then restored to `0.5.0` and HA restarted. `lovelace/resources`
and `.storage/lovelace_resources` both show **exactly one** item, the **same
`id`**, updated in place:

```json
{"id": "14b26598c6e84e7d9961a36aaa4d5909",
 "url": "/fints_atruvia/fints-atruvia-card.js?v=0.5.0", "type": "module"}
```

`GET /fints_atruvia/fints-atruvia-card.js?v=0.5.0` → `200`, 14553 bytes,
md5 `c2dd41862f8506e7b18226529e161c1c`, identical to the committed
`custom_components/fints_atruvia/www/fints-atruvia-card.js` (and to
`frontend/src/fints-card.js`).

**2. Options toggle without a manual reload — pass, with the SCA question
still open.** `expose_full_data` was written through the options flow at
`13:25:39.235`; the entry reloaded itself and the sensor already carried the
`transactions` attribute (4 entries, with `purpose` and `creditor`) when read
**2 s** later, at `13:25:39.250`. Toggling it back off removed the attribute
just as fast. The reload cost **exactly one** extra bank dialog
(`CONNECT … restored_state=yes`) and **zero** `SEND_TAN` lines in
`fakebank.log`. Since the offline fake never volunteers an SCA challenge on a
state-restoring connect, this answers the 2026-07-31 question only halfway:
the toggle causes one additional dialog, and whether a real Atruvia gateway
answers that dialog with a fresh SecureGo+ prompt is still unverified.

**3. Card transaction ordering — pass.** With `extra1` and `extra2` set the
attribute arrives in the bank's own order, which is *not* sorted:

```
2026-08-09, 2026-08-06, 2026-08-02, 2026-07-22, 2026-08-11, 2026-08-11
```

The rendered card shows the five **newest**, descending, and drops the oldest:

```
11.08.2026  -7,35 €    SANDBOX-PURPOSE-BAECKEREI-NEU
11.08.2026  -13,37 €   SANDBOX-PURPOSE-ZWEITE-NEUE
09.08.2026  -42,99 €   SANDBOX-PURPOSE-GROCERIES-4711
06.08.2026  -19,90 €   SANDBOX-PURPOSE-STREAMING-ABO
02.08.2026  2.500,00 € SANDBOX-PURPOSE-GEHALT-JULI
```

Unsorted, the first five of that input would have been
`09.08, 06.08, 02.08, 22.07, 11.08` — the 2026-07-31 symptom.

**4. Log hygiene — pass.** All four HA logs of the run (`ha.log`, `ha.log.1`,
plus two snapshots taken before restarts; 102 KB in total, with the component
at `DEBUG` — 12 `custom_components.fints_atruvia` DEBUG lines in the final
boot's log alone) plus `ha.stdout`, grepped for every secret in play:

| pattern | hits |
|---|---|
| `SandboxPIN12345` (entry PIN) | 0 |
| `OtherFlowPIN987654` (config-flow PIN) | 0 |
| `DE89370400440532013000` | 0 |
| `DE02120300000000202051` | 0 |
| `0000123456` / `0000654321` (account numbers) | 0 |
| `sandboxuser` / `otheruser` (logins) | 0 |

The only `custom_components/fints_atruvia` traceback frames in any log (4) are
the `ConfigEntryAuthFailed` chain quoted under fix 1, which HA itself logs at
`DEBUG`; its messages are the integration's own constants, not bank text.
Neither PIN appears anywhere under `SB_ROOT` outside the encrypted blob.

**5. `TYPE_CHECKING` import moves — pass.** The lint pass moved imports into
`TYPE_CHECKING` blocks in **all eight** modules (`__init__`, `api`, `button`,
`config_flow`, `coordinator`, `frontend`, `sensor`, `storage`). Every module
was executed in this run, with a distinct piece of evidence each: the
migration log line (`__init__`), `Generated new fints_atruvia master key`
(`storage`), `CONNECT` in `fakebank.log` (`api`), `Finished fetching
fints_atruvia data` (`coordinator`), the registered Lovelace resource and the
served card (`frontend`), `Setting up fints_atruvia.sensor` / `.button`, and
the rendered flow forms (`config_flow`, including `_account_labels`, whose
`SEPAAccount` import was the one moved out of the runtime path). `grep -E
"ImportError|NameError|AttributeError"` over every log: **0 hits**.

---

## New findings from this run

### 1. The plaintext IBAN reaches the recorder database via the unique-ID migration

**Severity:** medium (privacy claim not fully met) · **Status:** documentation
corrected (`6a13e74`) — the underlying behaviour remains open and is not
fixable by the integration; see the update below

`_async_migrate_unique_ids` goes through `er.async_migrate_entries`, and HA
fires an `entity_registry_updated` event per rewritten row whose `changes`
payload carries the **old** value. The recorder persists those events, so the
plaintext IBAN lands in `config/home-assistant_v2.db`:

```
entity_registry_updated 2026-08-11T13:21:22
  {"action":"update","entity_id":"sensor.konto_3000",
   "changes":{"unique_id":"01KSANDBOX000000000000FINT_DE89370400440532013000"}}
entity_registry_updated 2026-08-11T13:21:22
  {"action":"update","entity_id":"sensor.konto_3000_einnahmen_30t",
   "changes":{"unique_id":"01KSANDBOX000000000000FINT_DE89370400440532013000_income_30d"}}
entity_registry_updated 2026-08-11T13:21:22
  {"action":"update","entity_id":"sensor.konto_3000_ausgaben_30t",
   "changes":{"unique_id":"01KSANDBOX000000000000FINT_DE89370400440532013000_expense_30d"}}
```

(Three rows in `event_data.shared_data`, timestamped at the migration. The
second account, whose entities were created after the fix and never migrated,
produces no such rows.)

This is the same class as the 2026-07-31 finding 1 (`previous_unique_id`
residue), which was fixed for the registry file — `previous_unique_id` is
verified `None` on all seven rows here — but the migration *event* was not
considered.

**The claim this falsifies is `SECURITY.md:62`** (§5, "IBAN-Maskierung —
flächendeckend"):

> Die volle IBAN steht damit weder in `home-assistant_v2.db` noch im Event-Bus
> noch im aktiven `unique_id`-Feld von `core.entity_registry`.

Two of those three clauses are false on the migration path: the plaintext IBAN
reaches the **event bus** (`entity_registry_updated`) and, through the
recorder, **`home-assistant_v2.db`**. Only the third clause — the active
`unique_id` field — holds, and it is verified above.

Two documents that might look like the target are *not*:
`iban_unique_id()`'s docstring (`custom_components/fints_atruvia/__init__.py:71-81`)
scopes its claim explicitly to the entity registry and is accurate as written,
and `CLAUDE.md` makes no recorder or event-bus claim at all.

Scope: only installs that actually migrate legacy unique_ids (an upgrade from
a pre-hashing version), once, and only until the recorder purge window passes
(default `purge_keep_days: 10`) — but any backup taken inside that window
carries it, next to the existing plaintext-PIN backup caveat.

**Options** — the integration cannot suppress the event, since HA fires it, so
the remediation is a **correction of `SECURITY.md:62`**, not an addition
elsewhere: narrow that sentence to the active `unique_id` field and record the
migration-event residue as a second `Restrisiko` next to the existing
`previous_unique_id` one, optionally with a recorder-purge recommendation.
Adding a note *beside* the sentence would leave an unretracted false claim in
the shipped security documentation.

**Update (final review, `6a13e74`):** done as proposed. `SECURITY.md:62` now
narrows the claim to the active `unique_id` field and records this
migration-event residue as a second `Restrisiko`, with the recorder-purge
window and the backup caveat. That closes the *documentation* gap — the
sentence this finding falsified no longer makes the false claim. It does not
and cannot close the *behaviour*: HA still fires `entity_registry_updated`
with the old value on migration, and the recorder still persists it until
`purge_keep_days` elapses; the integration has no hook to suppress either.
That part of the finding stays open as a product limitation, tracked in
`SECURITY.md:62` rather than as an unfinished to-do here.

### 2. Adding an account to an existing entry replays its whole 30-day history as events

**Severity:** low (event flood / spurious automation triggers) · **Status:** open

`_seen_initialised` is a single per-entry flag, but `_seen_hashes` is keyed per
IBAN. Once the flag is set, an IBAN with no entry in the seen-set is treated as
having seen nothing, so *every* transaction in the 30-day window counts as new.

Reproduced by dropping the second account's key from
`.storage/fints_atruvia_seen_transactions_01KSANDBOX000000000000FINT` and
reloading — the exact state a user reaches by adding an account through the
reauth flow (which is the only way `selected_accounts` can change): **6**
`fints_atruvia_new_transaction` events fired at once, all with
`iban_last4: "2051"`, one per historical booking. The documented
"first run after install seeds the set silently" guarantee does not extend to
a newly tracked account on an existing entry.

### 3. `transaction_hash` is not account-scoped

**Severity:** low (informational) · **Status:** open

`_transaction_hash` hashes `date|amount|purpose|creditor` only, so the same
booking on two accounts produces the same hash. Observed directly — the two
events below differ only in the IBAN fields:

```
{"iban_last4": "3000", "transaction_hash": "fdfd415b3d01a479217f390fc6c2e4c1f47999cddfb181dc0b23d8caf1976f9f"}
{"iban_last4": "2051", "transaction_hash": "fdfd415b3d01a479217f390fc6c2e4c1f47999cddfb181dc0b23d8caf1976f9f"}
```

Dedup itself is unaffected (the seen-set is keyed per IBAN), but an automation
that dedupes on `transaction_hash` alone across accounts would silently drop
one of the two. The fake bank returns identical transactions for both
accounts, which exaggerates how often this happens in reality; an internal
transfer between two accounts of the same login is the realistic case. Worth a
sentence in the event documentation rather than a code change.

### 4. The "served over the WebSocket API" claim is wrong on 2026.3.2, in four places

**Severity:** trivial (documentation) · **Status:** corrected — all four
places now carry the at-rest framing instead

Both claims were checked against the installed HA 2026.3.2 source:

- The 2026-08-10 finding 6 says the cleartext `unique_id` was "returned by the
  `config/config_entries/get` WebSocket API to any client with read access".
  It is not: `ConfigEntry.as_json_fragment` (`config_entries.py:633-655`) does
  not include `unique_id`, and neither `config_entries/get`,
  `config_entries/get_single` nor the REST equivalent returned it here. The
  at-rest exposure in `.storage/core.config_entries` (and backups, and
  diagnostics) is real and is reason enough for the fix.
- The 2026-08-10 finding 4 says the `ContextVar` fallback "is scheduled to
  break in HA 2026.8". For a *custom* integration it is not — the call passes
  `custom_integration_behavior=ReportBehavior.IGNORE` and carries the upstream
  comment "It is not planned to enforce this for custom integrations."

The WebSocket-API half travelled beyond that report. It was corrected in
`verification-2026-08-10.md` by this round's amendment, and the three
code/doc locations below — outside this task's scope to edit at the time —
have since all been narrowed too, in two rounds:

| location | state |
|---|---|
| `SECURITY.md:88` (§8) | corrected (`6a13e74`) |
| `custom_components/fints_atruvia/__init__.py:89-101` (`_entry_unique_id` docstring) | corrected (`6a13e74`) |
| `custom_components/fints_atruvia/__init__.py:243-244` (`async_migrate_entry` docstring) | corrected (final-review fix wave) |

`6a13e74` missed the third row; the final whole-branch review caught it
(alongside a fourth location this finding never listed, `CHANGELOG.md:29`,
which named a WebSocket command — `config/config_entries/get` — that does not
exist either) and both were fixed together. All are now narrowed to the
at-rest exposure (storage file, backups, diagnostics), which is what actually
motivates the HMAC. None of this changed the fix itself; the false framing
would only have misled the next reader.

### 5. Harness gaps found while probing (harness only, no production impact)

**Severity:** low · **Status:** one fixed in the harness, one open

- **Fixed:** the fake bank's `tanmode` flag failed *every* call, so a
  multi-account poll could not be made to fail after the first account had
  been processed — which is exactly what fix 2 is about. Added a `tanmode2`
  flag (SCA for `DE02…2051` only) to `harness/fake_fints/sitecustomize.py`
  and documented it in `SKILL.md`'s flag table. This is a harness change made
  during a verification run; it is committed separately from the report.
- **Open:** the fake bank has no observable for a client being closed, so the
  abandoned-flow teardown of fix 7 cannot be proven at runtime (see fix 7
  above). The task brief for this round implied it could; `SKILL.md` never
  claimed it (its "Flows worth driving" list does not contain the
  abandoned-flow probe), and it now carries the limitation explicitly so the
  next run does not go looking.

---

## Verified-good (no action)

- **v1 → v3 migration in one start**: entry at `version 3`, `entry.data` with
  only `blz` / `url` / `product_id` / `selected_accounts` / `credential_id`,
  no `username` / `password`, `unique_id` a 16-hex HMAC. All three integration
  stores at mode `0600`.
- **Entity unique-ID migration**: all four legacy rows rewritten to
  `01KSANDBOX000000000000FINT_609c6c22bcb49f2c`, with `_income_30d` /
  `_expense_30d` preserved and `_reauth_button` untouched; entity_ids
  unchanged (no `_2` duplicates), and `previous_unique_id` `None` on every row.
- **IBAN masking** everywhere it is claimed: sensor attribute
  `DE89 **** **** **** **** 3000`, event payload
  `DE89**************3000` + `iban_last4`, card `DE** **** **** **** 3000`,
  account picker `Konto …3000` / `Konto …2051` (no account number — the
  2026-07-31 finding 2 fix, seen in both the new-entry and the reauth flow).
- **Disclosure toggle in both directions**: off → no `transactions`
  attribute and events carrying only
  `integration_id`/`iban_masked`/`iban_last4`/`date`/`amount`/`currency`/`transaction_hash`;
  on → `purpose` + `applicant_name` in both. Both directions took effect
  without a manual reload.
- **Event dedup**: two repeat polls and three entry reloads in one 40 s
  subscription window fired **0** events; removing a booking pruned its hash
  (6 → 5 per IBAN) and re-adding it fired exactly one event per account.
- **SCA lifecycle**: `tanmode` → persistent notification
  `fints_atruvia_reauth` created, button available, sensor holding
  `1234.56` with `2fa_pending: true`. Pressing the button *before* clearing
  the flag logged `SEND_TAN` and left the state consistent (still pending, no
  crash); clearing the flag and pressing again gave `2fa_pending: false`,
  button back to `unavailable`, notification dismissed — `persistent_notification/get`
  no longer lists it.
- **Encrypted FinTS state round-trip**: second and later connects log
  `restored_state=yes`; the file on disk is `{"ciphertext": …}` at `0600`.
- **Second account at the bank is never exposed by itself**: `DE02…2051`
  appeared only after it was explicitly selected in the reauth flow.
- **Card degradation**: with the toggle off the card renders balance-only
  without errors; with it on the "Letzte Transaktionen (5)" section appears.
- **`manifest.json` restored**: `git diff --exit-code --
  custom_components/fints_atruvia/manifest.json` is clean; the file is back at
  `"version": "0.5.0"`.
- **Regression guards after this round's docs-only commits**:
  `uvx ruff@0.16.2 check .` → `All checks passed!`,
  `uvx ruff@0.16.2 format --check .` → `17 files already formatted`,
  `PYTHONPATH=$PWD .venv/bin/pytest -q` → `68 passed`.
