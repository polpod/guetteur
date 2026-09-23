"""Notification via l'API Bot Telegram (sendMessage, parse_mode MarkdownV2)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from guetteur.notify.base import Message, Notifier, NotifyError, split_message

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        if not token or not chat_id:
            raise NotifyError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID sont requis")
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=20.0)

    def _post(self, payload: dict[str, Any]) -> None:
        try:
            resp = self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"Telegram injoignable : {type(exc).__name__}") from exc
        if resp.status_code != 200:
            try:
                desc = resp.json().get("description", resp.text)
            except ValueError:
                desc = resp.text
            raise NotifyError(f"Telegram HTTP {resp.status_code} : {desc}")

    def send(self, message: Message) -> None:
        parts = split_message(message.markdown_v2, TELEGRAM_LIMIT)
        for part in parts:
            self._post(
                {
                    "chat_id": self._chat_id,
                    "text": part,
                    "parse_mode": "MarkdownV2",
                    "link_preview_options": {"is_disabled": True},
                }
            )
        log.info("telegram.sent", extra={"parts": len(parts)})
