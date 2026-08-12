# Changelog

Alle nennenswerten Änderungen an diesem Projekt werden hier dokumentiert.
Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/);
Einträge sind deutsch, konsistent mit `README.md` / `SECURITY.md`.

## [0.5.0] - 2026-08-11

Bugfix- und Härtungs-Runde auf Basis eines Runtime-Verifikationslaufs gegen
eine synthetische Sandbox-Bank (`docs/verification-2026-07-31.md`) und eines
anschließenden Code-Review-Durchgangs (`docs/verification-2026-08-10.md`).

**Migration:** Config-Entries werden beim nächsten HA-Start automatisch von
v1 über v2 nach v3 migriert — kein manueller Schritt nötig. HA-Backups, die
*vor* der v1→v2-Migration angelegt wurden, enthalten weiterhin den
Klartext-PIN und sollten gelöscht oder neu (mit Backup-Passwort) erzeugt
werden. Siehe `SECURITY.md` §8. Bekannte Einschränkung dieser Migration: Wer
von einer Version mit noch nicht gehashten Entity-`unique_id`s aktualisiert,
löst dabei pro betroffener Entity ein `entity_registry_updated`-Event mit der
Klartext-IBAN im `changes`-Payload aus — das landet über den Recorder auch in
`home-assistant_v2.db`, bis das Standard-Purge-Fenster (`purge_keep_days: 10`)
es entfernt. Nicht durch die Integration unterdrückbar, da HA das Event
selbst feuert. Siehe `SECURITY.md` §5. Downgrade auf v0.4.0: kein
Hard-Failure — die Entry bleibt bei `version = 3` und das Setup läuft durch,
da v0.4.0s `async_migrate_entry` nur `version == 1` behandelt und sonst
`True` zurückgibt. Einziger Effekt: v0.4.0 berechnet `unique_id` wieder als
`"{blz}_{username}"` und kann den HMAC nicht mehr reproduzieren, sodass
`_abort_if_unique_id_configured` die Bank nicht mehr wiedererkennt — ein
erneutes Hinzufügen legt eine zweite Config-Entry mit eigenem
Credential-Blob an, statt die bestehende zu deduplizieren.

### Sicherheit

- Der `unique_id` der Config-Entry enthielt BLZ und Login zunächst im
  Klartext, dann als unsalted Hash — beides landete in
  `.storage/core.config_entries` und damit in jedem unverschlüsselten Backup
  und Diagnose-Download dieser Datei. Ersetzt durch einen mit dem
  Master-Fernet-Key **HMAC-SHA256**-geschlüsselten Hash; Config-Flow ist
  jetzt bei `VERSION = 3`, mit einer v2→v3-Migration, die fail-open auf die
  alte `unique_id` zurückfällt, falls die Anmeldedaten nicht entschlüsselt
  werden können.
- Die Lovelace-Karte interpolierte den konfigurierten `title:` sowie die
  bankseitig gelieferten letzten vier IBAN-Ziffern per `escapeHtml()` in das
  `header="..."`-Attribut der Karte — `escapeHtml()` escaped aber keine
  Anführungszeichen, sodass ein `"` im Titel oder in der IBAN das Attribut
  verlassen und Markup injizieren konnte. Neuer `escapeAttr()`-Helper deckt
  jetzt auch den Attribut-Kontext ab.
- Nach der Unique-ID-Migration blieb die Klartext-IBAN in
  `previous_unique_id` der Entity-Registry stehen. Wird jetzt direkt nach der
  Migration geleert.
- Der Konto-Picker im Config-Flow zeigte die volle Kontonummer im Label
  (`Konto …3000 (0000123456)`) an, die zusammen mit der im selben Flow
  sichtbaren BLZ die volle IBAN rekonstruierbar machte. Label zeigt jetzt nur
  `Konto …{last4}`, mit einer nicht-identifizierenden laufenden Nummer nur
  bei doppelten letzten vier Ziffern.
