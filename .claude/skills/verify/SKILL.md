---
name: verify
description: Use when verifying a change to the fints_atruvia integration or its Lovelace card by actually running Home Assistant - builds a sandboxed HA against an offline fake bank, drives it via REST/WebSocket/browser, and captures evidence. Never touches the developer's real config or a real bank.
---

# Verifying fints_atruvia at runtime

The surfaces here are **HA's REST/WebSocket API** (entity states, attributes,
events, config/options flows, services) and **the browser** (Lovelace card).
`pytest` is not verification — the suite already runs in CI.

## Hard rules

- **Never start HA against `config/`.** It contains a real Sparda-Bank entry
  with live credentials, and a run would migrate it irreversibly and dial the
  real bank. Also never copy `config/.storage/*` anywhere.
- **Never let python-fints reach the network.** The harness swaps
  `fints.client.FinTS3PinTanClient` for an offline fake; keep it that way.
- Anything the sandbox learns about the change is evidence; a green test run
  is not.

## Start it

```bash
H=.claude/skills/verify/harness
$H/run.sh bootstrap          # once per machine: HA component deps -> $SB_ROOT/extra (needs net)
$H/run.sh up                 # fresh pre-migration sandbox + HA on :8199 + API token
```

`SB_ROOT` defaults to `$TMPDIR/fints-verify`; everything (config, logs, token,
flags) lives there. `run.sh stop` when finished, and check no `hass` is left
running.

The sandbox is written from scratch by `harness/make_sandbox.py`: a **v1**
config entry with plaintext `username`/`password` plus four entity-registry
rows carrying legacy `{entry_id}_{IBAN}` unique_ids — so the first start
exercises `async_migrate_entry` and `_async_migrate_unique_ids`. Account
`DE89370400440532013000` is selected; a second account exists at the bank and
must never appear.

Expected on a healthy run: balance `1234.56`, Einnahmen `2500.00`, Ausgaben
`150.99`, `count_30d` 4.

## Drive it

```bash
$H/run.sh api /api/states/sensor.konto_3000 | python3 -m json.tool
$H/run.sh api /api/services/homeassistant/update_entity -X POST \
  -H 'Content-Type: application/json' -d '{"entity_id":"sensor.konto_3000"}'   # force a poll
SB_ROOT=... .venv/bin/python $H/ws.py '{"cmds":[{"type":"config_entries/get","domain":"fints_atruvia"}]}'
SB_ROOT=... .venv/bin/python $H/ws.py '{"cmds":[],"event":"fints_atruvia_new_transaction","secs":40}'
```

Options flow (the `expose_full_data` toggle) — the entry reloads itself via
`entry.add_update_listener(_async_reload_entry)`, so no manual reload step
follows: read the sensor attributes right after the options write and confirm
they already reflect the new toggle.

```bash
FID=$($H/run.sh api /api/config/config_entries/options/flow -X POST \
  -H 'Content-Type: application/json' -d '{"handler":"01KSANDBOX000000000000FINT"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['flow_id'])")
$H/run.sh api /api/config/config_entries/options/flow/$FID -X POST \
  -H 'Content-Type: application/json' -d '{"expose_full_data":true}'
```

Fake-bank switches — create/remove files in `$SB_ROOT/flags`, effective on the
next poll, no restart:

| flag | effect |
|---|---|
| `extra1`, `extra2` | extra bookings → new-transaction events |
| `xss` | hostile `purpose`/`creditor` (`<img onerror>`, `<script>`) |
| `tanmode` | bank demands SCA on every call → TAN / re-auth path |
| `tanmode2` | SCA for the second account (`DE02…2051`) only → lets a multi-account poll fail *after* the first account was processed |
| `nohisal` | `get_balance` answers without a HISAL segment → `ValueError("No balance data returned for account …")` in `api.py` |
| `nobooked` | HISAL present but without a booked balance → `ValueError("No booked balance in HISAL response for account …")` |
| `nomech` | bank offers no two-step TAN mechanism (empty BPD/3920) → `NoTanMechanismError` in `api.py` → config-flow error key `no_tan_mechanism` — the regression path for the formerly opaque `KeyError: '999'` from python-fints' `is_tan_media_required` |

`$SB_ROOT/fakebank.log` records each connect (`pin_len`, `restored_state`) and
`SEND_TAN`.

## Flows worth driving

- **Migration** — `.storage/core.config_entries` goes to `version 3` with only
  `credential_id`; `fints_atruvia_master_key` / `_credentials_*` / `_state_*`
  exist at mode `0600`; grep the sandbox `.storage` for `SandboxPIN12345` → no
  hits. The v1 sandbox entry's `unique_id` is the historical cleartext
  `99999999_sandboxuser` — grep for `sandboxuser` too: no hits, and the entry's
  `unique_id` is now a 16-hex HMAC (`_entry_unique_id`, keyed with the master
  Fernet key) instead.
- **Entity unique-IDs** — registry rows become `{entry_id}_{16 hex}` with
  `_income_30d` / `_expense_30d` preserved and `_reauth_button` untouched.
