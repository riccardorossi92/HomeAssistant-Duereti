"""Config flow per Duereti Letture."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DuretiApiClient, DuretiApiError, DuretiAuthError
from .const import CONF_CLIENT_ID, CONF_PODS, CONF_SECRET_ID, DOMAIN, GITHUB_REPO_URL

_LOGGER = logging.getLogger(__name__)


def _pod_gia_configurato(
    hass, pod: str, pods_gia_in_flow: list[dict] | None = None, escludi_entry_id: str | None = None
) -> str | None:
    """Verifica se il POD è già configurato in un'altra istanza dell'integrazione
    (o già aggiunto nel flow corrente prima di salvare). Restituisce il titolo
    della config entry in conflitto, o None se il POD è libero."""
    pod_norm = pod.strip().upper()

    for p in pods_gia_in_flow or []:
        if p["pod"].strip().upper() == pod_norm:
            return "questa stessa configurazione"

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == escludi_entry_id:
            continue
        for p in entry.data.get(CONF_PODS, []):
            if p["pod"].strip().upper() == pod_norm:
                return entry.title

    return None


async def _valida_credenziali(hass, client_id: str, secret_id: str) -> str | None:
    """Prova ad autenticarsi con le credenziali fornite.

    Restituisce None se valide, altrimenti la chiave di errore da mostrare
    ('invalid_auth' o 'cannot_connect').
    """
    session = async_get_clientsession(hass)
    client = DuretiApiClient(session, client_id, secret_id)
    try:
        await client.async_validate_credentials()
    except DuretiAuthError:
        return "invalid_auth"
    except DuretiApiError:
        return "cannot_connect"
    return None


STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_SECRET_ID): str,
    }
)

STEP_POD_SCHEMA = vol.Schema(
    {
        vol.Required("pod"): str,
        vol.Required("df"): str,
    }
)


class DuretiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce il flusso di configurazione da UI."""

    VERSION = 1

    def __init__(self) -> None:
        self._client_id: str | None = None
        self._secret_id: str | None = None
        self._pods: list[dict] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            errore = await _valida_credenziali(
                self.hass, user_input[CONF_CLIENT_ID], user_input[CONF_SECRET_ID]
            )
            if errore:
                errors["base"] = errore
            else:
                self._client_id = user_input[CONF_CLIENT_ID]
                self._secret_id = user_input[CONF_SECRET_ID]
                return await self.async_step_add_pod()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"github_repo_url": GITHUB_REPO_URL},
        )

    async def async_step_add_pod(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Permette di aggiungere uno o più POD con relativo codice fiscale."""
        errors: dict[str, str] = {}
        conflitto: str | None = None

        if user_input is not None:
            conflitto = _pod_gia_configurato(self.hass, user_input["pod"], pods_gia_in_flow=self._pods)
            if conflitto:
                errors["pod"] = "pod_duplicato"
            else:
                self._pods.append({"pod": user_input["pod"], "df": user_input["df"]})
                if user_input.get("aggiungi_altro"):
                    return await self.async_step_add_pod()
                return self.async_create_entry(
                    title=f"Duereti ({len(self._pods)} POD)",
                    data={
                        CONF_CLIENT_ID: self._client_id,
                        CONF_SECRET_ID: self._secret_id,
                        CONF_PODS: self._pods,
                    },
                )

        schema = STEP_POD_SCHEMA.extend({vol.Optional("aggiungi_altro", default=False): bool})
        return self.async_show_form(
            step_id="add_pod",
            data_schema=schema,
            errors=errors,
            description_placeholders={"pod_conflitto": conflitto or ""},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Avviato automaticamente da HA quando le API segnalano credenziali
        non più valide (401 su una chiamata del coordinator)."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            errore = await _valida_credenziali(
                self.hass, user_input[CONF_CLIENT_ID], user_input[CONF_SECRET_ID]
            )
            if errore:
                errors["base"] = errore
            else:
                nuovi_dati = {
                    **self._reauth_entry.data,
                    CONF_CLIENT_ID: user_input[CONF_CLIENT_ID],
                    CONF_SECRET_ID: user_input[CONF_SECRET_ID],
                }
                self.hass.config_entries.async_update_entry(self._reauth_entry, data=nuovi_dati)
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"github_repo_url": GITHUB_REPO_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "DuretiOptionsFlow":
        return DuretiOptionsFlow()


class DuretiOptionsFlow(config_entries.OptionsFlow):
    """Permette di aggiungere/rimuovere POD dopo la configurazione iniziale.

    Non serve un __init__ che salva config_entry: dalle versioni recenti di
    Home Assistant, self.config_entry è già fornito automaticamente dalla
    classe base ed è di sola lettura (assegnarlo a mano causa AttributeError:
    'property config_entry has no setter').
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Menu: aggiungi un nuovo POD, o rimuovi uno di quelli esistenti."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["aggiungi_pod", "rimuovi_pod"],
        )

    async def async_step_aggiungi_pod(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        errors: dict[str, str] = {}
        conflitto: str | None = None

        if user_input is not None:
            conflitto = _pod_gia_configurato(
                self.hass,
                user_input["pod"],
                pods_gia_in_flow=pods,
                escludi_entry_id=self.config_entry.entry_id,
            )
            if conflitto:
                errors["pod"] = "pod_duplicato"
            else:
                pods.append({"pod": user_input["pod"], "df": user_input["df"]})
                new_data = {**self.config_entry.data, CONF_PODS: pods}
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
                # Reload mirato solo qui: serve perché il coordinator riprenda
                # la lista POD aggiornata e sensor.py crei le entità per il
                # nuovo POD. Non è un listener generico: non scatta per le
                # scritture interne del coordinator (es. flag di backfill),
                # che altrimenti causerebbero un reload indesiderato ogni
                # volta che il backfill iniziale si completa.
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="aggiungi_pod",
            data_schema=STEP_POD_SCHEMA,
            errors=errors,
            description_placeholders={
                "pod_correnti": ", ".join(p["pod"] for p in pods) or "nessuno",
                "pod_conflitto": conflitto or "",
            },
        )

    async def async_step_rimuovi_pod(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        pods = list(self.config_entry.data.get(CONF_PODS, []))

        if not pods:
            return self.async_abort(reason="nessun_pod")

        if user_input is not None:
            da_rimuovere = set(user_input.get("pods_da_rimuovere", []))
            if len(da_rimuovere) >= len(pods):
                # Non permettiamo di svuotare del tutto la lista da qui:
                # se serve, l'utente rimuove direttamente l'integrazione.
                return self.async_show_form(
                    step_id="rimuovi_pod",
                    data_schema=self._schema_rimuovi(pods),
                    errors={"pods_da_rimuovere": "non_puoi_rimuoverli_tutti"},
                )
            pods_rimasti = [p for p in pods if p["pod"] not in da_rimuovere]
            new_data = {**self.config_entry.data, CONF_PODS: pods_rimasti}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            # Stesso motivo del reload in async_step_aggiungi_pod: serve solo
            # qui, non come listener generico sulla entry.
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(step_id="rimuovi_pod", data_schema=self._schema_rimuovi(pods))

    @staticmethod
    def _schema_rimuovi(pods: list[dict]) -> vol.Schema:
        opzioni = [p["pod"] for p in pods]
        return vol.Schema(
            {
                vol.Required("pods_da_rimuovere", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=opzioni, multiple=True)
                )
            }
        )
