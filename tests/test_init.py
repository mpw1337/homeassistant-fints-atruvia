"""Tests for entry setup: unique_id migration and the options update listener."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import attr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fints_atruvia import (
    CONF_BLZ,
    CONF_CREDENTIAL_ID,
    CONF_EXPOSE_FULL_DATA,
    CONF_PRODUCT_ID,
    CONF_SELECTED_ACCOUNTS,
    CONF_URL,
    DOMAIN,
    _async_migrate_unique_ids,
    _async_reload_entry,
    _entry_unique_id,
    async_migrate_entry,
    async_setup_entry,
    iban_unique_id,
)
from custom_components.fints_atruvia.storage import (
    FintsCredentialStore,
    async_get_master_key,
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


async def test_setup_entry_registers_the_options_update_listener(hass):
    """Fix 1: async_setup_entry must register the listener, not just define it."""
    # The test above calls _async_reload_entry directly, so it stays green even
    # if add_update_listener is dropped from async_setup_entry. This one drives
    # the real setup path and then flips an option.
    entry = _make_entry()
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.async_init = AsyncMock()
    coordinator.async_load_seen = AsyncMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    # Both are imported inside async_setup_entry, so patch them at the source.
    with (
        patch(
            "custom_components.fints_atruvia.frontend.async_register_card",
            AsyncMock(),
        ),
        patch(
            "custom_components.fints_atruvia.coordinator.FintsBankingCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry) is True

    hass.config_entries.async_reload = AsyncMock(return_value=True)
    hass.config_entries.async_update_entry(entry, options={CONF_EXPOSE_FULL_DATA: True})
    await hass.async_block_till_done()

    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


# ---------------------------------------------------------------------------
# Entry migration: cleartext unique_id (v1/v2) -> hashed unique_id (v3)
# ---------------------------------------------------------------------------


async def test_migrate_entry_v1_to_v3_chains_both_steps(hass):
    """A v1 entry must land on v3 in one call, with a hashed unique_id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "blz": "12345678",
            "username": "netkey1",
            "password": "hunter2",
            "url": "https://example.test/fints",
            "product_id": None,
            "selected_accounts": [_IBAN],
        },
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 3
    key = await async_get_master_key(hass)
    assert entry.unique_id == _entry_unique_id(key, "12345678", "netkey1")
    assert "password" not in entry.data
    assert "username" not in entry.data
    credential_id = entry.data[CONF_CREDENTIAL_ID]
    creds = await FintsCredentialStore(hass, credential_id).load()
    assert creds == {"username": "netkey1", "pin": "hunter2"}


async def test_migrate_entry_v2_to_v3_hashes_unique_id(hass):
    """A v2 entry gets its cleartext ``{blz}_{username}`` unique_id rehashed."""
    credential_id = "cred-v2"
    await FintsCredentialStore(hass, credential_id).save("netkey1", "hunter2")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="12345678_netkey1",
        data={
            CONF_BLZ: "12345678",
            CONF_URL: "https://example.test/fints",
            CONF_PRODUCT_ID: None,
            CONF_SELECTED_ACCOUNTS: [_IBAN],
            CONF_CREDENTIAL_ID: credential_id,
        },
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 3
    key = await async_get_master_key(hass)
    assert entry.unique_id == _entry_unique_id(key, "12345678", "netkey1")
    assert "netkey1" not in entry.unique_id
    assert "12345678" not in entry.unique_id


async def test_migrate_entry_v2_to_v3_keeps_legacy_unique_id_if_undecryptable(hass):
    """A lost master key must not block setup — bump the version, keep the id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="12345678_netkey1",
        data={
            CONF_BLZ: "12345678",
            CONF_URL: "https://example.test/fints",
            CONF_PRODUCT_ID: None,
            CONF_SELECTED_ACCOUNTS: [_IBAN],
            CONF_CREDENTIAL_ID: "never-saved-cred-id",
        },
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 3
    assert entry.unique_id == "12345678_netkey1"


async def test_migrate_entry_v2_to_v3_survives_unique_id_collision(hass):
    """A genuine blz/login duplicate must not block the version bump either."""
    credential_id = "cred-collision"
    await FintsCredentialStore(hass, credential_id).save("netkey1", "hunter2")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="12345678_netkey1",
        data={
            CONF_BLZ: "12345678",
            CONF_URL: "https://example.test/fints",
            CONF_PRODUCT_ID: None,
            CONF_SELECTED_ACCOUNTS: [_IBAN],
            CONF_CREDENTIAL_ID: credential_id,
        },
    )
    entry.add_to_hass(hass)

    real_update_entry = hass.config_entries.async_update_entry
    attempts: list[dict] = []

    def _flaky_update_entry(target_entry: MockConfigEntry, **kwargs: Any) -> bool:
        attempts.append(kwargs)
        if "unique_id" in kwargs:
            msg = "simulated duplicate unique_id"
            raise RuntimeError(msg)
        return real_update_entry(target_entry, **kwargs)

    hass.config_entries.async_update_entry = _flaky_update_entry

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 3
    # The failed attempt left the legacy unique_id in place.
    assert entry.unique_id == "12345678_netkey1"
    assert len(attempts) == 2
