"""Interfaces communes de l'archivage : erreur, protocole, redaction, no-op."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from guetteur.models import Video

# Variables interdites par l'audit §8.3 : leur simple présence dans l'environnement
# du service pourrait exécuter du code arbitraire (NOTEBOOKLM_REFRESH_CMD*),
# forcer un transport non audité (NOTEBOOKLM_TRANSPORT), remplacer les cookies
# audités par une charge inline (NOTEBOOKLM_AUTH_JSON), ou déclencher une ré-auth
# headless via Chrome DevTools (NOTEBOOKLM_HEADLESS_REAUTH*). Le lot 3 refuse de
# démarrer l'archivage si l'une d'elles est présente et les retire de
# l'environnement passé au client (défense en profondeur).
FORBIDDEN_ENV_VARS: tuple[str, ...] = (
    "NOTEBOOKLM_REFRESH_CMD",
    "NOTEBOOKLM_REFRESH_CMD_USE_SHELL",
    "NOTEBOOKLM_REFRESH_CMD_MIDSESSION",
    "NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT",
    "NOTEBOOKLM_REFRESH_PROFILE",
    "NOTEBOOKLM_REFRESH_STORAGE_PATH",
    "NOTEBOOKLM_AUTH_JSON",
    "NOTEBOOKLM_HEADLESS_REAUTH",
    "NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL",
    "NOTEBOOKLM_TRANSPORT",
)


class ArchiveError(RuntimeError):
    """Échec d'archivage. retryable=True : rate limit ou 5xx, on retentera."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ArchiveOutcome:
    """Résultat d'un archivage réussi : le notebook cible et l'ID de la note."""

    notebook_id: str
    note_id: str


class Archiver(Protocol):
    """Un archiveur ajoute (video.url, résumé Markdown) à un notebook."""

    enabled: bool

    def archive(self, video: Video, summary_markdown: str) -> ArchiveOutcome: ...


class NoOpArchiver:
    """Archiveur inactif : renvoie l'exception disabled si on l'appelle."""

    enabled: bool = False

    def archive(self, video: Video, summary_markdown: str) -> ArchiveOutcome:
        raise ArchiveError("archivage désactivé (archive.enabled = false)", retryable=False)


# Motifs sensibles (audit §7.5) : cookies, jetons, en-têtes Google, secrets Android.
# Utilisé sur les messages d'erreur avant journalisation, JAMAIS sur les résumés.
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"ya29\.[A-Za-z0-9._~+/=-]+"),
    re.compile(r"aas_et/[A-Za-z0-9._~+/=-]+"),
    re.compile(r"SNlM0e[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(?:cookie|set-cookie):\s*[^\r\n]+"),
    re.compile(r"(?i)(?:authorization|x-goog-authuser|x-goog-visitor-id):\s*[^\r\n]+"),
    re.compile(r"__Secure-\d+PSIDT?S?=[^\s;,]+"),
    re.compile(r"SID=[^\s;,]+"),
)


def redact(text: str) -> str:
    """Masque cookies, tokens et en-têtes sensibles dans un message d'erreur."""
    out = text
    for pattern in _REDACTION_PATTERNS:
        out = pattern.sub("<redacted>", out)
    return out
