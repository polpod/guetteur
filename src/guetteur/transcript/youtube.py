"""Sous-titres YouTube via youtube_transcript_api : langues préférées, puis n'importe laquelle."""

from __future__ import annotations

from collections.abc import Sequence

from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)

from guetteur.models import Segment, Transcript
from guetteur.transcript.base import NoTranscriptError, TranscriptError


class YouTubeCaptions:
    def __init__(self, api: YouTubeTranscriptApi | None = None) -> None:
        self._api = api or YouTubeTranscriptApi()

    def fetch(self, video_id: str, languages: Sequence[str]) -> Transcript:
        try:
            transcripts = self._api.list(video_id)
            try:
                chosen = transcripts.find_transcript(languages)
            except NoTranscriptFound:
                fallback = next(iter(transcripts), None)  # n'importe quelle langue
                if fallback is None:
                    raise NoTranscriptError(f"Aucun sous-titre pour {video_id}") from None
                chosen = fallback
            fetched = chosen.fetch()
        except (TranscriptsDisabled, NoTranscriptFound) as exc:
            raise NoTranscriptError(f"Sous-titres indisponibles pour {video_id}") from exc
        except CouldNotRetrieveTranscript as exc:
            raise TranscriptError(f"Échec de récupération des sous-titres : {exc}") from exc
        segments = tuple(Segment(start=s.start, text=s.text) for s in fetched)
        if not segments:
            raise NoTranscriptError(f"Sous-titres vides pour {video_id}")
        return Transcript(
            video_id=video_id, language=chosen.language_code, source="youtube", segments=segments
        )
