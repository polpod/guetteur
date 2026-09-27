"""Structures de données partagées entre les modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Niveau de détail du résumé (par playlist ou surchargé en CLI).
DetailLevel = Literal["bref", "standard", "detaille"]
DETAIL_LEVELS: tuple[DetailLevel, ...] = ("bref", "standard", "detaille")


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    channel: str
    published: datetime | None
    url: str


@dataclass(frozen=True)
class Segment:
    start: float
    text: str


@dataclass(frozen=True)
class Transcript:
    video_id: str
    language: str
    source: str  # "youtube" ou "whisper"
    segments: tuple[Segment, ...]

    def to_timestamped_text(self) -> str:
        """Texte avec l'offset en secondes de chaque segment, pour que Claude puisse citer."""
        return "\n".join(f"[{int(s.start)}s] {s.text.strip()}" for s in self.segments if s.text)


@dataclass(frozen=True)
class KeyPoint:
    seconds: int
    text: str


@dataclass(frozen=True)
class Section:
    """Une section thématique du mode « detaille » (titre + timestamp + puces)."""

    title: str
    seconds: int
    bullets: tuple[str, ...]


@dataclass(frozen=True)
class Citation:
    """Passage marquant reformulé (mode « detaille »)."""

    seconds: int
    text: str


@dataclass(frozen=True)
class Summary:
    title: str
    tldr: str
    key_points: tuple[KeyPoint, ...]
    why_it_matters: str
    reading_time_minutes: int
    # Champs Lot 4 : renseignés selon le niveau de détail. « standard » ignore
    # sections/citations/reserves ; « bref » n'a qu'une action ; « detaille » les
    # remplit tous. Défauts vides pour rester rétrocompatible avec les résumés du
    # Lot 1/2/3 déjà en base.
    detail: DetailLevel = "standard"
    sections: tuple[Section, ...] = field(default_factory=tuple)
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    actions: tuple[str, ...] = field(default_factory=tuple)
    reserves: tuple[str, ...] = field(default_factory=tuple)
