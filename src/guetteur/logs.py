"""Logs structurés : une ligne JSON par événement sur stdout.

Redaction : chaque ligne JSON est passée dans `redact()` avant d'être écrite. Le
filtre couvre (1) les motifs de secrets connus — clés Google ``AIza...``, tokens
de bot Telegram ``<id>:<token>``, clés Anthropic ``sk-ant-...`` — (2) le
paramètre d'URL ``key=...`` (utilisé par la YouTube Data API par clé) et (3) les
valeurs des variables d'environnement sensibles enregistrées via
`register_secret()` au démarrage. Le même filtre est exposé pour durcir les
messages sortants (Telegram) — une alerte accidentelle contenant un token ne
quittera jamais le processus en clair.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}

REDACTED = "[REDACTED]"

# Motifs auto-reconnus, par forme — toujours actifs, même sans enregistrement.
# AIza... : clé Google (API Keys, 39 caractères total).
# <chiffres>:<token> : jeton de bot Telegram, souvent précédé de ``bot`` dans
#   l'URL — pas de ``\b`` en tête, qui bloquerait le match dans
#   ``api.telegram.org/bot1234567890:AA.../sendMessage``.
# sk-ant-... : clé API Anthropic.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\d{8,10}:[A-Za-z0-9_\-]{35}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    # URL ``?key=<secret>&...`` : on remplace la valeur mais on garde le nom du
    # paramètre pour que les messages restent lisibles.
    re.compile(r"(?P<prefix>[?&]key=)[^&\s\"'<>]+"),
)
# `key=` est capturé par un groupe nommé : on reconstitue le préfixe au remplacement.
_URL_KEY_PATTERN = _SECRET_PATTERNS[-1]

# Valeurs enregistrées explicitement (lues dans l'environnement au démarrage).
# On garde une liste et pas un set : la boucle de remplacement est O(n) mais n ≤ 5.
_REGISTERED_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Ajoute `value` à la liste des chaînes à remplacer par ``[REDACTED]``.

    Les valeurs vides ou trop courtes sont ignorées : rendre « 1 » ou « ok »
    illisible casserait les logs sans gain de sécurité. Seuil : 8 caractères,
    sous la longueur du plus court secret attendu."""
    if not value or len(value) < 8:
        return
    if value not in _REGISTERED_SECRETS:
        _REGISTERED_SECRETS.append(value)


def _reset_registered_secrets_for_tests() -> None:
    """Vide la liste des secrets enregistrés — réservé aux tests."""
    _REGISTERED_SECRETS.clear()


def redact(text: str) -> str:
    """Remplace par ``[REDACTED]`` les motifs connus et les valeurs enregistrées.

    Idempotent, sans allocation si rien ne matche (fast path via `in`). Préserve
    la validité JSON : le remplacement ne contient aucun caractère spécial."""
    if not text:
        return text
    # 1) Motifs réguliers.
    for pattern in _SECRET_PATTERNS:
        if pattern is _URL_KEY_PATTERN:
            # Garde le préfixe ``?key=`` ou ``&key=`` dans le texte redacté.
            text = pattern.sub(lambda m: f"{m.group('prefix')}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    # 2) Valeurs enregistrées (tokens exacts lus de l'env au démarrage).
    for value in _REGISTERED_SECRETS:
        if value and value in text:
            text = text.replace(value, REDACTED)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


# Variables d'environnement dont la valeur doit être redactée dans les logs et
# dans les messages Telegram sortants. Lecture *après* `load_dotenv()` dans la
# CLI, qui appelle `setup_logging` juste après le chargement de la config.
_SECRET_ENV_VARS: tuple[str, ...] = (
    "YOUTUBE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WA_TOKEN",
    "ANTHROPIC_API_KEY",
)


def setup_logging(level: str = "INFO") -> None:
    for name in _SECRET_ENV_VARS:
        register_secret(os.environ.get(name))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Bibliothèques bavardes.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
