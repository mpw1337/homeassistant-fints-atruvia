"""Tests for sensor.py's extra_state_attributes on an uninitialised coordinator."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.fints_atruvia.sensor import FintsBankingSensor

_IBAN = "DE11500105175407324931"


def _make_sensor(*, data: dict | None) -> FintsBankingSensor:
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = "entry"
    coordinator.data = data
    coordinator.is_2fa_pending = True
    coordinator.expose_full_data = False
    return FintsBankingSensor(coordinator, _IBAN)


def test_2fa_pending_attribute_survives_an_empty_first_poll():
    """First poll with an SCA challenge returns {} from the coordinator.

    The sensor must still expose ``2fa_pending`` so the Lovelace card's
    re-auth hint has something to key off — only ``coordinator.data is None``
    (never initialised at all) should suppress attributes entirely.
    """
    sensor = _make_sensor(data={})

    attrs = sensor.extra_state_attributes

    assert attrs["2fa_pending"] is True
    assert "transactions" not in attrs


def test_extra_state_attributes_empty_when_data_is_none():
    sensor = _make_sensor(data=None)

    assert sensor.extra_state_attributes == {}
