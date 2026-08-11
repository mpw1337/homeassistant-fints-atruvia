"""Regression tests for the bundled-card / Lovelace-resource registration.

The card is shipped inside the integration folder because HACS only delivers
``custom_components/fints_atruvia/`` — see ``frontend.py``. These tests pin the
behaviour that must not regress: exactly one resource, version-stamped, never
duplicated, and never a hard failure when Lovelace runs in YAML mode.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.helpers import collection

from custom_components.fints_atruvia.frontend import (
    CARD_FILENAME,
    CARD_URL_PATH,
    _async_register_resource,
    _card_path,
    async_register_card,
)

if TYPE_CHECKING:
    import pytest

_VERSION = "0.4.0"
_EXPECTED_URL = f"{CARD_URL_PATH}?v={_VERSION}"


def _storage_collection() -> ResourceStorageCollection:
    """Build a ResourceStorageCollection without touching disk.

    ``loaded = True`` short-circuits ``async_get_info()``, the MagicMock store
    swallows the delayed save that ``async_create_item`` schedules.
    """
    coll = ResourceStorageCollection.__new__(ResourceStorageCollection)
    collection.ObservableCollection.__init__(coll, None)
    coll.store = MagicMock()
    coll.ll_config = MagicMock()
    coll.loaded = True
    return coll


def _seed(coll: ResourceStorageCollection, item_id: str, url: str) -> None:
    """Insert a pre-existing resource the way the store would have loaded it."""
    coll.data[item_id] = {"id": item_id, "type": "module", "url": url}


def _hass_data(resources: object) -> dict:
    """Return a hass.data stand-in carrying the Lovelace collection."""
    lovelace = SimpleNamespace(resources=resources, resource_mode="storage")
    return {LOVELACE_DATA: lovelace}


async def test_creates_resource_when_missing() -> None:
    """A fresh install gets exactly one module resource, version-stamped."""
    coll = _storage_collection()

    await _async_register_resource(MagicMock(data=_hass_data(coll)), _VERSION)

    items = coll.async_items()
    assert len(items) == 1
    assert items[0]["url"] == _EXPECTED_URL
    assert items[0]["type"] == "module"


async def test_second_call_does_not_duplicate() -> None:
    """Setting up a second bank entry must not add the resource twice."""
    coll = _storage_collection()
    hass = MagicMock(data=_hass_data(coll))

    await _async_register_resource(hass, _VERSION)
    await _async_register_resource(hass, _VERSION)

    assert len(coll.async_items()) == 1


async def test_outdated_version_is_updated_in_place() -> None:
    """An update bumps the cache-busting query string instead of duplicating."""
    coll = _storage_collection()
    _seed(coll, "abc", f"{CARD_URL_PATH}?v=0.1.0")

    await _async_register_resource(MagicMock(data=_hass_data(coll)), _VERSION)

    items = coll.async_items()
    assert len(items) == 1
    assert items[0]["id"] == "abc"
    assert items[0]["url"] == _EXPECTED_URL


async def test_yaml_resource_mode_writes_nothing() -> None:
    """YAML mode is read-only: warn, don't raise, don't touch the collection."""
    yaml_coll = ResourceYAMLCollection([])

    await _async_register_resource(MagicMock(data=_hass_data(yaml_coll)), _VERSION)

    assert yaml_coll.async_items() == []


async def test_missing_lovelace_data_writes_nothing() -> None:
    """Lovelace not loaded at all must not blow up the integration setup."""
    await _async_register_resource(MagicMock(data={}), _VERSION)


async def test_legacy_local_resource_is_kept_but_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A leftover /local/ entry is reported, not silently ignored or deleted."""
    coll = _storage_collection()
    legacy_url = f"/local/{CARD_FILENAME}"
    _seed(coll, "legacy", legacy_url)

    await _async_register_resource(MagicMock(data=_hass_data(coll)), _VERSION)

    urls = {item["url"] for item in coll.async_items()}
    assert urls == {legacy_url, _EXPECTED_URL}
    assert legacy_url in caplog.text


async def test_register_card_is_registered_only_once() -> None:
    """The static path may only be registered once per Home Assistant run."""
    coll = _storage_collection()
    hass = MagicMock(data=_hass_data(coll))
    hass.http.async_register_static_paths = AsyncMock()

    with patch(
        "custom_components.fints_atruvia.frontend.async_get_integration",
        AsyncMock(return_value=SimpleNamespace(version=_VERSION)),
    ):
        await async_register_card(hass)
        await async_register_card(hass)

    assert hass.http.async_register_static_paths.await_count == 1
    static_config = hass.http.async_register_static_paths.await_args.args[0][0]
    assert static_config.url_path == CARD_URL_PATH
    assert len(coll.async_items()) == 1


async def test_register_card_survives_a_broken_registration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A card problem must never stop the sensors from loading."""
    hass = MagicMock(data={})
    hass.http.async_register_static_paths = AsyncMock(side_effect=RuntimeError("boom"))

    with patch(
        "custom_components.fints_atruvia.frontend.async_get_integration",
        AsyncMock(return_value=SimpleNamespace(version=_VERSION)),
    ):
        await async_register_card(hass)

    assert "RuntimeError" in caplog.text
    # The exception text itself must not be logged verbatim (log hygiene).
    assert "boom" not in caplog.text


def test_card_bundle_is_shipped() -> None:
    """The built card must be committed — HACS does not run rollup."""
    path = Path(_card_path())
    assert path.is_file(), "run `cd frontend && npm run build`"
    assert "fints-atruvia-card" in path.read_text(encoding="utf-8")
