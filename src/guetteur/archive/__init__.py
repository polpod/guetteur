"""Archivage optionnel de la veille dans un notebook Google NotebookLM.

Le module reste importable même sans notebooklm-py installé : la bibliothèque
est chargée paresseusement quand l'archivage est activé et utilisé pour la
première fois. Voir docs/audit-notebooklm (§8 conditions) pour les règles de
sécurité verrouillées ici (variables d'environnement interdites, permissions
0700/0600 sur NOTEBOOKLM_HOME, redaction, épinglage 0.8.3 avec hash sha256)."""

from __future__ import annotations

from guetteur.archive.base import (
    FORBIDDEN_ENV_VARS,
    ArchiveError,
    ArchiveOutcome,
    Archiver,
    NoOpArchiver,
    redact,
)
from guetteur.archive.notebooklm import NotebookLMArchiver

__all__ = [
    "FORBIDDEN_ENV_VARS",
    "ArchiveError",
    "ArchiveOutcome",
    "Archiver",
    "NoOpArchiver",
    "NotebookLMArchiver",
    "redact",
]
