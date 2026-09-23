"""Structures de données partagées entre les modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
class Summary:
    title: str
    tldr: str
    key_points: tuple[KeyPoint, ...]
    why_it_matters: str
    reading_time_minutes: int
