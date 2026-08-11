"""Config flow for fints_atruvia integration."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import (
    CONF_BLZ,
    CONF_CREDENTIAL_ID,
    CONF_EXPOSE_FULL_DATA,
    CONF_PRODUCT_ID,
    CONF_SELECTED_ACCOUNTS,
    CONF_URL,
    DOMAIN,
    _entry_unique_id,
)
from .api import FinTsAtruviaClient, InvalidUrlError, NoTanMechanismError
from .storage import (
    CredentialStoreError,
    FintsCredentialStore,
    async_get_master_key,
)

if TYPE_CHECKING:
    from fints.client import NeedTANResponse
    from fints.models import SEPAAccount

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


def _validate_https_url(url: str) -> str | None:  # noqa: PLR0911 - one early return per rejection reason
    """Return an error code if *url* is not a valid https URL.

    Rejects:
    * non-https schemes (downgrade attack)
    * empty netloc
    * netlocs that cannot be encoded as plain ASCII via IDNA — protects
      against homoglyph / Punycode lookalike domains like ``атруvia.de``.
    """  # noqa: RUF002 - the lookalike letters above are the input this rejects
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid_url"
    if parsed.scheme.lower() != "https":
        return "insecure_url"
    if not parsed.netloc:
        return "invalid_url"
    # Strip optional userinfo and port before the IDNA check.
    host = parsed.hostname or ""
    if not host:
        return "invalid_url"
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host.encode("idna")
        except UnicodeError:
            return "invalid_url"
        # Non-ASCII hostname: refuse outright to avoid homoglyph attacks.
        return "invalid_url"
    return None


def _account_labels(accounts: list[SEPAAccount]) -> list[str]:
    """
    Return display labels for the account picker, one per account.

    Masked to the IBAN's last four digits only — the label travels through
    the WebSocket form to the browser (visible via DevTools to anyone with
    admin access), and the BLZ sits in ``entry.data`` from the same flow, so
    anything more specific (account number, subaccount, BIC) would let the
    full IBAN be reconstructed. When two or more offered accounts share the
    same last four digits, a non-identifying running number is appended so
    the options stay distinguishable.
    """
    last4_counts: dict[str, int] = {}
    for account in accounts:
        last4 = account.iban[-4:]
        last4_counts[last4] = last4_counts.get(last4, 0) + 1

    seen: dict[str, int] = {}
    labels: list[str] = []
    for account in accounts:
        last4 = account.iban[-4:]
        label = f"Konto …{last4}"
        if last4_counts[last4] > 1:
            seen[last4] = seen.get(last4, 0) + 1
            label = f"{label} ({seen[last4]})"
        labels.append(label)
    return labels


class FintsBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for fints_atruvia."""

    VERSION = 3

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

    @callback
    def async_remove(self) -> None:
        """
        Close a still-open FinTS client if the flow is abandoned.

        HA calls this synchronously from ``_async_remove_flow_progress`` when
        the dialog is closed without completing setup. Without it, the
        client — and the PIN python-fints holds internally — would stay
        referenced on this (now orphaned) flow object until HA later garbage
        collects it. ``close()`` is blocking, so it goes through the executor
        rather than running inline here.
        """
        client = self._client
        self._client = None
        if client is not None:
            self.hass.async_add_executor_job(client.close)

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
                # First-ever flow: this creates the master key. Intentional —
                # the credential save at the end of the flow reuses it.
                key = await async_get_master_key(self.hass)
                await self.async_set_unique_id(
                    _entry_unique_id(key, user_input["blz"], user_input["username"])
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
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - HA passes it to every step; this one shows no form
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
            result = await self.hass.async_add_executor_job(self._client.init_system_id)
        except NoTanMechanismError:
            _LOGGER.error(  # noqa: TRY400 - log hygiene: static text only, no exception content (SECURITY.md §10)
                "FinTS system-ID init failed: no supported two-step TAN mechanism"
            )
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors={"base": "no_tan_mechanism"},
            )
        except Exception as exc:  # noqa: BLE001
            # Log only the exception type. python-fints errors may quote
            # bank-response content (HBCI segments can include account
            # numbers) — keep that out of HA logs.
            _LOGGER.error(  # noqa: TRY400 - log hygiene: no python-fints traceback (SECURITY.md §10)
                "FinTS system-ID init failed: %s", type(exc).__name__
            )
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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error(  # noqa: TRY400 - log hygiene: no python-fints traceback (SECURITY.md §10)
                    "TAN challenge failed: %s", type(exc).__name__
                )
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
            accounts = await self.hass.async_add_executor_job(self._client.get_accounts)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error(  # noqa: TRY400 - log hygiene: no python-fints traceback (SECURITY.md §10)
                "Account fetch failed: %s", type(exc).__name__
            )
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

        # Mask the displayed IBAN — only the value (kept server-side via
        # selected_accounts) needs the full IBAN. The label travels through
        # the WebSocket form to the browser and can be sniffed via DevTools,
        # so it must not carry the account number: combined with the BLZ
        # (already visible in entry.data from this same flow), that would
        # reconstruct the full IBAN. See _account_labels().
        account_options = [
            selector.SelectOptionDict(value=account.iban, label=label)
            for account, label in zip(accounts, _account_labels(accounts), strict=True)
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
        # The FinTS dialog's last bank access (get_accounts / complete_tan)
        # already happened before this step runs — release the client (and
        # the PIN python-fints holds for it) now, in both the reauth and the
        # new-entry branch below, rather than leaving it referenced on the
        # flow object until HA discards it.
        if self._client is not None:
            client = self._client
            self._client = None
            await self.hass.async_add_executor_job(client.close)

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
            # Reauth: update credentials in place, reuse existing credential_id
            # so the encrypted blob is overwritten rather than orphaned. A
            # fresh uuid is generated only when migrating from a broken state.
            credential_id = (
                self._reauth_entry.data.get(CONF_CREDENTIAL_ID) or uuid.uuid4().hex
            )
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
            self._credentials = {}
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        # New entry: generate credential_id, persist credentials encrypted
        # BEFORE creating the config entry so the cleartext PIN never lives
        # in core.config_entries.
        credential_id = uuid.uuid4().hex
        await FintsCredentialStore(self.hass, credential_id).save(
            creds["username"], creds["password"]
        )
        result = self.async_create_entry(
            title=title,
            data={
                CONF_BLZ: creds["blz"],
                CONF_URL: effective_url,
                CONF_PRODUCT_ID: creds.get("product_id") or None,
                CONF_SELECTED_ACCOUNTS: selected,
                CONF_CREDENTIAL_ID: credential_id,
            },
        )
        # Drop the plaintext PIN from the flow object now that the encrypted
        # store owns it.
        self._credentials = {}
        return result

    # ------------------------------------------------------------------
    # Reauth flow
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],  # noqa: ARG002 - HA's signature; the entry comes from the flow context
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
        if entry is None:
            return self.async_abort(reason="reauth_no_entry")

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

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler for this entry."""
        return FintsAtruviaOptionsFlow(config_entry)


class FintsAtruviaOptionsFlow(OptionsFlow):
    """Per-entry options. Currently exposes the data-disclosure toggle."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Remember the entry whose options are being edited."""
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form, or store the submitted toggle."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options.get(CONF_EXPOSE_FULL_DATA, False)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_EXPOSE_FULL_DATA, default=current
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
