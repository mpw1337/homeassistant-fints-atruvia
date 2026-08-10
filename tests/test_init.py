"""Tests for entry setup: unique_id migration and the options update listener."""

from __future__ import annotations

from unittest.mock import AsyncMock

import attr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fints_atruvia import (
    CONF_BLZ,
    CONF_CREDENTIAL_ID,
    CONF_PRODUCT_ID,
    CONF_SELECTED_ACCOUNTS,
    CONF_URL,
    DOMAIN,
    _async_migrate_unique_ids,
    _async_reload_entry,
    iban_unique_id,
)

_IBAN = "DE89370400440532013000"


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BLZ: "12345678",
            CONF_URL: "https://example.test/fints",
            CONF_PRODUCT_ID: None,
            CONF_SELECTED_ACCOUNTS: [_IBAN],
            CONF_CREDENTIAL_ID: "test-cred-id",
        },
        options={},
    )


async def test_migration_clears_previous_unique_id(hass):
    """Fix 2: the legacy plaintext IBAN must not survive as previous_unique_id."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy_uid = f"{entry.entry_id}_{_IBAN}"
    reg_entry = registry.async_get_or_create(
        "sensor", DOMAIN, legacy_uid, config_entry=entry
    )

    await _async_migrate_unique_ids(hass, entry)

    migrated = registry.async_get(reg_entry.entity_id)
    assert migrated is not None
    expected_uid = f"{entry.entry_id}_{iban_unique_id(entry.entry_id, _IBAN)}"
    assert migrated.unique_id == expected_uid
    assert migrated.previous_unique_id is None
    # No plaintext IBAN left anywhere on the entry, not just in the two
    # unique_id fields checked above.
    for value in attr.asdict(migrated).values():
        assert _IBAN not in str(value)


async def test_migration_preserves_stats_suffixes(hass):
    """Income/expense stats sensors keep their suffix through the rehash."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy_uid = f"{entry.entry_id}_{_IBAN}_income_30d"
    reg_entry = registry.async_get_or_create(
        "sensor", DOMAIN, legacy_uid, config_entry=entry
    )

    await _async_migrate_unique_ids(hass, entry)

    migrated = registry.async_get(reg_entry.entity_id)
    assert migrated is not None
    assert migrated.unique_id.endswith("_income_30d")
    assert migrated.previous_unique_id is None


async def test_migration_leaves_reauth_button_untouched(hass):
    """The re-auth button has no IBAN in its unique_id and must not be rewritten."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    button_uid = f"{entry.entry_id}_reauth_button"
    reg_entry = registry.async_get_or_create(
        "button", DOMAIN, button_uid, config_entry=entry
    )

    await _async_migrate_unique_ids(hass, entry)

    unchanged = registry.async_get(reg_entry.entity_id)
    assert unchanged is not None
    assert unchanged.unique_id == button_uid
    assert unchanged.previous_unique_id is None


async def test_options_update_triggers_reload(hass):
    """Fix 1: flipping an option must reload the entry, not wait for the poll."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_reload = AsyncMock(return_value=True)

    await _async_reload_entry(hass, entry)

    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
