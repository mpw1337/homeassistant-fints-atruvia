# Security

Sicherheitsmodell und Härtungen der FinTS-Atruvia-Integration.

## Neu in v0.3.0

Diese Release ist ein dedizierter Härtungs-Pass. Wer von v0.2.0 kommt, profitiert ohne Konfigurationsänderung von:

- **State-Cache verschlüsselt** (§4) — `system_id`, BPD, UPD waren bislang als Hex-Plaintext auf der Disk; jetzt Fernet.
- **Bank-Texte standardmäßig privat** (§6) — `purpose` und `applicant_name` verlassen die Integration nicht mehr per Default. Wer sie braucht, aktiviert sie per Options-Flow je Entry.
- **IBAN auch aus der Entity-Registry raus** (§5) — `unique_id` ist jetzt ein gesalzener SHA-256-Hash. Bestehende Entries werden beim Start automatisch migriert; History/Statistik bleiben erhalten.
- **XSS-Lücken in der Lovelace-Karte geschlossen** (§9) — die NaN-Fallback-Sinks `balanceFormatted` und `txFormatted` waren in v0.2.0 noch ungeescaped.
- **IDN-Phishing-Block** (§2) — Custom-URLs mit Nicht-ASCII-Hostname werden abgelehnt.
- **Log-Hygiene** (§10) — keine `_LOGGER.exception`-Tracebacks mehr aus FinTS-Pfaden; keine `%r`-Repr-Logs auf Transaktions-Objekten.
- **Migration idempotent + Self-Check** (§8) — der v1→v2-Upgrade lässt keine Klartext-Reste in `core.config_entries` zurück, auch nicht nach einem Crash mid-migration.
- **Regression-Tests** für IBAN-Maskierung, Unique-ID-Hashing, URL-Validierung, Storage-Round-Trip und Event-Payload-Shape — siehe `tests/`.

## Bedrohungsmodell

Diese Integration verarbeitet Banking-Zugangsdaten (NetKey + PIN) für FinTS-PSD2-Konten. Die wesentlichen Angriffsvektoren:

| Vektor | Schutz |
|---|---|
| Partieller Datenleak (eine Datei kopiert/synced) | Fernet-Verschlüsselung für Credentials **und** FinTS-State; Zwei-Datei-Trennung |
| Unverschlüsselte Übertragung der PIN | HTTPS-Erzwingung, IDN-/Punycode-Block |
| Logging von Klartext-Credentials | PIN nie in Konstruktor-Args, keine `_LOGGER.exception`-Tracebacks |
| Exposition gegenüber HA-Mitnutzern / Automatisierungen | IBAN-Maskierung in Attributen, Events, Entity-Registry; Verwendungszweck/Empfänger nur per Opt-In |
| XSS aus Bank-Daten (Verwendungszweck als Angriffsvektor) | `escapeHtml` an allen DOM-Sinks in der Lovelace-Karte |
| Kompromittierte oder geänderte PIN | Reauth-Flow ohne Datenverlust |
| Voll-Disk-Diebstahl | **Nicht abgedeckt** — Disk-Verschlüsselung beim Nutzer |

## Maßnahmen

### 1. Verschlüsselte Credential-Speicherung

NetKey und PIN werden mit **Fernet (AES-128-CBC + HMAC-SHA256)** verschlüsselt:

- `.storage/fints_atruvia_master_key` — Fernet-Schlüssel (Mode 0600, eine Datei pro HA-Instanz).
- `.storage/fints_atruvia_credentials_<id>` — verschlüsselter Blob mit NetKey + PIN (Mode 0600, eine Datei pro Config-Entry).

`core.config_entries` enthält nur die `credential_id`-Referenz, keine Klartext-PIN. Wer eine der beiden Dateien einzeln exfiltriert, kann den PIN nicht rekonstruieren.

### 2. HTTPS-Erzwingung und IDN-Block

`FinTsAtruviaClient` lehnt Nicht-HTTPS-URLs im Konstruktor ab. Der Config-Flow validiert Custom-URLs vor dem Speichern und blockt zusätzlich Nicht-ASCII-Hostnamen (Schutz gegen Homoglyph-/Punycode-Phishing wie `атруvia.de`). Downgrade-Angriffe über `http://`-URLs sind so blockiert.

### 3. In-Memory-Härtung

- PIN wird via `pin_provider`-Callback statt Konstruktor-Argument übergeben → erscheint nicht in Python-Tracebacks.
- PIN lebt nur im Coordinator-Objekt, nicht im FinTS-Client-Wrapper. Im Config-Flow wird `self._credentials` direkt nach `async_create_entry` geleert.
- Auf `async_unload_entry` wird `_pin = None` gesetzt und der FinTS-Client geschlossen.
- *Verbleibendes Risiko:* `python-fints` speichert die PIN intern bis zum Client-Neuaufbau — nicht von außen steuerbar.

