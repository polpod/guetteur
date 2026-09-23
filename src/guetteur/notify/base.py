"""Interface Notifier et découpage des messages longs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class NotifyError(RuntimeError):
    """Échec d'envoi d'une notification."""


@dataclass(frozen=True)
class Message:
    """Un même contenu dans les deux formats ; chaque canal choisit le sien."""

    markdown_v2: str
    plain: str
    # Texte court (titre + lien) utilisable comme variable de modèle WhatsApp.
    short: str = ""


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, message: Message) -> None:
        """Envoie le message ; lève NotifyError en cas d'échec."""


def _safe_cut(text: str, limit: int) -> int:
    """Position de coupe <= limit qui ne sépare pas un « \\x » d'échappement MarkdownV2."""
    cut = limit
    # Nombre de backslashes consécutifs juste avant la coupe : impair => échappement coupé.
    n = 0
    while cut - n - 1 >= 0 and text[cut - n - 1] == "\\":
        n += 1
    if n % 2 == 1:
        cut -= 1
    return cut


def split_message(text: str, limit: int) -> list[str]:
    """Découpe en morceaux de `limit` caractères max, de préférence sur des fins de ligne."""
    if limit <= 1:
        raise ValueError("limit doit être > 1")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            # Coupe sur le dernier espace si possible, sinon coupe brute sûre.
            cut = line.rfind(" ", 0, limit)
            if cut <= 0:
                cut = _safe_cut(line, limit)
            chunks.append(line[:cut])
            line = line[cut:].lstrip(" ")
        current = line
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]
