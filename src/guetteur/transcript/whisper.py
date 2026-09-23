"""Secours : audio m4a via yt-dlp puis transcription locale faster-whisper (CPU).

faster-whisper est une dépendance optionnelle (`uv sync --extra whisper`)."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from guetteur.models import Segment, Transcript
from guetteur.transcript.base import TranscriptError

log = logging.getLogger(__name__)


def download_audio(video_id: str, dest_dir: Path) -> Path:
    import yt_dlp

    opts: dict[str, Any] = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
        ],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception as exc:  # yt-dlp lève des types d'erreur variés
        raise TranscriptError(f"yt-dlp : téléchargement audio impossible : {exc}") from exc
    files = sorted(dest_dir.glob(f"{video_id}.*"))
    if not files:
        raise TranscriptError(f"yt-dlp : aucun fichier audio produit pour {video_id}")
    return files[0]


class WhisperTranscriber:
    def __init__(self, model_size: str = "small") -> None:
        self._model_size = model_size
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptError(
                    "faster-whisper non installé : `uv sync --extra whisper`"
                ) from exc
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, video_id: str) -> Transcript:
        model = self._load()
        with tempfile.TemporaryDirectory(prefix="guetteur-") as tmp:
            audio = download_audio(video_id, Path(tmp))
            log.info("whisper.start", extra={"video_id": video_id, "model": self._model_size})
            try:
                raw_segments, info = model.transcribe(str(audio), vad_filter=True)
                segments = tuple(
                    Segment(start=float(s.start), text=str(s.text)) for s in raw_segments
                )
            except Exception as exc:
                raise TranscriptError(f"faster-whisper : échec : {exc}") from exc
        if not segments:
            raise TranscriptError(f"faster-whisper : transcription vide pour {video_id}")
        return Transcript(
            video_id=video_id, language=str(info.language), source="whisper", segments=segments
        )