### 4. FinTS-State-Caching (verschlüsselt)

Per `deconstruct(including_private=True)` werden `system_id`, BPD und UPD nach jedem erfolgreichen Update **Fernet-verschlüsselt** auf das Filesystem (Mode 0600) persistiert. Per python-fints-Vertrag enthält der Blob **keinen** PIN, aber Kontonummern und Bank-Parameter — daher dieselbe Schlüsselbasis wie für Credentials. Vorteil: Bank löst nicht bei jedem Poll-Zyklus SCA aus → weniger TAN-Fatigue, weniger User-Klicks, kleineres Phishing-Fenster.

Alte (v1) Plaintext-Hex-Blobs werden einmalig beim Laden gelesen und beim nächsten Save automatisch verschlüsselt überschrieben — möglich macht das erst `_MigratingStore` in `storage.py`, da HAs Standard-`Store._async_migrate_func` bei einem Major-Version-Sprung sonst mit `NotImplementedError` abbricht und das Setup für alle mit einer echten v1-Datei blockiert hätte.

### 5. IBAN-Maskierung — flächendeckend

- **Sensor-Attribute (`iban`):** `DE51 **** **** **** 3922` statt voller IBAN.
- **`fints_atruvia_new_transaction` Events:** Nur `iban_masked` und `iban_last4`, keine volle IBAN.
- **Entity-Registry (`unique_id`):** SHA-256-Hash der IBAN (mit `entry_id` als Salt), 16 Hex-Zeichen. Die volle IBAN steht damit weder in `home-assistant_v2.db` noch im Event-Bus noch im aktiven `unique_id`-Feld von `core.entity_registry`. *Restrisiko:* HAs `er.async_migrate_entries` schreibt den Wert vor der Migration zusätzlich nach `previous_unique_id` — `_async_clear_previous_unique_ids` in `__init__.py` räumt dieses Feld direkt danach leer, aber HA-Backups, die vor diesem Fix angelegt wurden, können die Klartext-IBAN dort noch enthalten. `core.entity_registry` liegt außerdem mit Mode 0644 vor, anders als die eigenen Stores der Integration (0600).
- **Config-Flow Account-Picker:** Label zeigt nur `Konto …3922`, mit einer nicht-identifizierenden laufenden Nummer (` (2)`) nur dann, wenn zwei angebotene Konten dieselben letzten vier Ziffern teilen. Die Kontonummer wurde bewusst aus dem Label entfernt, da sie zusammen mit der BLZ (die aus demselben Flow in `entry.data` liegt) die volle IBAN rekonstruierbar gemacht hätte. Siehe `_account_labels()` in `config_flow.py`.

### 6. Verwendungszweck und Empfänger — Opt-In statt Default

Standardmäßig werden Bank-kontrollierte Textfelder (`purpose`, `applicant_name` / `creditor`) **nicht** im Event-Bus und **nicht** in den Sensor-`extra_state_attributes` exponiert. Das verhindert, dass sensitive Buchungstexte (medizinische Empfänger, politische Spenden, etc.) in der HA-Historie landen oder über `/api/states/*` für alle HA-User mit Lesezugriff sichtbar sind.

Aktivieren in HA: **Einstellungen → Geräte & Dienste → FinTS Atruvia → Konfigurieren → „Verwendungszweck und Empfängername exponieren"**. Die Option ist pro Config-Entry (also pro Bankkonto-Zugang) wählbar, ein Toggle nur für die Karte ohne Event-Bus-Exposition existiert bewusst nicht — beide Kanäle nutzen denselben Schalter, damit kein versehentliches asymmetrisches Setup entsteht.

Die Lovelace-Karte erkennt automatisch, ob das Attribut `transactions` verfügbar ist, und zeigt bei Opt-out den Hinweis *„Transaktionsdetails deaktiviert (in Integrations-Optionen aktivierbar)"*.

### 7. Reauth-Flow

Bei `ConfigEntryAuthFailed` (Decrypt-Fehler, Bank lehnt PIN ab, abgelaufene 90-Tage-SCA) öffnet HA automatisch einen Reauth-Dialog. Die Config-Entry, ausgewählte Konten und gesehene Transaktions-Hashes bleiben erhalten — keine Notwendigkeit, die Integration zu löschen. Die `credential_id` bleibt im Reauth stabil, sodass der vorhandene verschlüsselte Blob überschrieben statt verwaist wird und der gecachte FinTS-State erhalten bleibt.

