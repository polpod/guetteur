"""Notification via l'API Bot Telegram (sendMessage, parse_mode MarkdownV2)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from guetteur.notify.base import (
    Message,
    Notifier,
    NotifyError,
    is_retryable_status,
    split_message,
)

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

    def _post(self, payload: dict[str, Any]) -> str | None:
        try:
            resp = self._client.post(self._url, json=payload)
        except httpx.TimeoutException as exc:
            raise NotifyError("Telegram : délai dépassé", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise NotifyError(
                f"Telegram injoignable : {type(exc).__name__}", retryable=True
            ) from exc
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200:
            desc = data.get("description") or resp.text[:200] or "réponse vide"
            retry_after = (data.get("parameters") or {}).get("retry_after")
            raise NotifyError(
                f"Telegram HTTP {resp.status_code} : {desc}",
                retryable=is_retryable_status(resp.status_code),
                status_code=resp.status_code,
                retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
            )
        message_id = (data.get("result") or {}).get("message_id")
        return str(message_id) if message_id is not None else None

    def send(self, message: Message) -> str | None:
        parts = split_message(message.markdown_v2, TELEGRAM_LIMIT)
        ids: list[str] = []
        for part in parts:
            message_id = self._post(
                {
                    "chat_id": self._chat_id,
                    "text": part,
                    "parse_mode": "MarkdownV2",
                    "link_preview_options": {"is_disabled": True},
                }
            )
            if message_id:
                ids.append(message_id)
        log.info("telegram.sent", extra={"parts": len(parts)})
        return ",".join(ids) or None
