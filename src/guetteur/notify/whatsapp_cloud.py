"""Notification via l'API WhatsApp Cloud (Meta Graph v20+).

Un message texte libre n'est accepté que dans la fenêtre de 24 h suivant le dernier message
reçu du destinataire. Hors fenêtre, Meta refuse le texte (erreur 131047) : on envoie alors un
message modèle (template) approuvé, qui rouvre la conversation dès que le destinataire répond."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from guetteur.config import WhatsAppConfig
from guetteur.notify.base import Message, Notifier, NotifyError, split_message

log = logging.getLogger(__name__)

WHATSAPP_TEXT_LIMIT = 4096
TEMPLATE_PARAM_LIMIT = 1024
# 131047 : « Re-engagement message » (fenêtre de 24 h fermée).
# 131026 : message non délivrable (souvent la même cause côté destinataire).
WINDOW_CLOSED_CODES = frozenset({131047, 131026})


class WindowClosedError(NotifyError):
    """Fenêtre de service de 24 h fermée pour ce destinataire."""


def _template_param(text: str) -> str:
    # Les variables de modèle n'acceptent ni retours ligne, ni tabulations, ni 4+ espaces.
    flat = " ".join(text.split())
    return flat[:TEMPLATE_PARAM_LIMIT]


class WhatsAppCloudNotifier(Notifier):
    name = "whatsapp"

    def __init__(
        self,
        token: str,
        phone_id: str,
        to: str,
        settings: WhatsAppConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not token or not phone_id or not to:
            raise NotifyError("WA_TOKEN, WA_PHONE_ID et WA_TO sont requis")
        self._settings = settings or WhatsAppConfig()
        self._url = f"https://graph.facebook.com/{self._settings.api_version}/{phone_id}/messages"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._to = to
        self._client = client or httpx.Client(timeout=20.0)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": self._to}
        body.update(payload)
        try:
            resp = self._client.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise NotifyError(f"WhatsApp injoignable : {type(exc).__name__}") from exc
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or "error" in data:
            err: dict[str, Any] = data.get("error", {})
            code = err.get("code")
            msg = err.get("message", resp.text)
            if code in WINDOW_CLOSED_CODES:
                raise WindowClosedError(f"WhatsApp {code} : {msg}")
            raise NotifyError(f"WhatsApp HTTP {resp.status_code} (code {code}) : {msg}")
        return data

    def _send_text(self, text: str) -> None:
        self._post({"type": "text", "text": {"preview_url": True, "body": text}})

    def _send_template(self, message: Message) -> None:
        template: dict[str, Any] = {
            "name": self._settings.template_name,
            "language": {"code": self._settings.template_language},
        }
        if self._settings.template_body_param:
            param = _template_param(message.short or message.plain)
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": param}]}
            ]
        self._post({"type": "template", "template": template})

    def send(self, message: Message) -> None:
        parts = split_message(message.plain, WHATSAPP_TEXT_LIMIT)
        try:
            self._send_text(parts[0])
        except WindowClosedError:
            log.warning("whatsapp.window_closed", extra={"template": self._settings.template_name})
            self._send_template(message)
            return
        for part in parts[1:]:
            self._send_text(part)
        log.info("whatsapp.sent", extra={"parts": len(parts)})
