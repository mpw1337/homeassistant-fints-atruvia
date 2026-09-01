# Changelog

Alle nennenswerten Änderungen an diesem Projekt werden hier dokumentiert.
Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/);
Einträge sind deutsch, konsistent mit `README.md` / `SECURITY.md`.

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

## [0.6.2] - 2026-09-01

Bugfix-Release für die Ersteinrichtung: Auf einer frischen Installation
konnten keine Konten hinzugefügt werden („Die Bank hat kein unterstütztes
Zwei-Schritt-TAN-Verfahren angeboten“), sofern die Bank für die Anmeldung eine
starke Kundenauthentifizierung (SCA) verlangt — bei Atruvia der Normalfall.

### Behoben

- **Ersteinrichtung mit SCA-Pflicht.** Ohne gespeicherte Kundensystem-ID lehnt
  Atruvia den einschrittigen HKSYN-Sync ab (9075) und nennt per 3920 nur das
  erlaubte Zwei-Schritt-Verfahren — schickt dabei aber weder BPD noch HISYN.
  python-fints konnte daraus kein HKTAN bauen und fand keine System-ID. Die
  Integration holt die BPD jetzt vorab über einen anonymen Dialog (keine
  Zugangsdaten, nur öffentliche Bankparameter), wiederholt den Sync
  zweischrittig, zeigt die SecureGo+-Abfrage und übernimmt die System-ID aus
  der TAN-Bestätigung. SCA-befreite Logins (3076) laufen weiter einschrittig
  durch.
- **FinTS-Zustand aus dem Config-Flow ging verloren.** Nach der Kontoauswahl
  wurde der Client samt System-ID, TAN-Verfahren und BPD/UPD verworfen; der
  Coordinator begann beim ersten Abruf ohne Zustand und lief erneut in den
  Sync. Der Flow speichert den Zustand jetzt im verschlüsselten State-Store,
  der Coordinator startet mit `restored_state`.
- **„Weiter“ vor der App-Bestätigung.** Antwortet die Bank mit 3956 (Freigabe
  noch nicht eingegangen), zeigt der 2FA-Schritt jetzt den Hinweis „Die Bank
  hat die Freigabe noch nicht erhalten …“ und lässt sich wiederholen, statt mit
  „Keine Konten gefunden“ in einer Sackgasse zu enden. Der Re-Auth-Button
  meldet denselben Fall als Toast und bleibt drückbar.

### Intern

- `FinTsAtruviaClient.init_system_id` führt den HKSYN-Sync selbst (statt
  python-fints' `_ensure_system_id`, das den Dialog vor einer möglichen
  TAN-Antwort beendet); `complete_tan` liefert jetzt `NeedTANResponse | None`
  (Folge-Challenge oder fertig). Der `sca_not_required`-Merker und die
  TAN-Medium-Abfrage im Bootstrap sind entfallen — der Sync-Ausgang selbst
  entscheidet.
- Fake-Bank des Verify-Harness modelliert den Atruvia-Sync (Ablehnung,
  Zwei-Schritt-Challenge, System-ID mit der TAN); neues Flag `tanpending`
  für 3956. Unit-Suite: Sync-, Challenge- und Zustandsübergabe-Szenarien.

## [0.6.1] - 2026-08-17

Diagnose-Verbesserung für Setup-Fehler: FinTS-Antwortcodes werden jetzt beim
„kein unterstütztes Zwei-Schritt-TAN-Verfahren"-Fehler mitgeloggt; dazu
Dokumentation zur optionalen Produkt-ID im Einrichtungsdialog und README.

### Geändert

