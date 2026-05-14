"""Config flow for fints_atruvia integration."""
from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from fints.client import NeedTANResponse

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from . import (
    CONF_BLZ,
    CONF_CREDENTIAL_ID,
    CONF_PRODUCT_ID,
    CONF_SELECTED_ACCOUNTS,
    CONF_URL,
    DOMAIN,
)
from .api import FinTsAtruviaClient, InvalidUrlError
from .storage import CredentialStoreError, FintsCredentialStore

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


def _validate_https_url(url: str) -> str | None:
    """Return an error code if *url* is not a valid https URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid_url"
    if parsed.scheme.lower() != "https":
        return "insecure_url"
    if not parsed.netloc:
        return "invalid_url"
    return None


class FintsBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for fints_atruvia."""

    VERSION = 2

    _credentials: dict[str, Any]
    _client: FinTsAtruviaClient | None
    _tan_response: NeedTANResponse | None
    _reauth_entry: ConfigEntry | None

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._credentials = {}
        self._client = None
        self._tan_response = None
        self._reauth_entry = None

    # ------------------------------------------------------------------
    # Step 1: user credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — collect bank credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url_choice = user_input.get("url")
            if url_choice == "custom":
                custom_url = (user_input.get("custom_url") or "").strip()
                if not custom_url:
                    errors["custom_url"] = "required"
                else:
                    url_error = _validate_https_url(custom_url)
                    if url_error:
                        errors["custom_url"] = url_error
            else:
                # The preset list only contains https URLs, but guard anyway.
                url_error = _validate_https_url(url_choice or "")
                if url_error:
                    errors["url"] = url_error

            if not errors:
                self._credentials = user_input
                await self.async_set_unique_id(
                    f"{user_input['blz']}_{user_input['username']}"
                )
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
        effective_url = self._effective_url(creds)

        # Plaintext PIN lives only inside this flow's memory; we never
        # write the unencrypted credentials to disk via async_create_entry.
        pin = creds["password"]
        try:
            self._client = FinTsAtruviaClient(
                blz=creds["blz"],
                login=creds["username"],
                pin_provider=lambda: pin,
                url=effective_url,
                product_id=creds.get("product_id") or None,
            )
        except InvalidUrlError:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors={"base": "insecure_url"},
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

    @staticmethod
    def _effective_url(creds: dict[str, Any]) -> str:
        if creds.get("url") == "custom":
            return creds["custom_url"]
        return creds["url"]

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
                return await self._finish_setup(selected)

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

    async def _finish_setup(self, selected: list[str]) -> ConfigFlowResult:
        """Persist credentials encrypted and create / update the config entry."""
        creds = self._credentials
        effective_url = self._effective_url(creds)
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

        if self._reauth_entry is not None:
            # Reauth: update credentials in place, reuse existing credential_id.
            credential_id = self._reauth_entry.data.get(CONF_CREDENTIAL_ID) or uuid.uuid4().hex
            await FintsCredentialStore(self.hass, credential_id).save(
                creds["username"], creds["password"]
            )
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                data={
                    CONF_BLZ: creds["blz"],
                    CONF_URL: effective_url,
                    CONF_PRODUCT_ID: creds.get("product_id") or None,
                    CONF_SELECTED_ACCOUNTS: selected,
                    CONF_CREDENTIAL_ID: credential_id,
                },
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        # New entry: generate credential_id, persist credentials encrypted
        # BEFORE creating the config entry so the cleartext PIN never lives
        # in core.config_entries.
        credential_id = uuid.uuid4().hex
        await FintsCredentialStore(self.hass, credential_id).save(
            creds["username"], creds["password"]
        )
        return self.async_create_entry(
            title=title,
            data={
                CONF_BLZ: creds["blz"],
                CONF_URL: effective_url,
                CONF_PRODUCT_ID: creds.get("product_id") or None,
                CONF_SELECTED_ACCOUNTS: selected,
                CONF_CREDENTIAL_ID: credential_id,
            },
        )

    # ------------------------------------------------------------------
    # Reauth flow
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth triggered by ConfigEntryAuthFailed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a fresh PIN / username for an existing entry."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None

        schema = vol.Schema(
            {
                vol.Required(
                    "username",
                    default=await self._suggested_username(entry),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Required("password"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )

        if user_input is not None:
            self._credentials = {
                "blz": entry.data.get(CONF_BLZ, ""),
                "username": user_input["username"],
                "password": user_input["password"],
                "url": entry.data.get(CONF_URL, ""),
                "product_id": entry.data.get(CONF_PRODUCT_ID),
            }
            # The stored URL is already validated, but re-check before use.
            url_error = _validate_https_url(self._credentials["url"])
            if url_error:
                return self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=schema,
                    errors={"base": url_error},
                )
            return await self.async_step_sync()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
        )

    async def _suggested_username(self, entry: ConfigEntry) -> str:
        """Best-effort retrieval of the stored username for the reauth form."""
        credential_id = entry.data.get(CONF_CREDENTIAL_ID)
        if not credential_id:
            return ""
        try:
            creds = await FintsCredentialStore(self.hass, credential_id).load()
        except CredentialStoreError:
            return ""
        return creds.get("username", "")
