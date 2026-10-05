"""Modèle d'item LIEN (Lot 8).

Un item représente un lien (tweet, article web, repo GitHub, vidéo YouTube hors
playlist) partagé au bot ou injecté par un collecteur (Lot 8b : retweets X). Il
suit la même machine à états que les vidéos (new → fetched → summarized →
sending → sent) mais vit dans une table parallèle `items` : la table `videos`
reste intacte, aucune colonne n'est renommée, aucune migration destructive.

Choix (générique vs table parallèle) : table parallèle. Rationale : fusionner
`videos` imposerait `transcript` / `playlist_id` NULLables et rendrait tous les
chemins vidéo défensifs sur des champs qui n'ont pas de sens pour un lien ; le
gain tiendrait en un ou deux index. L'isolation évite toute régression sur le
pipeline vidéo existant.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

ItemKind = Literal["tweet", "article", "github", "youtube_oneshot"]
ITEM_KINDS: tuple[ItemKind, ...] = ("tweet", "article", "github", "youtube_oneshot")

ItemSource = Literal["telegram", "cli", "x_retweets", "x_bookmarks"]
ITEM_SOURCES: tuple[ItemSource, ...] = ("telegram", "cli", "x_retweets", "x_bookmarks")


class ItemStatus(StrEnum):
    NEW = "new"
    FETCHED = "fetched"
    SUMMARIZED = "summarized"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    RETRY = "retry"


ITEM_PENDING = (
    ItemStatus.NEW,
    ItemStatus.RETRY,
    ItemStatus.FETCHED,
    ItemStatus.SUMMARIZED,
)


def item_id_for(url: str) -> str:
    """ID stable d'un lien = sha1 de l'URL normalisée. 16 hex chars suffisent
    pour éviter les collisions au volume attendu (< 10^6 items)."""
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LinkContent:
    """Données extraites d'un lien, prêtes à être résumées."""

    url: str
    kind: ItemKind
    title: str
    author: str
    published_at: datetime | None
    text: str
    extras: dict[str, str]  # métadonnées libres : thread_count, paywall=true, langs…


@dataclass(frozen=True)
class LinkItem:
    """État persistant d'un item LIEN (reflet d'une ligne de la table items)."""

    item_id: str
    kind: ItemKind
    url: str
    source: ItemSource
    status: ItemStatus
    retries: int
    title: str
    author: str
    published_at: datetime | None
    content: str | None
    summary: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    send_attempt_at: datetime | None
    archived_at: datetime | None
    theme: str

    @property
    def really_sent(self) -> bool:
        return self.status == ItemStatus.SENT and self.sent_at is not None

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None
