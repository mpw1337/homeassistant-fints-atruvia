# Verification — card delivery via the integration (2026-07-31)

Runtime verification of the change that ships the Lovelace card inside
`custom_components/fints_atruvia/www/` and registers the Lovelace resource
automatically (`frontend.py`), replacing the manual copy to `/config/www/`.

**Method:** real Home Assistant against the sandbox from `.claude/skills/verify/`
(`harness/run.sh up`), offline fake bank, port 8199. Browser checks via
Playwright as `dev`. No real config, no real bank.

## Results

| Claim | Evidence |
|---|---|
| Card is served from the integration folder | `GET /fints_atruvia/fints-atruvia-card.js` → **HTTP 200**, 11 567 bytes, `content-type: text/javascript` — byte-identical to the rollup output |
| Resource is registered automatically | `lovelace/resources` → exactly one item: `{"url": "/fints_atruvia/fints-atruvia-card.js?v=0.4.0", "type": "module"}` |
| No duplicate on config-entry reload | Reload of `01KSANDBOX000000000000FINT` → still one item, same `id` `17b3ca51…` |
| No duplicate across a full HA restart | After `stop` + `start` → still one item, same `id` |
| Version bump updates in place (cache-busting) | `manifest.json` → `0.4.1`, restart → same `id`, url now `?v=0.4.1`; reverted to `0.4.0` afterwards |
| The browser actually loads it | `performance.getEntriesByType('resource')` contains `http://127.0.0.1:8199/fints_atruvia/fints-atruvia-card.js?v=0.4.0` and nothing under `/local/` |
| Card renders | `customElements.get('fints-atruvia-card')` truthy, 1 card in the DOM, 4 458 chars of shadow DOM, balance `1.234,56 €`, masked IBAN `DE** **** **** **** 3000`, degraded view text shown (`expose_full_data` off) — screenshot `.playwright-mcp/card-served-from-integration.png` (no longer on disk, see note below) |
| No new log noise | Only the standard "custom integration … has not been tested" warning for the domain; no card-registration warning or traceback |

Sensors were unaffected: `sensor.konto_3000` = `1234.56` with the expected
attributes, i.e. the extra `http` / `lovelace` manifest dependencies did not
disturb setup.

## Not covered here

- A *second* config entry (two bank connections) was not created in the
  sandbox; the once-per-run guard is covered by
  `tests/test_frontend.py::test_register_card_is_registered_only_once`, and the
  entry-reload/restart checks above exercise the same early-return paths.
- YAML resource mode is covered by unit test only
  (`test_yaml_resource_mode_writes_nothing`) — the sandbox runs storage mode.

## Note on the screenshot cited above

`.playwright-mcp/card-served-from-integration.png` no longer exists: the
2026-08-11 verification round cleaned up `.playwright-mcp/` wholesale and
removed this round's artefacts along with its own. The directory was and is
gitignored, so the file was never part of the repository. Every measurement in
the table above is textual and unaffected; only the visual confirmation of the
rendered card is gone, and it was re-established from scratch in
[`verification-2026-08-11.md`](verification-2026-08-11.md).

## Note on the dashboard path

HA's default dashboard is `home/overview`; `lovelace/config/save` without a
`url_path` writes a `lovelace` dashboard that is no longer reachable at
`/lovelace/0`. Create an explicit dashboard
(`lovelace/dashboards/create` with `url_path`) and browse that instead — the
instruction in `.claude/skills/verify/SKILL.md` is stale on this point.
