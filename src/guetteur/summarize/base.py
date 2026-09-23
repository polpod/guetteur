"""Interface commune des backends de résumé, prompt, schéma et découpage partagés.

Les transcriptions de plus de CHUNK_CHARS caractères sont découpées : chaque morceau est
résumé, puis les résumés partiels sont fusionnés (résumé de résumés)."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from guetteur.models import KeyPoint, Summary, Transcript, Video
from guetteur.summarize.format import reading_time_minutes

log = logging.getLogger(__name__)

CHUNK_CHARS = 150_000
MIN_KEY_POINTS = 5
MAX_KEY_POINTS = 8

SYSTEM_PROMPT = """\
Tu es GUETTEUR, un assistant de veille qui résume des vidéos YouTube pour un lecteur pressé.

On te fournit les métadonnées d'une vidéo et sa transcription. Chaque ligne de la transcription \
commence par son horodatage en secondes, au format [123s].

Produis un résumé fidèle, dense et factuel :
- title : un titre clair et informatif (pas de clickbait), 90 caractères maximum.
- tldr : exactement 2 phrases qui donnent l'essentiel.
- key_points : entre 5 et 8 points clés, dans l'ordre chronologique. Chaque point contient \
"seconds" (entier : l'horodatage en secondes, repris de la transcription, du passage où l'idée \
est développée) et "text" (une phrase autonome et concrète : chiffres, noms, conclusions).
- why_it_matters : 1 phrase expliquant pourquoi ce contenu compte pour le lecteur.

Règles :
- N'invente rien : n'utilise que ce qui figure dans la transcription.
- N'utilise pas de Markdown ni d'emojis dans les champs : texte brut uniquement.
- Rédige dans la langue demandée, quelle que soit la langue de la transcription.
- Les horodatages doivent correspondre à des lignes réelles de la transcription.
- Réponds uniquement avec l'objet JSON demandé, sans texte autour."""

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tldr": {"type": "string"},
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["seconds", "text"],
                "additionalProperties": False,
            },
        },
        "why_it_matters": {"type": "string"},
    },
    "required": ["title", "tldr", "key_points", "why_it_matters"],
    "additionalProperties": False,
}


class SummarizeError(RuntimeError):
    """Échec (a priori transitoire) de génération d'un résumé : la vidéo sera retentée."""


class SummarizerUnavailableError(SummarizeError):
    """Backend inutilisable (binaire absent, session non connectée…). Ce n'est pas la faute de
    la vidéo : le cycle s'arrête sans consommer ses essais."""


@dataclass(frozen=True)
class SummaryMeta:
    video: Video
    language: str


class Summarizer(Protocol):
    def summarize(self, transcript: Transcript, meta: SummaryMeta) -> Summary: ...


def split_transcript(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Découpe sur les fins de ligne pour ne jamais couper un segment horodaté."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        while len(line) > max_chars:  # ligne pathologique : coupe brute
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        if size + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _video_header(meta: SummaryMeta) -> str:
    v = meta.video
    return (
        f"Langue du résumé : {meta.language}\n"
        f"Titre de la vidéo : {v.title}\n"
        f"Chaîne : {v.channel}\n"
        f"URL : {v.url}"
    )


class ChunkedSummarizer(ABC):
    """Orchestration commune ; un backend n'implémente que `_complete`."""

    @abstractmethod
    def _complete(self, instruction: str, document: str) -> dict[str, Any]:
        """Envoie une consigne courte et un document long ; retourne le JSON du résumé."""

    def summarize(self, transcript: Transcript, meta: SummaryMeta) -> Summary:
        body = transcript.to_timestamped_text()
        chunks = split_transcript(body)
        header = _video_header(meta)

        if len(chunks) == 1:
            data = self._complete(
                f"{header}\n\nRésume cette vidéo à partir de sa transcription.",
                f"<transcription>\n{body}\n</transcription>",
            )
            return to_summary(data)

        log.info(
            "summarize.chunked", extra={"video_id": meta.video.video_id, "chunks": len(chunks)}
        )
        partials: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            part = self._complete(
                f"{header}\n\nVoici la partie {i}/{len(chunks)} de la transcription. "
                "Résume uniquement cette partie.",
                f"<transcription>\n{chunk}\n</transcription>",
            )
            partials.append(_partial_as_text(i, part))
        joined = "\n\n".join(partials)
        data = self._complete(
            f"{header}\n\nLa transcription était trop longue : voici les résumés de ses "
            f"{len(chunks)} parties, dans l'ordre. Fusionne-les en un résumé unique de "
            "toute la vidéo, en conservant les horodatages [Ns] d'origine.",
            f"<resumes_partiels>\n{joined}\n</resumes_partiels>",
        )
        return to_summary(data)


def _partial_as_text(index: int, data: dict[str, Any]) -> str:
    lines = [f"## Partie {index} — {data.get('title', '')}", str(data.get("tldr", ""))]
    for kp in data.get("key_points", []):
        lines.append(f"[{int(kp.get('seconds', 0))}s] {kp.get('text', '')}")
    lines.append(str(data.get("why_it_matters", "")))
    return "\n".join(lines)


def to_summary(data: dict[str, Any]) -> Summary:
    try:
        points = tuple(
            KeyPoint(seconds=max(0, int(kp["seconds"])), text=str(kp["text"]).strip())
            for kp in data["key_points"]
        )
        title = str(data["title"]).strip()
        tldr = str(data["tldr"]).strip()
        why = str(data["why_it_matters"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise SummarizeError(f"Résumé mal formé : {exc}") from exc
    if len(points) < MIN_KEY_POINTS:
        log.warning("summarize.few_key_points", extra={"count": len(points)})
    points = points[:MAX_KEY_POINTS]
    return Summary(
        title=title,
        tldr=tldr,
        key_points=points,
        why_it_matters=why,
        reading_time_minutes=reading_time_minutes(title, tldr, why, *(p.text for p in points)),
    )


def summary_to_json(summary: Summary) -> str:
    return json.dumps(
        {
            "title": summary.title,
            "tldr": summary.tldr,
            "key_points": [{"seconds": k.seconds, "text": k.text} for k in summary.key_points],
            "why_it_matters": summary.why_it_matters,
            "reading_time_minutes": summary.reading_time_minutes,
        },
        ensure_ascii=False,
    )


def summary_from_json(raw: str) -> Summary:
    data: dict[str, Any] = json.loads(raw)
    summary = to_summary(data)
    if isinstance(data.get("reading_time_minutes"), int):
        summary = Summary(
            title=summary.title,
            tldr=summary.tldr,
            key_points=summary.key_points,
            why_it_matters=summary.why_it_matters,
            reading_time_minutes=data["reading_time_minutes"],
        )
    return summary