### 8. Migration v1 → v2 → v3 (idempotent)

Bestehende Configs mit Klartext-PIN in `core.config_entries` werden beim nächsten HA-Start automatisch in den verschlüsselten Store überführt. Die alten Klartext-Felder werden via `async_update_entry` aus dem Config-Entry entfernt. Die Migration:

- prüft, ob bereits eine `credential_id` vorhanden ist (idempotent bei Retry nach Crash),
- verifiziert nach `async_update_entry`, dass `password`/`username` wirklich weg sind, und scheitert ansonsten lautstark.

Eine v1-Entry durchläuft beide Schritte in einem Aufruf und landet direkt bei v3.

**Wichtig:** HA-Backups, die *vor* der Migration angelegt wurden, enthalten weiterhin den Klartext-PIN. Diese sollten gelöscht oder neu mit Passwort-Schutz erzeugt werden.

Der Config-Flow ist inzwischen bei `VERSION = 3`: die `unique_id` der Config-Entry war früher der Klartext `"{blz}_{username}"`, der damit im Config-Entry-Datensatz (`.storage/core.config_entries`) landete und über die Config-Entries-WebSocket-API ausgeliefert wurde. Der v2→v3-Schritt ersetzt ihn durch `_entry_unique_id(key, blz, username)` — einen **HMAC-SHA256 mit dem Master-Fernet-Key** als Schlüssel, auf 16 Hex-Zeichen gekürzt. Als Nachricht dient `entry_unique_id|{blz}|{username}`; das Präfix ist Domain-Separation, damit diese Verwendung des Master-Keys sich nicht mit späteren keyed Verwendungen ins Gehege kommt.

Ein *ungeschlüsselter* Hash würde hier nicht genügen: die BLZ steht im selben `core.config_entries` im Klartext, und NetKey-Logins sind kurze numerische Kundennummern. Wer die Datei lesen kann, könnte einen unsalted SHA-256 über BLZ+Login also offline in Minuten durchprobieren und das Login rekonstruieren. Mit HMAC scheidet dieser Offline-Brute-Force aus, weil der Schlüssel in `.storage/fints_atruvia_master_key` (Mode 0600) liegt und weder in `entry.data` noch über die WebSocket-API ausgeliefert wird. Für die Deduplizierung reicht das: `_abort_if_unique_id_configured` muss dasselbe BLZ/Login-Paar nur innerhalb *einer* HA-Installation wiedererkennen, und dort ist der Master-Key persistent.

Die Kehrseite: geht der Master-Key verloren, ist der Wert nicht reproduzierbar. Das deckt die bestehende Fail-open-Migration bereits ab — lässt sich der verschlüsselte Credential-Blob nicht entschlüsseln (z. B. eben wegen verlorenem Master-Key), behält die Migration die alte Klartext-`unique_id` bei und hebt nur die Versionsnummer an, statt das Setup zu blockieren. Das ist keine neue Exposition, da der Wert vorher ohnehin schon unverschlüsselt in `core.config_entries` lag.

### 9. XSS-Schutz in der Lovelace-Karte

Alle dynamischen Strings (Verwendungszwecke, Empfängernamen, IBANs, Beträge, Datumsangaben, Entity-IDs) werden vor dem Einfügen ins Shadow DOM escaped, aber nicht mit derselben Funktion: `escapeHtml()` (textContent→innerHTML-Pattern) für Element-Inhalte, `escapeAttr()` für Werte in gequoteten Attributen. Der Grund für die Trennung ist, dass `escapeHtml()` ein reiner Text-Kontext-Escaper ist — er weist den String `textContent` zu und liest `innerHTML` zurück, wodurch der HTML-Serializer `"` und `'` unangetastet lässt. Für die eine Stelle mit Attribut-Kontext-Interpolation (`<ha-card header="...">`, die sowohl den per `title:`-Option konfigurierten Titel als auch die bank-abgeleiteten letzten vier IBAN-Ziffern trägt) escaped `escapeAttr()` zusätzlich Anführungszeichen. Konkret werden auch die NaN-Fallbacks (`stateObj.state` bei nicht-numerischem Balance, `tx.amount` bei nicht-parsbarer Transaktion) escaped — das war in v0.2.0 noch unbehandelt.

Source-Maps werden im Production-Build nicht erzeugt (`rollup.config.js: sourcemap: false`), sodass der Browser-Inspector keine internen Code-Pfade preisgibt. Wer aus einer früheren Version eine `fints-atruvia-card.js.map` in `config/www/` liegen hat, sollte sie löschen.