- An zwei Stellen schrieb die Integration die volle IBAN in Text, der in
  `home-assistant.log` landet — entgegen der Regel, dass IBANs an jeder
  externen Grenze maskiert werden (`SECURITY.md` §5/§10). Erstens die
  Coordinator-Warnung „Account … not found at bank“: die feuert auf
  **WARNING**, also im Standard-Loglevel und ohne jede Fehlerbedingung, sobald
  ein ausgewähltes Konto in der Kontoliste der Bank fehlt (etwa nach einer
  Kontoschließung oder -umnummerierung). Zweitens die beiden `ValueError`s
  beim Abruf des Kontostands, die das Konto per voller IBAN benannten: der
  Coordinator loggt selbst nur den Exception-Typ, HA formatiert die
  `__cause__`-Kette danach aber auf zwei eigenen Loggern auf **DEBUG** —
  genau in dem Log, das man über „Debug-Protokollierung aktivieren“ einsammelt
  und in ein öffentliches Issue kopiert. Beide Stellen maskieren jetzt
  (`DE89**…**3000` bzw. `…3000`). Kein remote auslösbares Problem, sondern
  eine lokale Offenlegung gegenüber jedem, der das Logfile lesen oder
  weitergeben kann; beide Stellen existierten bereits vor v0.4.0. Zur
  Laufzeit gegen ein echtes Home Assistant nachgeprüft, beide Pfade erzwungen
  — siehe `docs/verification-2026-08-11.md`, Abschnitt „Follow-up“.

### Behoben

- `TanRequiredError` während eines laufenden HA-Neustarts löste wegen eines
  ungültigen `raise ... from`-Chainings einen `TypeError` statt
  `ConfigEntryAuthFailed` aus und brach damit den 90-Tage-Reauth-Flow.
- Bereits gesehene Transaktions-Hashes wurden mitten im Poll-Durchlauf pro
  Konto geschrieben; scheiterte ein späteres Konto im selben Durchlauf (z. B.
  mit `TanRequiredError`), gingen `fints_atruvia_new_transaction`-Events für
  bereits verarbeitete Konten dauerhaft verloren. Hashes werden jetzt erst
  nach einem vollständig erfolgreichen Poll-Durchlauf gemerged.
- `async_shutdown` im Coordinator rief `super().async_shutdown()` nicht auf,
  wodurch HAs eigene Abmeldung von geplanten Refreshs nie lief.
- `config_entry` wurde nicht an `DataUpdateCoordinator.__init__` durchgereicht.
  HA markiert den fehlenden Parameter intern mit `breaks_in_ha_version:
  "2026.8"`, plant laut Code-Kommentar aber keine Durchsetzung für
  Custom-Integrations — trotzdem korrigiert, statt sich auf den Fallback zu
  verlassen.
- `FintsStateStore` konnte eine echte v1-Plaintext-State-Datei aus der Zeit
  vor der Fernet-Verschlüsselung nicht mehr laden — HAs Standard-Storage
  bricht die Migration ohne eigenen `_async_migrate_func` mit
  `NotImplementedError` ab. Neuer `_MigratingStore` gibt alte Daten
  unverändert zurück, statt abzubrechen.
- Der `FinTsAtruviaClient` eines abgebrochenen Config-Flows (PIN im
  `pin_provider`) wurde nie geschlossen, wenn der Nutzer den Flow verließ,
  ohne ihn abzuschließen. Neuer `async_remove()`-Teardown-Hook schließt ihn.
- Der Options-Toggle „Verwendungszweck und Empfängername exponieren" wirkte
  erst nach dem nächsten 6-Stunden-Poll oder einem manuellen Entry-Reload. Ein
  Options-Update-Listener lädt die Config-Entry jetzt sofort neu.
- Bei mehr als fünf Buchungen im 30-Tage-Fenster zeigte die Karte die
  ältesten statt der neuesten fünf Transaktionen im Bereich „Letzte
  Transaktionen" an. Wird jetzt vor dem Slicing absteigend nach Datum
  sortiert.
- Die Karte ignorierte die `title:`-Konfigurationsoption und zeigte immer den
  Entity-Friendly-Name als Überschrift.
- Ein expandierter Transaktionen-Bereich in der Karte klappte bei jedem
  Sensor-Update im Haushalt wieder zusammen, weil der `hass`-Setter bei jedem
  Aufruf `shadowRoot.innerHTML` neu schrieb, auch wenn die betroffene Änderung
  gar nicht diese Karte betraf. `_render()` vergleicht die generierte HTML
  jetzt mit der zuletzt geschriebenen und überspringt den Write, wenn sich
  nichts geändert hat.
- Ein nachträglich (über den Reauth-Flow) hinzugefügtes Konto feuerte beim
  ersten Poll seine komplette 30-Tage-Historie als
  `fints_atruvia_new_transaction`-Events, weil das „still übernehmen"-Gate
  nur einmal pro Config-Entry griff statt pro IBAN. Ein erstmals abgefragtes
  Konto wird jetzt genauso still übernommen wie der allererste Abruf nach
  der Einrichtung.
