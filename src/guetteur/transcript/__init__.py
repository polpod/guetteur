"""Transcription : sous-titres YouTube (fr, en, puis toute langue), secours yt-dlp + whisper."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from guetteur.models import Transcript
from guetteur.transcript.base import NoTranscriptError, TranscriptError, TranscriptProvider
from guetteur.transcript.whisper import WhisperTranscriber
from guetteur.transcript.youtube import YouTubeCaptions

log = logging.getLogger(__name__)

__all__ = [
    "NoTranscriptError",
    "Transcriber",
    "TranscriptError",
    "TranscriptProvider",
]


class Transcriber:
    def __init__(
        self,
        languages: Sequence[str] = ("fr", "en"),
        whisper: WhisperTranscriber | None = None,
        captions: YouTubeCaptions | None = None,
    ) -> None:
        self._languages = tuple(languages)
        self._captions = captions or YouTubeCaptions()
        self._whisper = whisper

    def get(self, video_id: str) -> Transcript:
        try:
            return self._captions.fetch(video_id, self._languages)
        except TranscriptError as exc:
            if self._whisper is None:
                # Sous-titres absents (NoTranscriptError) ou erreur transitoire (TranscriptError,
                # ex. IP bloquée) : le type est conservé pour que le pipeline les distingue.
                raise type(exc)(f"{exc} (whisper désactivé)") from exc
            log.info(
                "transcript.fallback_whisper", extra={"video_id": video_id, "reason": str(exc)}
            )
        return self._whisper.transcribe(video_id)