### 10. Log-Hygiene

`_LOGGER.exception(...)` wurde im Config-Flow, Coordinator und Button-Handler durch `_LOGGER.error("...: %s", type(exc).__name__)` ersetzt. Tracebacks aus `python-fints` können HBCI-Segment-Inhalte (Konto-IDs, Server-Antworten) mit sich tragen — die landen jetzt nicht mehr im HA-Log und damit nicht in Add-On-Forwardern oder Bug-Report-Uploads. Der ursprüngliche Exception-Chain bleibt für `_LOGGER.debug` erhalten.

Zusätzlich wird das `%r`-Logging des Transaktions-Objekts in `api.py` nicht mehr verwendet, da `repr()` je nach python-fints-Version Verwendungszweck oder Empfänger enthalten kann.

### 11. Regression-Tests

Security-relevante Logik ist durch Unit-Tests abgesichert (`tests/`):

- `test_pure.py` — IBAN-Maskierung, Unique-ID-Hashing (Stabilität, IBAN-Recovery unmöglich, Cross-Entry-Trennung), HTTPS-/IDN-Validierung, Credential-Redaction, Transaktions-Hash-Determinismus.
- `test_storage.py` — Fernet-Round-Trip für Credentials und State, Fail-Closed bei korruptem Ciphertext, Lese-Kompatibilität mit v1-Hex-Blobs, Master-Key-Persistenz.
- `test_event_payload.py` — Event-Schema bei Opt-out und Opt-in: keine IBAN-Leakage, kein `purpose`/`applicant_name` ohne explizite Aktivierung.

Tests laufen im DevContainer via `pytest tests/` mit `pytest-homeassistant-custom-component`.

## Grenzen

Diese Maßnahmen schützen **nicht** gegen:

- **Voll-Disk-Theft** — Master-Key liegt auf demselben Datenträger wie die verschlüsselten Credentials. Mitigation: LUKS/BitLocker/FileVault auf dem HA-Host.
- **Unverschlüsselte Backups** — HA-Backups enthalten `.storage/*` im Klartext, sofern nicht passwortgeschützt. Mitigation: Backup-Passwort setzen.
- **SSD-Wear-Leveling-Reste** — alte Klartext-Blöcke aus v1 können physisch auf der SSD verbleiben. Nicht aus User-Space lösbar. Mitigation: PIN bei der Bank ändern, falls Disk je außer Kontrolle war.
- **Privilegierten Zugriff auf den HA-Prozess** — wer als HA-User Code auf der Maschine ausführen kann, kann jeden im Memory liegenden PIN auslesen (Memory-Dump, Debugger).
- **Kompromittierte Add-Ons** — Add-Ons mit Zugriff auf `/config` können Master-Key und Credentials lesen. Mitigation: nur vertrauenswürdige Add-Ons installieren.

## Empfehlungen für Betreiber

- HA-Host-Disk verschlüsseln (LUKS/BitLocker/FileVault).
- HA-Backups mit Passwort schützen (HA `backup`-Komponente).
- HA hinter starkem Passwort + 2FA betreiben (`auth_provider.homeassistant`).
- `recorder.exclude` für Banking-Sensoren setzen, wenn lange Historie nicht benötigt wird. Bei aktiviertem **„Verwendungszweck/Empfänger exponieren"** ist das nahezu Pflicht.
- Logger-Level `info` oder höher für `custom_components.fints_atruvia` — `debug` kann Verwendungszwecke und Empfängernamen mitloggen.
- Nach Migration alte unverschlüsselte HA-Backups löschen oder neu verschlüsseln.

## Verifikation nach Update

Schnellprüfungen für den Operator nach einem Update:

```bash
# Im HA-Container/-Host:
stat -c '%a %n' .storage/fints_atruvia_*
# → erwartet: 600 für alle Dateien

# State-Blob muss ciphertext-Layout haben (gAAAAA…), nicht raw hex:
python -c "import json; print(json.load(open('.storage/fints_atruvia_state_<id>'))['data'].keys())"
# → erwartet: dict_keys(['ciphertext'])

# Entity-Registry darf keine vollen IBANs enthalten:
python -c "import json,re; d=json.load(open('.storage/core.entity_registry'));
print([e['unique_id'] for e in d['data']['entities'] if e['platform']=='fints_atruvia' and re.search(r'DE\d{20}', e['unique_id'])])"
# → erwartet: []
```

## Vulnerability Reporting

Sicherheitsrelevante Funde bitte **nicht** über öffentliche GitHub-Issues melden, sondern über ein privates Security-Advisory im Repository.
