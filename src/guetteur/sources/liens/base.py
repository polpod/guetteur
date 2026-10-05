"""Protocole commun des lecteurs de liens (Lot 8)."""

from __future__ import annotations

from typing import Protocol

from guetteur.items import LinkContent


class LinkReader(Protocol):
    """Un lecteur transforme une URL en LinkContent. Lève LinkFetchError si
    la lecture échoue (DNS, HTTP, parsing)."""

    def read(self, url: str) -> LinkContent: ...


# Pour les tests : réutiliser LinkFetchError pour tout échec de lecture.
from guetteur.sources.liens.net import LinkFetchError  # noqa: E402

__all__ = ["LinkFetchError", "LinkReader"]
