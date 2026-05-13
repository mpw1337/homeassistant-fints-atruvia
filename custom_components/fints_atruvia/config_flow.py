"""Config flow for fints_atruvia integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from fints.client import NeedTANResponse

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .api import FinTsAtruviaClient
from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

_BANK_URL_OPTIONS = [
    selector.SelectOptionDict(
        value="https://fints2.atruvia.de/cgi-bin/hbciservlet",
        label="Sparda-Bank (Atruvia)",
    ),
    selector.SelectOptionDict(
        value="custom",
        label="Andere (manuell eingeben)",
    ),
]

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("blz"): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Required("username"): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Required("password"): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Required("url"): selector.SelectSelector(
            selector.SelectSelectorConfig(options=_BANK_URL_OPTIONS)
        ),
        vol.Optional("custom_url"): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
        vol.Optional("product_id"): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
    }
)


class FintsBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for fints_atruvia."""

    VERSION = 1

    _credentials: dict[str, Any]
    _client: FinTsAtruviaClient | None
    _tan_response: NeedTANResponse | None

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._credentials = {}
        self._client = None
        self._tan_response = None

    # ------------------------------------------------------------------
    # Step 1: user credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — collect bank credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate: if "custom" selected, custom_url must be provided
            if user_input.get("url") == "custom" and not user_input.get("custom_url"):
                errors["custom_url"] = "required"
            else:
                self._credentials = user_input
                await self.async_set_unique_id(f"{user_input['blz']}_{user_input['username']}")
                self._abort_if_unique_id_configured()
                return await self.async_step_sync()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2: FinTS system-ID sync (no user-facing form)
    # ------------------------------------------------------------------

    async def async_step_sync(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Perform FinTS system-ID handshake — runs automatically, no form shown."""
        if not self._credentials:
            return await self.async_step_user()
        creds = self._credentials

        # Resolve the effective URL
        if creds.get("url") == "custom":
            effective_url = creds["custom_url"]
        else:
            effective_url = creds["url"]

        self._client = FinTsAtruviaClient(
            blz=creds["blz"],
            login=creds["username"],
            pin=creds["password"],
            url=effective_url,
            product_id=creds.get("product_id") or None,
        )

        try:
            result = await self.hass.async_add_executor_job(
                self._client.init_system_id
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error during FinTS system-ID init")
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors={"base": "cannot_connect"},
            )

        if result is None:
            return await self.async_step_accounts()

        # TAN required (SecureGo+ / pushTAN)
        self._tan_response = result
        return await self.async_step_2fa()

    # ------------------------------------------------------------------
    # Step 3: 2FA confirmation (optional — only when bank requires it)
    # ------------------------------------------------------------------

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show SecureGo+ confirmation prompt; wait for user to confirm in app."""
        if user_input is not None:
            # User clicked "Weiter" — complete the TAN challenge
            try:
                await self.hass.async_add_executor_job(
                    self._client.complete_tan,
                    self._tan_response,
                    "",
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error completing TAN challenge")
                return self.async_show_form(
                    step_id="2fa",
                    data_schema=vol.Schema({}),
                    errors={"base": "cannot_connect"},
                )

            return await self.async_step_accounts()

        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema({}),
        )

    # ------------------------------------------------------------------
    # Step 4: account selection
    # ------------------------------------------------------------------

    async def async_step_accounts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user select which accounts to track."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected: list[str] = user_input.get("selected_accounts", [])
            if not selected:
                errors["base"] = "no_accounts_found"
            else:
                creds = self._credentials
                effective_url = (
                    creds["custom_url"]
                    if creds.get("url") == "custom"
                    else creds["url"]
                )

                bank_label = next(
                    (
                        opt["label"]
                        for opt in _BANK_URL_OPTIONS
                        if opt["value"] == creds.get("url")
                    ),
                    None,
                )
                title = (
                    f"{bank_label} ({creds['blz']})"
                    if bank_label
                    else f"Sparda-Bank ({creds['blz']})"
                )
                return self.async_create_entry(
                    title=title,
                    data={
                        "blz": creds["blz"],
                        "username": creds["username"],
                        "password": creds["password"],
                        "url": effective_url,
                        "product_id": creds.get("product_id") or None,
                        "selected_accounts": selected,
                    },
                )

        # Fetch available accounts
        try:
            accounts = await self.hass.async_add_executor_job(
                self._client.get_accounts
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error fetching accounts")
            return self.async_show_form(
                step_id="accounts",
                data_schema=vol.Schema({}),
                errors={"base": "cannot_connect"},
            )

        if not accounts:
            return self.async_show_form(
                step_id="accounts",
                data_schema=vol.Schema({}),
                errors={"base": "no_accounts_found"},
            )

        account_options = [
            selector.SelectOptionDict(
                value=account.iban,
                label=f"{account.iban} ({account.accountnumber})",
            )
            for account in accounts
        ]

        schema = vol.Schema(
            {
                vol.Required("selected_accounts"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=account_options,
                        multiple=True,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="accounts",
            data_schema=schema,
            errors=errors,
        )
