"""Tests for the encrypted credential and FinTS-state stores."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from custom_components.fints_atruvia.storage import (
    CredentialStoreError,
    FintsCredentialStore,
    FintsStateStore,
    _get_or_create_master_key,
)


async def test_credential_store_round_trip(hass):
    store = FintsCredentialStore(hass, "test-cred-id")
    await store.save("alice", "hunter2")
    loaded = await store.load()
    assert loaded == {"username": "alice", "pin": "hunter2"}


async def test_credential_store_rejects_corrupt_ciphertext(hass):
    store = FintsCredentialStore(hass, "test-cred-id")
    await store.save("alice", "hunter2")

    # Tamper with the on-disk ciphertext.
    raw = await store._store.async_load()
    raw["ciphertext"] = "not-a-valid-fernet-token"
    await store._store.async_save(raw)

    with pytest.raises(CredentialStoreError):
        await store.load()


async def test_credential_store_load_without_save(hass):
    store = FintsCredentialStore(hass, "missing-id")
    with pytest.raises(CredentialStoreError):
        await store.load()


async def test_state_store_round_trip_encrypted(hass):
    store = FintsStateStore(hass, "test-cred-id")
    payload = b"system_id=12345;BPD=...binary..."
    await store.save(payload)
    # Round-trip through load
    assert await store.load() == payload

    # The on-disk representation must NOT contain the plaintext.
    raw = await store._store.async_load()
    assert "ciphertext" in raw
    assert "blob" not in raw  # legacy hex key is gone
    assert payload.hex() not in raw["ciphertext"]


async def test_state_store_reads_real_v1_file(hass, hass_storage):
    """A v1 file (version: 1, plaintext hex) must load and then re-encrypt."""
    hass_storage["fints_atruvia_state_v1-id"] = {
        "version": 1,
        "minor_version": 1,
        "key": "fints_atruvia_state_v1-id",
        "data": {"blob": b"legacy-state".hex()},
    }
    store = FintsStateStore(hass, "v1-id")
    assert await store.load() == b"legacy-state"


async def test_state_store_migrates_v1_file_on_next_save(hass, hass_storage):
    """After reading a v1 file, save() must rewrite it as v2 ciphertext."""
    hass_storage["fints_atruvia_state_v1-id"] = {
        "version": 1,
        "minor_version": 1,
        "key": "fints_atruvia_state_v1-id",
        "data": {"blob": b"legacy-state".hex()},
    }
    store = FintsStateStore(hass, "v1-id")
    assert await store.load() == b"legacy-state"

    await store.save(b"new-state")

    raw = hass_storage["fints_atruvia_state_v1-id"]["data"]
    assert "ciphertext" in raw
    assert "blob" not in raw
    assert await store.load() == b"new-state"


async def test_state_store_rejects_corrupt_v1_hex(hass, hass_storage):
    """A v1 file with unparseable hex must yield None, not raise."""
    hass_storage["fints_atruvia_state_v1-id"] = {
        "version": 1,
        "minor_version": 1,
        "key": "fints_atruvia_state_v1-id",
        "data": {"blob": "nothex"},
    }
    store = FintsStateStore(hass, "v1-id")
    assert await store.load() is None


async def test_state_store_remove_when_none(hass):
    store = FintsStateStore(hass, "removable-id")
    await store.save(b"data")
    await store.save(None)
    assert await store.load() is None


async def test_master_key_is_persistent(hass):
    key1 = await _get_or_create_master_key(hass)
    key2 = await _get_or_create_master_key(hass)
    assert key1 == key2
    # Sanity: should be a valid Fernet key.
    Fernet(key1)
