"""Serve the bundled Lovelace card and register it as a Lovelace resource.

For integration-category repositories HACS only ships
``custom_components/fints_atruvia/``, so the built card lives inside the
integration folder and is served from there instead of ``/config/www/``.
Home Assistant OS users have no convenient shell to copy the file by hand,
which is why the Lovelace resource is registered automatically rather than
being a documented manual step.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "fints-atruvia-card.js"
CARD_URL_PATH = f"/{DOMAIN}/{CARD_FILENAME}"

# Set once per Home Assistant run. Registering the static path twice would
# raise a duplicate-route error, so this is deliberately never reset — a
# failed registration is recovered by restarting Home Assistant.
_REGISTERED_KEY = f"{DOMAIN}_card_registered"

# Where the card used to live when it had to be copied to /config/www/.
# Such a leftover resource loads a second, stale copy of the card.
_LEGACY_URL_PREFIX = f"/local/{CARD_FILENAME}"


def _card_path() -> str:
    """Return the on-disk path of the built card bundle."""
    return str(Path(__file__).parent / "www" / CARD_FILENAME)


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card and make sure Lovelace knows about it.

    Safe to call from every config entry setup: only the first call does any
    work. Never raises — a missing card must not stop the integration from
    loading its sensors.
    """
    if hass.data.get(_REGISTERED_KEY):
        return
    hass.data[_REGISTERED_KEY] = True

    try:
        integration = await async_get_integration(hass, DOMAIN)
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, _card_path(), cache_headers=True)]
        )
        await _async_register_resource(hass, str(integration.version))
    except Exception as exc:  # noqa: BLE001 - card issues must not break setup
        _LOGGER.warning(
            "Lovelace-Karte konnte nicht registriert werden (%s). "
            "Die Integration funktioniert weiter, die Karte muss ggf. manuell "
            "als Ressource eingetragen werden: %s",
            type(exc).__name__,
            CARD_URL_PATH,
        )


async def _async_register_resource(hass: HomeAssistant, version: str) -> None:
    """Create or update the Lovelace resource pointing at the card.

    The version is carried in the query string so browsers pick up a new
    bundle after an update instead of serving the cached one.
    """
    url = f"{CARD_URL_PATH}?v={version}"
    lovelace_data = hass.data.get(LOVELACE_DATA)
    resources = getattr(lovelace_data, "resources", None)

    if not isinstance(resources, ResourceStorageCollection):
        # YAML resource mode (or Lovelace not loaded): the collection is
        # read-only, the user has to add the resource themselves.
        _LOGGER.warning(
            "Lovelace läuft im YAML-Ressourcen-Modus. Bitte ergänze in der "
            "configuration.yaml unter lovelace/resources: "
            "{url: %s, type: module}",
            url,
        )
        return

    # Loads the store on first access; async_items() is empty until it ran.
    await resources.async_get_info()

    for item in resources.async_items():
        item_url = str(item.get("url", ""))
        if item_url.startswith(_LEGACY_URL_PREFIX):
            _LOGGER.warning(
                "Die alte Lovelace-Ressource %s lädt eine zweite Kopie der "
                "Karte. Bitte unter Einstellungen → Dashboards → Ressourcen "
                "entfernen.",
                item_url,
            )
            continue
        if not item_url.startswith(CARD_URL_PATH):
            continue
        if item_url == url:
            return
        await resources.async_update_item(
            item["id"], {"res_type": "module", "url": url}
        )
        return

    await resources.async_create_item({"res_type": "module", "url": url})