- `NoTanMechanismError` (etwa: „Bank bietet kein Zwei-Schritt-TAN-Verfahren")
  wird jetzt mit aufgezeichneten Response-Codes als Diagnose-Hilfe ausgegeben
  (z. B. `no supported two-step TAN mechanism (codes: 3920)`), statt die
  Codes stillschweigend zu verwerfen. Der Config-Flow erfasst diese Codes
  auch log-hygienisch wie beim Authentifizierungs-Fehler. Hilft bei der
  Unterscheidung zwischen „Bank schickte 3920 gar nicht" vs. „BPD-Parser
  kann keine HITANS-Segmente dekodieren".

### Hinzugefügt

- Optional-Feld-Hinweise im Config-Flow und README zur Produkt-ID: Das
  Feld war schon immer optional, neu ist nur die Dokumentation dazu. Wenn
  leer gelassen, wird eine eingebaute FinTS-Produkt-ID verwendet; das Feld
  bleibt für abweichende Produkt-IDs (z. B. für mehrere Onlinebanking-
  Clients) nutzbar.

### Intern

- `_ExtendedFinTSClient` zeichnet alle Response-Codes (nicht nur 9xxx) während
  des System-ID-Setups auf; `NoTanMechanismError` trägt sie jetzt als
  `.codes` Property.
- Neue Fake-Bank-Flag `nohitans` im Verify-Harness: Der Konten-Automat
  scheitert mit `NoTanMechanismError` und observed_codes = [3920], um
  die neuen Diagnose-Verbesserungen zu testen.
- Testabdeckung: Unit-Suite ergänzt um die `nohitans`-Szenarien.

## [0.6.0] - 2026-08-14

Diagnose- und Robustheits-Runde für den Config-Flow, ausgelöst durch einen
realen Fehlversuch beim Einrichten eines zweiten Kontos: Die Bank lehnte die
Anmeldung ab, die Integration zeigte aber nur „Verbindung fehlgeschlagen“
und der eigentliche FinTS-Antwortcode war nirgends sichtbar.

### Behoben

- Lehnt die Bank die Anmeldung ab (`FinTSClientPINError` /
  `FinTSClientTemporaryAuthError` aus python-fints), zeigt der Config-Flow
  jetzt einen eigenen Fehler „Die Bank hat die Anmeldung abgelehnt …“
  (`invalid_auth`) statt des irreführenden `cannot_connect`, inklusive
  Hinweis auf mögliche Zugangs-Sperrung nach mehreren Fehlversuchen. Gilt
  auch für den 2FA-Schritt und den Reauth-Flow.
- Die nackten FinTS-Antwortcodes (z. B. `9942`) werden dafür jetzt in der
  Client-Erweiterung mitgeschnitten und log-hygienisch ausgegeben
  (`bank rejected authentication (codes: 9942)`) — nur der numerische Code,
  nie Bank-Text. Bisher waren die Codes komplett unsichtbar, weil
  python-fints die still verarbeiteten Bootstrap-Dialoge nicht loggt und der
  Flow nur den Exception-Typ ausgab.
- Bei wiederholten Anläufen im Config-Flow wurde der jeweils vorherige
  FinTS-Client (samt PIN-Referenz) nie geschlossen; er wird jetzt vor jedem
  neuen Versuch aufgeräumt.

### Hinzugefügt

- SCA-befreite Logins werden unterstützt: Meldet die Bank Antwortcode `3076`
  („Starke Kundenauthentifizierung nicht notwendig“) und bietet kein
  Zwei-Schritt-TAN-Verfahren an, läuft die Einrichtung jetzt einschrittig
  und ohne TAN-Abfrage bis zur Kontoauswahl durch, statt mit „kein
  TAN-Verfahren“ abzubrechen.

### Intern

- Neue Exception `AuthRejectedError(codes)` in `api.py` kapselt die
  Auth-Ablehnung samt aufgezeichneter Antwortcodes; `_ExtendedFinTSClient`
  zeichnet 9xxx-Codes und die SCA-Befreiung in `_process_response` auf.
- Die Fake-Bank des Verify-Harness kennt zwei neue Flags: `pinerror`
  (Ablehnung mit 9942 → `invalid_auth`-Pfad) und `nosca` (SCA-befreiter
  Login ohne TAN-Challenge). Beide Szenarien sind end-to-end gegen ein
  Sandbox-HA verifiziert.

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
