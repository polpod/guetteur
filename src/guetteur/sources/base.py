"""Interface commune des sources de vidéos."""

from __future__ import annotations

from typing import Protocol

from guetteur.models import Video


class SourceError(RuntimeError):
    """Impossible de lire une playlist."""


class VideoSource(Protocol):
    def fetch(self, playlist_id: str) -> list[Video]:
        """Retourne les vidéos de la playlist, les plus récentes en premier."""
        ...


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
