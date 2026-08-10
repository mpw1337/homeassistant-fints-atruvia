"""Create a synthetic HA config sandbox for fints_atruvia verification.

Nothing is copied from the developer's own ``config/`` — the real HA store
holds live bank credentials and must stay where it is. Instead this writes a
pre-migration state from scratch:

  * a fints_atruvia config entry at version 1 with plaintext username/password
  * entity-registry rows carrying legacy ``{entry_id}_{IBAN}`` unique_ids

so a single HA start exercises async_migrate_entry + _async_migrate_unique_ids.

Usage: SB=/path/to/sandbox python make_sandbox.py [--fresh|--keep-storage]
       --fresh (default) wipes the sandbox: entry back to v1, no seen-txn set
       --keep-storage keeps .storage (already-migrated entry, seeded seen-set)
"""
import json
import os
import pathlib
import shutil
import sys

SB = pathlib.Path(os.environ["SB"])
REPO = pathlib.Path(__file__).resolve().parents[4]

ENTRY = "01KSANDBOX000000000000FINT"
IBAN = "DE89370400440532013000"
NOW = "2026-01-01T10:00:00.000000+00:00"
PORT = os.environ.get("SB_PORT", "8199")

keep = "--keep-storage" in sys.argv
if SB.exists() and not keep:
    shutil.rmtree(SB)
(SB / ".storage").mkdir(parents=True, exist_ok=True)
(SB / "themes").mkdir(exist_ok=True)
(SB / "www").mkdir(exist_ok=True)

(SB / "configuration.yaml").write_text(f"""
homeassistant:
  auth_providers:
    - type: homeassistant
    - type: trusted_networks
      trusted_networks:
        - 127.0.0.1/32
        - ::1/128
      allow_bypass_login: true

http:
  server_port: {PORT}

config:
history:
logbook:
person:
sun:
zone:
network:
webhook:
system_health:
frontend:
  themes: !include_dir_merge_named themes

logger:
  default: info
  logs:
    custom_components.fints_atruvia: debug
    homeassistant.config_entries: debug

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml
""")
(SB / "automations.yaml").write_text("[]\n")
for f in ("scripts.yaml", "scenes.yaml"):
    (SB / f).write_text("")

if keep:
    print(f"sandbox kept: {SB}")
    raise SystemExit(0)

entries = {
    "version": 1,
    "minor_version": 5,
    "key": "core.config_entries",
    "data": {"entries": [{
        "created_at": NOW,
        "data": {
            "blz": "99999999",
            "url": "https://fints.sandbox.invalid/fints30",
            "product_id": "SANDBOXPRODUCT0000000001",
            "username": "sandboxuser",
            "password": "SandboxPIN12345",
            "selected_accounts": [IBAN],
        },
        "disabled_by": None,
        "discovery_keys": {},
        "domain": "fints_atruvia",
        "entry_id": ENTRY,
        "minor_version": 1,
        "modified_at": NOW,
        "options": {},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "user",
        "subentries": [],
        "title": "Sandbox Bank",
        "unique_id": "99999999_sandboxuser",
        "version": 1,
    }]},
}
(SB / ".storage" / "core.config_entries").write_text(json.dumps(entries, indent=1))


def ent(entity_id, unique_id, name):
    return {
        "aliases": [], "area_id": None, "categories": {}, "capabilities": None,
        "config_entry_id": ENTRY, "config_subentry_id": None, "created_at": NOW,
        "device_class": None, "device_id": None, "disabled_by": None,
        "entity_category": None, "entity_id": entity_id, "hidden_by": None,
        "icon": None, "id": unique_id.replace("_", "")[:32].ljust(32, "0"),
        "has_entity_name": False, "labels": [], "modified_at": NOW, "name": None,
        "object_id_base": None, "options": {}, "original_device_class": None,
        "original_icon": None, "original_name": name, "platform": "fints_atruvia",
        "suggested_object_id": None, "supported_features": 0,
        "translation_key": None, "unique_id": unique_id,
        "previous_unique_id": None, "unit_of_measurement": None,
    }


registry = {
    "version": 1,
    "minor_version": 20,
    "key": "core.entity_registry",
    "data": {
        "entities": [
            ent("sensor.konto_3000", f"{ENTRY}_{IBAN}", "Konto 3000"),
            ent("sensor.konto_3000_einnahmen_30t", f"{ENTRY}_{IBAN}_income_30d",
                "Konto 3000 Einnahmen 30T"),
            ent("sensor.konto_3000_ausgaben_30t", f"{ENTRY}_{IBAN}_expense_30d",
                "Konto 3000 Ausgaben 30T"),
            ent("button.re_authentifizierung_bestatigen", f"{ENTRY}_reauth_button",
                "Re-Authentifizierung bestätigen"),
        ],
        "deleted_entities": [],
    },
}
(SB / ".storage" / "core.entity_registry").write_text(json.dumps(registry, indent=1))

print(f"sandbox: {SB}")
print(
    f"entry {ENTRY}: version 1, plaintext creds, "
    "legacy unique_id 99999999_sandboxuser, 4 legacy entity unique_ids"
)
