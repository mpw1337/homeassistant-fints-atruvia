# FinTS Atruvia — Home Assistant Integration

Verbindet Home Assistant mit deutschen Spardabanken (Atruvia-Backend) via FinTS/HBCI. Zeigt Kontostände und Transaktionen als Sensoren und bringt eine eigene Lovelace-Karte mit.

**Unterstützte Banken (via Atruvia):**
- Sparda-Bank Südwest
- Sparda-Bank Baden-Württemberg

---

## Installation via HACS

1. HACS in Home Assistant installieren (falls noch nicht vorhanden): [hacs.xyz](https://hacs.xyz)
2. HACS → **Integrationen** → Drei-Punkte-Menü → **Benutzerdefinierte Repositories**
3. URL dieses Repositories eingeben, Kategorie: **Integration**
4. Integration **FinTS Atruvia** in HACS suchen und installieren
5. Home Assistant neu starten

---

## Manuelle Installation

```bash
cp -r custom_components/fints_atruvia /config/custom_components/
```

Home Assistant neu starten.

---

## Einrichtung

1. **Einstellungen** → **Integrationen** → **Integration hinzufügen** → "FinTS Atruvia" suchen
2. Zugangsdaten eingeben:
   - Bankleitzahl (BLZ)
   - NetKey / Benutzername
   - PIN
   - Bank auswählen: "Sparda-Bank (Atruvia)" für Sparda SW oder BW
3. Falls die Bank eine Bestätigung anfordert: **SecureGo+ App öffnen**, Anmeldung bestätigen, dann auf **Weiter** klicken
4. Konten auswählen, die überwacht werden sollen

---

## Lovelace-Karte einrichten

Die Karte muss nach der Integration-Installation als Lovelace-Ressource registriert werden:

1. **Einstellungen** → **Dashboards** → Drei-Punkte-Menü → **Ressourcen**
2. Ressource hinzufügen:
   - URL: `/local/fints-atruvia-card.js`
   - Typ: **JavaScript-Modul**
3. Die Datei `config/www/fints-atruvia-card.js` in das Verzeichnis `/config/www/` der HA-Instanz kopieren

Karte im Dashboard hinzufügen (YAML):
```yaml
type: custom:fints-atruvia-card
entity: sensor.konto_1234
```

---

## Entities

Pro konfiguriertem Konto werden folgende Entities erstellt:

| Entity | Typ | Beschreibung |
|--------|-----|--------------|
| `sensor.konto_XXXX` | Sensor | Aktueller Kontostand (EUR) |
| `button.re_authentifizierung_bestatigen` | Button | Wird nach ~90 Tagen aktiv |

**Sensor-Attribute:**
- `iban` — vollständige IBAN
- `transactions` — letzte 10 Transaktionen (Datum, Betrag, Verwendungszweck)
- `2fa_pending` — `true` wenn Re-Authentifizierung erforderlich

---

## 90-Tage Re-Authentifizierung

Atruvia-Banken erfordern alle ~90 Tage eine erneute Bestätigung via SecureGo+:

1. Home Assistant sendet eine **persistente Benachrichtigung**
2. **SecureGo+ App** öffnen und die Anfrage bestätigen
3. In HA den Button **"Re-Authentifizierung bestätigen"** drücken

---

## Entwicklungsumgebung

Voraussetzungen: VS Code, Docker Desktop mit WSL2-Backend

```bash
git clone <dieses-repo>
code ha-development/
```

In VS Code: **"Reopen in Container"** → Home Assistant startet auf [http://localhost:8123](http://localhost:8123)

**Lovelace-Karte bauen:**
```bash
cd frontend
npm install
npm run build   # einmalig
npm run watch   # bei Entwicklung
```

---

## Sicherheit

### Speicherung der Zugangsdaten

NetKey und PIN werden mit **Fernet (AES-128-CBC + HMAC-SHA256)** verschlüsselt
und in einer separaten Datei abgelegt:

- `.storage/fints_atruvia_master_key` — Master-Key (Mode 0600).
- `.storage/fints_atruvia_credentials_<id>` — verschlüsselter Blob mit
  NetKey + PIN (Mode 0600).

Die `core.config_entries`-Datei enthält **nur** Bankleitzahl, URL und eine
Referenz auf den Credential-Blob — **keine** Klartext-PIN. Damit ist der
häufigste Leak-Vektor (versehentlich kopierte / synchronisierte Einzeldatei)
abgesichert.

**Wichtig:** Da Master-Key und Credential-Blob auf demselben Datenträger
liegen, schützt diese Maßnahme nicht gegen Diebstahl der gesamten HA-Disk.
Dafür ist Voll-Disk-Verschlüsselung (LUKS / BitLocker / FileVault) erforderlich.
Backups deiner HA-Instanz sollten ebenfalls verschlüsselt sein.

### Migration v1 → v2

Bestehende Konfigurationen mit Klartext-Zugangsdaten in `core.config_entries`
werden beim nächsten Start automatisch migriert. Die alten Klartext-Felder
werden aus dem Config-Entry entfernt und in den verschlüsselten Store
verschoben. **Nach der Migration kann es sinnvoll sein, frühere unverschlüsselte
Backups zu löschen.**

### HTTPS-Erzwingung

Custom-FinTS-URLs müssen mit `https://` beginnen. Plain `http://` würde die
PIN unverschlüsselt über das Netz schicken und wird abgelehnt.

### IBAN-Maskierung

Sensor-Attribute und `fints_atruvia_new_transaction`-Events enthalten die IBAN
**maskiert** (`DE51 **** **** **** 3922`) bzw. nur die letzten 4 Stellen.
Dies hält die volle IBAN aus dem HA-State-Recorder (`home-assistant_v2.db`),
dem Event-Bus und der UI-History fern.

### Re-Authentifizierung

Wenn die PIN nicht mehr entschlüsselt werden kann oder die Bank die Anmeldung
verweigert, öffnet HA automatisch einen Reauth-Dialog. Die Integration muss
**nicht** gelöscht und neu angelegt werden — die selektierten Konten und die
gesehenen Transaktions-Hashes bleiben erhalten.

### Empfehlungen

- HA hinter starkem Passwort / 2FA betreiben (`auth_provider.homeassistant`).
- HA-Disk verschlüsseln (LUKS, BitLocker, FileVault).
- HA-Backups verschlüsseln (`backup`-Komponente unterstützt Passwort-Schutz).
- Logger nicht auf `debug` für die Domain `fints_atruvia` setzen, falls Logs
  geteilt werden — Verwendungszwecke / Empfängernamen würden dort sichtbar.
- Den Banking-Sensor optional aus dem Recorder ausschließen:

  ```yaml
  recorder:
    exclude:
      entities:
        - sensor.konto_3922
  ```

## Lizenz

MIT
