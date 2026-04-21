# FinTS Atruvia — Home Assistant Integration

Verbindet Home Assistant mit deutschen Spardabanken (Atruvia-Backend) via FinTS/HBCI. Zeigt Kontostände und Transaktionen als Sensoren und bringt eine eigene Lovelace-Karte mit.

**Unterstützte Banken:**
- Spardabank SW
- Spardabank BW

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
   - Bank auswählen (Sparda SW / Sparda BW)
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

## Lizenz

MIT
