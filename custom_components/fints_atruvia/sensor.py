"""Sensor platform for fints_atruvia."""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN
from .coordinator import FintsBankingCoordinator


def _to_float(value: Any) -> float | None:
    """Convert Decimal/int/float to float, or return None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_iso(value: Any) -> str | None:
    """Return ISO date string for date/datetime, otherwise pass through string or None."""
    if value is None:
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up fints_atruvia sensor entities from a config entry."""
    coordinator: FintsBankingCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    selected_ibans: list[str] = config_entry.data.get("selected_accounts", [])

    entities: list[SensorEntity] = []
    for iban in selected_ibans:
        entities.append(FintsBankingSensor(coordinator, iban))
        entities.append(FintsIncomeSensor(coordinator, iban))
        entities.append(FintsExpenseSensor(coordinator, iban))
    async_add_entities(entities)


class FintsBankingSensor(CoordinatorEntity[FintsBankingCoordinator], SensorEntity):
    """Sensor entity representing a single bank account balance."""

    def __init__(self, coordinator: FintsBankingCoordinator, iban: str) -> None:
        """Initialise the sensor for the given IBAN."""
        super().__init__(coordinator)
        self._iban = iban
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{iban}"
        self._attr_name = f"Konto {iban[-4:]}"
        self._attr_icon = "mdi:bank"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.TOTAL

    @property
    def native_value(self):
        """Return the current account balance."""
        if not self.coordinator.data:
            return None
        balance = self.coordinator.data.get(self._iban, {}).get("balance")
        return _to_float(balance)

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the currency of the account."""
        return self.coordinator.data.get(self._iban, {}).get("currency", "EUR")

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return {}
        account_data = self.coordinator.data.get(self._iban, {})
        transactions = account_data.get("transactions", [])
        return {
            "iban": self._iban,
            "available_balance": _to_float(account_data.get("available_balance")),
            "balance_pending": _to_float(account_data.get("balance_pending")),
            "pending_amount": _to_float(account_data.get("pending_amount")),
            "booking_date": _date_iso(account_data.get("booking_date")),
            "transactions": [
                {**txn, "amount": float(txn["amount"])} if txn.get("amount") is not None else txn
                for txn in transactions[-10:]
            ],
            "2fa_pending": self.coordinator.is_2fa_pending,
        }


class _FintsStatsSensor(CoordinatorEntity[FintsBankingCoordinator], SensorEntity):
    """Base class for the rolling 30-day income / expense statistics sensors.

    Uses TOTAL because HA does not accept MEASUREMENT in combination with the
    MONETARY device class. TOTAL without ``last_reset`` records the value as a
    point-in-time total at each update — fine for rolling window sums, which
    HA's long-term statistics engine treats as a current snapshot.
    """

    _stats_key: str = ""
    _name_suffix: str = ""
    _icon: str = "mdi:cash"

    def __init__(self, coordinator: FintsBankingCoordinator, iban: str) -> None:
        super().__init__(coordinator)
        self._iban = iban
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{iban}_{self._stats_key}"
        )
        self._attr_name = f"Konto {iban[-4:]} {self._name_suffix}"
        self._attr_icon = self._icon
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.TOTAL

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        stats = self.coordinator.data.get(self._iban, {}).get("stats", {})
        return _to_float(stats.get(self._stats_key))

    @property
    def native_unit_of_measurement(self) -> str:
        return self.coordinator.data.get(self._iban, {}).get("currency", "EUR")

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {}
        stats = self.coordinator.data.get(self._iban, {}).get("stats", {})
        return {
            "iban": self._iban,
            "count_30d": stats.get("count_30d"),
        }


class FintsIncomeSensor(_FintsStatsSensor):
    """Sum of all positive transactions in the last 30 days."""

    _stats_key = "income_30d"
    _name_suffix = "Einnahmen 30T"
    _icon = "mdi:cash-plus"


class FintsExpenseSensor(_FintsStatsSensor):
    """Sum of all negative transactions in the last 30 days (absolute value)."""

    _stats_key = "expense_30d"
    _name_suffix = "Ausgaben 30T"
    _icon = "mdi:cash-minus"
