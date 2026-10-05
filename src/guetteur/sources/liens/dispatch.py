"""Dispatcher kind → reader.

Un unique point d'entrée `read_link(url, kind)` que la pipeline appelle. Tous
les lecteurs sont instanciés à la volée (ils n'ont pas d'état coûteux) ;
l'injection d'un lecteur personnalisé par kind reste possible via `ReaderBundle`
pour les tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from guetteur.items import ItemKind, LinkContent
from guetteur.sources.liens.article import ArticleReader
from guetteur.sources.liens.base import LinkReader
from guetteur.sources.liens.github import GitHubReader
from guetteur.sources.liens.tweet import TweetReader
from guetteur.sources.liens.youtube import YouTubeReader


def _default_readers() -> dict[ItemKind, LinkReader]:
    return {
        "tweet": TweetReader(),
        "article": ArticleReader(),
        "github": GitHubReader(),
        "youtube_oneshot": YouTubeReader(),
    }


@dataclass(frozen=True)
class ReaderBundle:
    readers: dict[ItemKind, LinkReader] = field(default_factory=_default_readers)

    def read(self, url: str, kind: ItemKind) -> LinkContent:
        reader = self.readers.get(kind)
        if reader is None:
            raise KeyError(f"Pas de lecteur pour kind={kind!r}")
        return reader.read(url)