- Bot die Bank kein unterstütztes Zwei-Schritt-TAN-Verfahren an (kein
  Antwortcode 3920, BPD zurückgehalten oder nur nicht unterstützte
  HITANS-Versionen), scheiterte der Config-Flow mit einem rohen `KeyError`
  aus python-fints und zeigte `cannot_connect` — ununterscheidbar von einer
  echten Netzwerkstörung. Neue `NoTanMechanismError` fängt diesen Fall vor
  dem Aufruf ab und zeigt eine eigene Fehlermeldung (`no_tan_mechanism`).

### Hinzugefügt

- README: eigener Abschnitt zum `fints_atruvia_new_transaction`-Event mit dem
  Payload, dem Still-Übernehmen-Verhalten und dem Hinweis, dass
  `transaction_hash` nicht kontenbezogen ist (dieselbe Buchung auf zwei
  Konten trägt denselben Hash) — kontenübergreifende Deduplizierung in
  Automatisierungen sollte `iban_last4`/`integration_id` einbeziehen.
- `translations/en.json` — HA lädt Übersetzungen zur Laufzeit ausschließlich
  aus `translations/<lang>.json` ohne Fallback auf `strings.json`, sodass
  englischsprachige Instanzen bislang rohe Config-/Options-Flow-Keys statt
  Labels angezeigt bekamen.

### Intern

- `ruff format` erstmals über die gesamte Codebase laufen lassen und den
  verbleibenden Lint-Backlog (92 Findings) manuell aufgeräumt; Widersprüche
  in `.ruff.toml` bereinigt (`pep257`-Convention, `extend-exclude` für den
  Verify-Harness und `*.md`, `tests/**`-Ausnahmen für Standard-Pytest-Muster).
- `ruff check .` und `ruff format --check .` sind jetzt tatsächlich in
  `.github/workflows/validate.yml` gegated, Ruff auf `0.16.2` gepinnt.
- CI: `actions/checkout`/`setup-uv` auf ihre Node-24-Majore angehoben,
  `setup-uv` auf `v9.0.0` gepinnt, HACS-Brands-Check übersprungen.
- Regressionstests für alle oben genannten Fixes ergänzt (u. a.
  `tests/test_coordinator.py`, `tests/test_storage.py`, `tests/test_init.py`)
  sowie den Options-Listener und den Flow-Client-Close abgesichert.

## [0.4.0] - 2026-07-30

Erste Veröffentlichungs-Runde: Lovelace-Karte wird mit der Integration
ausgeliefert, Repository für HACS vorbereitet.

- Lovelace-Karte wird jetzt mit der Integration ausgeliefert und beim Setup
  automatisch als Dashboard-Ressource registriert (`frontend.py`), statt
  manuell nach `config/www/` kopiert werden zu müssen.
- Repository für die Veröffentlichung vorbereitet (u. a. `hacs.json`,
  Issue-Tracker- und Dokumentations-Links im Manifest).
- Erster Runtime-Verifikationslauf gegen eine synthetische Sandbox-Bank
  hinzugefügt (`.claude/skills/verify/`); Ergebnisse dokumentiert in
  `docs/verification-2026-07-31.md`.
- Veraltete IBAN-Test-Assertions korrigiert, die noch auf die alten
  Test-Fixture-Daten verwiesen.

## [0.3.0] - 2026-05-15

Dedizierter Härtungs-Pass gegenüber v0.2.0:

- FinTS-State-Cache (`system_id`, BPD, UPD) Fernet-verschlüsselt statt als
  Hex-Plaintext auf der Disk.
- Bank-Texte (Verwendungszweck, Empfängername) verlassen die Integration
  standardmäßig nicht mehr; Opt-in per Options-Flow je Config-Entry.
- Entity-`unique_id` durch einen gesalzenen SHA-256-Hash der IBAN ersetzt
  statt der Klartext-IBAN; bestehende Entries migrieren automatisch beim
  Start, History und Statistik bleiben erhalten.
- XSS-Lücken in der Lovelace-Karte geschlossen — die NaN-Fallback-Sinks
  `balanceFormatted` und `txFormatted` waren zuvor ungeescaped.
- IDN-/Punycode-Phishing-Block für Custom-URLs mit Nicht-ASCII-Hostnamen.
- Log-Hygiene: keine `_LOGGER.exception`-Tracebacks mehr aus FinTS-Pfaden,
  kein `%r`-Repr-Logging auf Transaktions-Objekten.
- v1→v2-Migration idempotent gemacht, inklusive Self-Check, dass
  `password`/`username` nach der Migration tatsächlich aus `entry.data`
  entfernt wurden.
- Regressionstests für IBAN-Maskierung, Unique-ID-Hashing, URL-Validierung,
  Storage-Round-Trip und Event-Payload-Shape ergänzt.
