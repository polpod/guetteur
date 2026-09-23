"""Interface et erreurs de transcription."""

from __future__ import annotations

from typing import Protocol

from guetteur.models import Transcript


class TranscriptError(RuntimeError):
    """Échec (potentiellement transitoire) de l'obtention d'une transcription."""


class NoTranscriptError(TranscriptError):
    """Aucune transcription disponible pour cette vidéo."""


class TranscriptProvider(Protocol):
    def get(self, video_id: str) -> Transcript: ...