- **Config-entry unique-ID** — distinct from the entity ones above: it is the
  HMAC set on the config entry itself during v2→v3 migration, not an entity
  registry row. Don't confuse the two when grepping.
- **Disclosure toggle** — off: no `transactions` attribute and events carry
  only `iban_masked`/`iban_last4`/amount/date/hash; on (after reload):
  `purpose` + `applicant_name` appear in both.
- **Event dedup** — first run seeds silently; each new booking fires exactly
  once, and neither a repeat poll nor a reload re-fires it.
- **SCA lifecycle** — `touch flags/tanmode`, poll: persistent notification
  appears, button becomes available, sensor keeps its last-known-good value
  with `2fa_pending: true`; remove the flag, press the button
  (`/api/services/button/press`), everything recovers and the notification is
  dismissed. Pressing the button *before* removing the flag is a good probe.
- **Credential loss** — `mv` the master key away and reload the entry: expect
  `setup_error` + a `reauth_confirm` flow; completing it must reuse the same
  `credential_id`.
- **Log hygiene** — grep `$SB_ROOT/ha.log` for the PIN, IBAN, account number
  and username: all must be 0 hits (the component logs at DEBUG here). Two
  IBAN-bearing paths need forcing: the coordinator's "account not found at
  bank" **WARNING** wants an IBAN in `selected_accounts` that the fake does not
  offer (edit `.storage/core.config_entries`, restart), and `api.py`'s two
  `ValueError`s want `nohisal` / `nobooked`. HA formats the latter's
  `__cause__` chain at DEBUG on *two* loggers —
  `helpers/update_coordinator.py` on a poll and `config_entries.py` on the
  setup-time first refresh — so exercise the flag both with HA running and
  across a restart.

## Card (browser)

The integration serves the card itself from
`custom_components/fints_atruvia/www/` and registers the Lovelace resource on
setup — so rebuild into the repo (`cd frontend && npm run build`) and do *not*
create a `/local/` resource by hand; that would load a second copy.

Lay out a dashboard through HA's own APIs and drive Chrome (Playwright),
logging in as `dev` / `sandboxdevpw`. HA's default dashboard is
`home/overview`, and a `lovelace/config/save` without `url_path` lands on a
dashboard that is *not* reachable at `/lovelace/0` — create an explicit one:

```bash
SB_ROOT=... .venv/bin/python $H/ws.py '{"cmds":[
 {"type":"lovelace/resources"},
 {"type":"lovelace/dashboards/create","url_path":"bank-test","title":"Bank Test","mode":"storage","show_in_sidebar":true,"require_admin":false},
 {"type":"lovelace/config/save","url_path":"bank-test","config":{"views":[{"title":"Bank","cards":[
   {"type":"custom:fints-atruvia-card","entity":"sensor.konto_3000"},
   {"type":"entities","entities":["sensor.konto_3000","button.re_authentifizierung_bestatigen"]}]}]}}]}'
```

Then browse `http://127.0.0.1:8199/bank-test/0`.

For the XSS check the card lives in light DOM inside nested shadow roots —
walk them, then assert nothing executed:

```js
const deep=(r,o=[])=>{r.querySelectorAll('*').forEach(e=>{if(e.tagName==='FINTS-ATRUVIA-CARD')o.push(e);
  if(e.shadowRoot)deep(e.shadowRoot,o)});return o};
const root=deep(document)[0]; // expect: no <img>/<script> created, window.__XSS__ undefined,
                              // purpose rendered as &lt;img …&gt;
```

The card renders only `transactions.slice(0, 5)`, so put the transaction you
care about inside the first five (clear `extra1`/`extra2` when using `xss`).

## Gotchas that cost time

- **`pkill -f hass` kills your own shell** — the pattern matches the command
  line that launched it. Use `run.sh stop` (pidfile).
- Access tokens expire after 30 min → `401`. Re-run `run.sh token`.
- If the UI hangs on "Loading data", `get_services` hit a missing HA component
  dependency; the traceback's last `ModuleNotFoundError` names it — add it to
  `DEPS` in `run.sh` and re-bootstrap.
- While `frontend` fails to import, HA ignores `http.server_port` and binds
  8123 instead of 8199 — a sign the bootstrap deps are incomplete.
- `hassil` must stay pinned to `3.5.0`; 3.10 has no `hassil.fuzzy`.
- Playwright MCP only writes inside the repo, e.g. `.playwright-mcp/` — clean
  up your own screenshots afterwards or mention them as evidence, but delete
  them individually: earlier rounds left theirs in the same directory and
  `docs/verification-card-delivery-2026-07-31.md` cites one by filename.
- **An abandoned config flow's `client.close()` is not observable.** The fake
  logs only `CONNECT` and `SEND_TAN`, and on the no-SCA path `init_system_id`
  has already closed the standing dialog in its `finally`, so `close()` just
  drops references — nothing reaches `fakebank.log`, and instrumenting
  `__exit__` or `__del__` would not discriminate either. Drive the flow and
  `DELETE` it to prove the teardown path runs without error; don't expect more.

Findings from prior runs: `docs/verification-2026-07-31.md`,
`docs/verification-2026-08-10.md` and `docs/verification-2026-08-11.md`.
