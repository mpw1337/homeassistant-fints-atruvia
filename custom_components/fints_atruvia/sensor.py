"""Sensor platform for fints_atruvia."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import FintsBankingCoordinator
from . import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up fints_atruvia sensor entities from a config entry."""
    coordinator: FintsBankingCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([
        FintsBankingSensor(coordinator, iban)
        for iban in (coordinator.data or {})
    ])


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
        balance = self.coordinator.data.get(self._iban, {}).get("balance")
        return float(balance) if balance is not None else None

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the currency of the account."""
        return self.coordinator.data.get(self._iban, {}).get("currency", "EUR")

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional state attributes."""
        account_data = self.coordinator.data.get(self._iban, {})
        transactions = account_data.get("transactions", [])
        return {
            "iban": self._iban,
            "transactions": [
                {**txn, "amount": float(txn["amount"])} if txn.get("amount") is not None else txn
                for txn in transactions[-10:]
            ],
            "2fa_pending": self.coordinator.is_2fa_pending,
        }
