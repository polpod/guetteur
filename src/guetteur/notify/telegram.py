"""Notification via l'API Bot Telegram (sendMessage, parse_mode MarkdownV2).

Envoi durci :

- Pré-validation : chaque partie MarkdownV2 est passée dans `validate_markdown_v2` AVANT
  d'envoyer la première. Si l'une échoue, on bascule TOUT le message en texte brut, on
  loggue `markdown_v2.fallback_plain` et aucune part MarkdownV2 n'est envoyée : plus
  jamais de message partiellement envoyé.
- Fallback à la volée : si Telegram renvoie quand même 400 « can't parse entities » sur
  une partie donnée (validateur trop indulgent, glyphe exotique…), les parties DÉJÀ
  envoyées ne sont pas renvoyées ; SEULES les parties restantes basculent en texte brut,
  avec le même préfixe (i/N), et l'incident est loggué avec l'index et un extrait de 80
  caractères autour de l'offset signalé par Telegram."""

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
from guetteur.summarize.format import (
    excerpt_around,
    telegram_byte_offset,
    validate_markdown_v2,
)

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
# Motif d'erreur Telegram signalant un problème d'analyse MarkdownV2 (échappement).
_PARSE_ERROR_MARKERS = (
    "can't parse entities",
    "character entities",
    "reserved and must be escaped",
)


def _is_parse_entities_error(exc: NotifyError) -> bool:
    """`exc` provient de Telegram et signale une erreur de parsing MarkdownV2."""
    if exc.status_code != 400:
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _PARSE_ERROR_MARKERS)


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

    def _post_markdown(self, text: str) -> str | None:
        return self._post(
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "link_preview_options": {"is_disabled": True},
            }
        )

    def _post_plain(self, text: str) -> str | None:
        """Envoi texte brut : pas de parse_mode. Les caractères réservés MarkdownV2 sont
        laissés tels quels — Telegram ne les interprète plus."""
        return self._post(
            {
                "chat_id": self._chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            }
        )

    def send(self, message: Message) -> str | None:
        md_parts, plain_parts = self._select_parts(message)
        # Pré-validation : si UNE partie n'est pas parsable, tout bascule en texte brut.
        invalid_index = self._find_invalid_part(md_parts)
        if invalid_index is not None:
            log.warning(
                "markdown_v2.fallback_plain",
                extra={
                    "reason": "pre_validation",
                    "invalid_index": invalid_index + 1,
                    "total": len(md_parts),
                },
            )
            return self._send_all_plain(plain_parts)
        # Envoi MarkdownV2 avec fallback à la volée si Telegram rejette une part.
        ids: list[str] = []
        for i, part in enumerate(md_parts):
            try:
                message_id = self._post_markdown(part)
            except NotifyError as exc:
                if not _is_parse_entities_error(exc):
                    raise
                # Fallback à la volée : on n'a PAS envoyé cette part ni les suivantes.
                # On envoie SEULEMENT le reste (à partir de i) en texte brut, sans
                # doublonner ce qui a déjà été envoyé.
                offset = telegram_byte_offset(str(exc)) or 0
                excerpt = excerpt_around(part, offset).replace("\n", " ")[:80]
                log.warning(
                    "markdown_v2.fallback_plain",
                    extra={
                        "reason": "telegram_parse_error",
                        "part_index": i + 1,
                        "total": len(md_parts),
                        "byte_offset": offset,
                        "excerpt": excerpt,
                        "error": str(exc),
                    },
                )
                plain_tail = plain_parts[i:] if i < len(plain_parts) else [part]
                tail_ids = self._send_plain_sequence(plain_tail)
                ids.extend(tail_ids)
                log.info(
                    "telegram.sent",
                    extra={
                        "parts": len(md_parts),
                        "parse_mode": "MarkdownV2+plain_fallback",
                        "fallback_from": i + 1,
                    },
                )
                return ",".join(ids) or None
            if message_id:
                ids.append(message_id)
        log.info("telegram.sent", extra={"parts": len(md_parts), "parse_mode": "MarkdownV2"})
        return ",".join(ids) or None

    def _select_parts(self, message: Message) -> tuple[list[str], list[str]]:
        """Choisit les listes de parts à envoyer, MarkdownV2 et brut, alignées 1:1.

        Si les parts MarkdownV2 pré-numérotées sont fournies, on utilise les parts brutes
        équivalentes ; sinon on découpe `markdown_v2` et `plain` de la même façon pour
        garder un mapping stable entre les deux listes (permet le fallback à la volée)."""
        if message.markdown_v2_parts:
            md_parts = list(message.markdown_v2_parts)
        else:
            md_parts = split_message(message.markdown_v2, TELEGRAM_LIMIT)
        if message.plain_parts:
            plain_parts = list(message.plain_parts)
        else:
            plain_parts = split_message(message.plain, TELEGRAM_LIMIT)
        # Aligner les longueurs quand elles diffèrent (rendu bref/standard : 1 seule part
        # de chaque côté, sinon on complète le brut par des morceaux vides pour indexation
        # défensive lors du fallback).
        while len(plain_parts) < len(md_parts):
            plain_parts.append("")
        return md_parts, plain_parts

    @staticmethod
    def _find_invalid_part(md_parts: list[str]) -> int | None:
        """Retourne l'index de la première part MarkdownV2 mal formée, ou None si toutes
        sont valides."""
        for i, part in enumerate(md_parts):
            ok, _ = validate_markdown_v2(part)
            if not ok:
                return i
        return None

    def _send_all_plain(self, plain_parts: list[str]) -> str | None:
        ids = self._send_plain_sequence(plain_parts)
        log.info("telegram.sent", extra={"parts": len(plain_parts), "parse_mode": "plain"})
        return ",".join(ids) or None

    def _send_plain_sequence(self, plain_parts: list[str]) -> list[str]:
        ids: list[str] = []
        for part in plain_parts:
            if not part:
                continue
            message_id = self._post_plain(part)
            if message_id:
                ids.append(message_id)
        return ids
