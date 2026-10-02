"""Interface commune des backends de résumé, prompts, schémas et découpage partagés.

Trois niveaux de détail (Lot 4) :

- « bref » : titre + tldr 2 phrases + 3 points clés + 1 action, ≤ 120 mots.
- « standard » : le résumé historique (5 à 8 points clés, 12 si liste annoncée).
- « detaille » : sections thématiques dans l'ordre, citations reformulées, actions
  à l'impératif, réserves ; 900 à 1500 mots pour 15-30 min de vidéo.

Les transcriptions de plus de CHUNK_CHARS caractères sont découpées : chaque morceau est
résumé, puis les résumés partiels sont fusionnés (résumé de résumés). La fusion utilise
le même prompt que le niveau demandé (sections d'une part, points clés d'autre part)."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from guetteur.models import Citation, DetailLevel, KeyPoint, Section, Summary, Transcript, Video
from guetteur.summarize.format import reading_time_minutes

log = logging.getLogger(__name__)

CHUNK_CHARS = 150_000
MIN_KEY_POINTS = 5
MAX_KEY_POINTS = 8
# Limite relevée quand la vidéo annonce une liste numérotée (« 9 pièges », « top 10 »).
LIST_MAX_KEY_POINTS = 12

# Cibles du mode détaillé.
MIN_SECTIONS = 4
MAX_SECTIONS = 10
MIN_BULLETS_PER_SECTION = 3
MAX_BULLETS_PER_SECTION = 6
MIN_CITATIONS = 2
MAX_CITATIONS = 4
MIN_ACTIONS_DETAILED = 3
MAX_ACTIONS_DETAILED = 6
MIN_RESERVES = 1
MAX_RESERVES = 3

# Cibles du mode bref.
BRIEF_MAX_WORDS = 120
BRIEF_KEY_POINTS = 3
BRIEF_ACTIONS = 1


# --- system prompts ---------------------------------------------------------------------

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
- announced_items : nombre d'éléments de la liste numérotée annoncée par le titre ou la \
transcription (« 9 pièges » → 9, « top 10 » → 10), ou 0 s'il n'y en a pas.

Liste numérotée : quand le titre ou la transcription annonce une liste numérotée (« 9 pièges », \
« 5 étapes », « top 10 »…), couvre chaque élément de la liste, dans l'ordre, avec un point clé \
par élément : n'en saute aucun et n'en fusionne aucun. Dans ce cas, la limite passe de 8 à 12 \
points clés ; au-delà de 12 éléments, regroupe les derniers pour tenir en 12 points. Commence \
chaque point par le numéro et le nom de l'élément tels qu'annoncés.

Règles :
- N'invente rien : n'utilise que ce qui figure dans la transcription.
- N'utilise pas de Markdown ni d'emojis dans les champs : texte brut uniquement.
- Rédige dans la langue demandée, quelle que soit la langue de la transcription.
- Les horodatages doivent correspondre à des lignes réelles de la transcription.
- Réponds uniquement avec l'objet JSON demandé, sans texte autour."""

SYSTEM_PROMPT_BRIEF = """\
Tu es GUETTEUR, un assistant de veille qui résume des vidéos YouTube. Rends le résumé le plus \
compact possible : un lecteur pressé doit tout retenir en 30 secondes.

On te fournit les métadonnées d'une vidéo et sa transcription (chaque ligne commence par son \
horodatage en secondes, au format [123s]).

Produis un résumé bref, factuel, sans remplissage :
- title : titre clair et informatif, 90 caractères maximum, pas de clickbait.
- tldr : exactement 2 phrases qui donnent l'essentiel.
- key_points : exactement 3 points clés, dans l'ordre chronologique. Chaque point contient \
"seconds" (entier repris de la transcription) et "text" (phrase autonome, avec un chiffre, un \
nom ou une conclusion précise).
- actions : exactement 1 action à retenir, formulée à l'impératif, sans "il faut" ni \
"vous devriez".
- announced_items : nombre d'éléments d'une éventuelle liste numérotée (« 9 pièges » → 9), \
0 sinon.

Longueur cible : au total 120 mots maximum sur l'ensemble des champs.

Règles :
- N'invente rien : n'utilise que ce qui figure dans la transcription.
- Pas de Markdown ni d'emojis dans les champs : texte brut uniquement.
- Rédige dans la langue demandée, quelle que soit la langue de la transcription.
- Les horodatages doivent correspondre à des lignes réelles de la transcription.
- Réponds uniquement avec l'objet JSON demandé, sans texte autour."""

SYSTEM_PROMPT_DETAILED = """\
Tu es GUETTEUR, un assistant de veille qui rédige des résumés détaillés de vidéos YouTube pour \
un lecteur qui veut tout retenir sans revisionner.

On te fournit les métadonnées d'une vidéo et sa transcription (chaque ligne commence par son \
horodatage en secondes, au format [123s]).

Produis un résumé fidèle, dense, verifiable :
- title : titre clair et informatif, 90 caractères maximum, pas de clickbait.
- tldr : 3 à 4 phrases qui donnent l'essentiel.
- sections : 4 à 10 sections thématiques suivant l'ordre de la vidéo, chacune avec \
"title" (titre de section, court et parlant), "seconds" (entier : timestamp de début, repris \
d'une ligne réelle) et "bullets" (3 à 6 puces).
  Chaque puce doit apporter une information VÉRIFIABLE dans la transcription : affirmation \
précise, chiffre, nom d'outil, exemple donné, comparaison. Sont interdits : paraphrase vague, \
généralités, formules du type « l'auteur explique que », « il aborde la question de ».
- citations : 2 à 4 passages marquants REFORMULÉS (jamais de verbatim de plus de 15 mots), \
chacun avec "seconds" et "text".
- actions : 3 à 6 choses concrètes à faire ou à retenir, formulées à l'impératif \
(« Vérifie X », « Utilise Y », « Évite Z »), sans « il faut » ni « vous devriez ».
- reserves : 1 à 3 limites ou affirmations discutables relevées dans la vidéo. S'il n'y en \
a aucune, mets exactement une entrée : "aucune".
- announced_items : nombre d'éléments d'une éventuelle liste numérotée (« 9 pièges » → 9), \
0 sinon.

Liste numérotée : quand le titre ou la transcription annonce une liste numérotée (« 9 pièges », \
« 5 étapes », « top 10 »…), consacre une section OU une puce à chaque élément, dans l'ordre. \
N'en saute aucun. Si le total dépasse le plafond (10 sections), regroupe la fin.

Longueur cible : 900 à 1500 mots pour une vidéo de 15 à 30 minutes, proportionnelle au-delà. \
Ne pas gonfler pour atteindre la cible : mieux vaut un peu moins que du remplissage.

Règles :
- N'invente rien : n'utilise que ce qui figure dans la transcription.
- Pas de Markdown ni d'emojis dans les champs : texte brut uniquement.
- Rédige dans la langue demandée, quelle que soit la langue de la transcription.
- Les horodatages doivent correspondre à des lignes réelles de la transcription.
- Sections dans l'ordre chronologique, avec timestamps croissants.
- Réponds uniquement avec l'objet JSON demandé, sans texte autour."""


# --- schémas JSON -----------------------------------------------------------------------


def _kp_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "seconds": {"type": "integer"},
            "text": {"type": "string"},
        },
        "required": ["seconds", "text"],
        "additionalProperties": False,
    }


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tldr": {"type": "string"},
        "key_points": {"type": "array", "items": _kp_schema()},
        "why_it_matters": {"type": "string"},
        "announced_items": {"type": "integer"},
    },
    "required": ["title", "tldr", "key_points", "why_it_matters", "announced_items"],
    "additionalProperties": False,
}

SCHEMA_BRIEF: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tldr": {"type": "string"},
        "key_points": {"type": "array", "items": _kp_schema()},
        "actions": {"type": "array", "items": {"type": "string"}},
        "announced_items": {"type": "integer"},
    },
    "required": ["title", "tldr", "key_points", "actions", "announced_items"],
    "additionalProperties": False,
}

SCHEMA_DETAILED: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tldr": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "seconds": {"type": "integer"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "seconds", "bullets"],
                "additionalProperties": False,
            },
        },
        "citations": {"type": "array", "items": _kp_schema()},
        "actions": {"type": "array", "items": {"type": "string"}},
        "reserves": {"type": "array", "items": {"type": "string"}},
        "announced_items": {"type": "integer"},
    },
    "required": [
        "title",
        "tldr",
        "sections",
        "citations",
        "actions",
        "reserves",
        "announced_items",
    ],
    "additionalProperties": False,
}


def system_prompt_for(detail: DetailLevel) -> str:
    if detail == "bref":
        return SYSTEM_PROMPT_BRIEF
    if detail == "detaille":
        return SYSTEM_PROMPT_DETAILED
    return SYSTEM_PROMPT


def schema_for(detail: DetailLevel) -> dict[str, Any]:
    if detail == "bref":
        return SCHEMA_BRIEF
    if detail == "detaille":
        return SCHEMA_DETAILED
    return SUMMARY_SCHEMA


class SummarizeError(RuntimeError):
    """Échec (a priori transitoire) de génération d'un résumé : la vidéo sera retentée."""


class SummarizerUnavailableError(SummarizeError):
    """Backend inutilisable (binaire absent, session non connectée…). Ce n'est pas la faute de
    la vidéo : le cycle s'arrête sans consommer ses essais."""


@dataclass(frozen=True)
class SummaryMeta:
    video: Video
    language: str
    detail: DetailLevel = "standard"


class Summarizer(Protocol):
    def summarize(self, transcript: Transcript, meta: SummaryMeta) -> Summary: ...

    def raw_call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        timeout_s: float,
    ) -> str:
        """Appel « brut » à Claude utilisé par les passes livre (plan JSON et
        rédaction de chapitre). Même garde-fous que `summarize` côté backend :
        aucun outil, settings utilisateur ignorés, clé API retirée de l'env sur
        le backend claude_code. `json_schema` contraint la sortie quand il est
        fourni (plan) ; `None` laisse le texte libre (chapitre Markdown)."""
        ...


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


# --- détection d'une liste numérotée annoncée -----------------------------------------------

_NUMBER_WORDS = {
    "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
    "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "quinze": 15, "vingt": 20,
    "two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}  # fmt: skip
_LIST_NOUNS = (
    "pièges|étapes|erreurs|conseils|astuces|raisons|choses|outils|façons|manières|règles|"
    "leçons|secrets|mythes|signes|habitudes|questions|idées|techniques|méthodes|principes|"
    "commandements|clés|trucs|applis|applications|logiciels|livres|fonctionnalités|nouveautés|"
    "bonnes pratiques|points|critères|options|alternatives|commandes|scripts|"
    "pitfalls|mistakes|errors|steps|tips|tricks|reasons|things|tools|ways|rules|lessons|"
    "secrets|myths|signs|habits|questions|ideas|techniques|methods|principles|features|apps|"
    "books|commands|best practices|options|alternatives"
)
_NUMBER = rf"(\d{{1,2}}|{'|'.join(_NUMBER_WORDS)})"
# « 9 pièges », « 5 grosses erreurs », « 10 best tips » (jusqu'à 2 mots entre les deux).
_COUNTED_LIST = re.compile(
    rf"\b{_NUMBER}\s+(?:[\w'\u2019-]+\s+){{0,2}}?(?:{_LIST_NOUNS})\b", re.IGNORECASE
)
_TOP_LIST = re.compile(rf"\btop\s*-?\s*{_NUMBER}\b", re.IGNORECASE)
# Une annonce se trouve dans le titre ou dans l'introduction de la vidéo.
_INTRO_CHARS = 5_000
MIN_LIST_ITEMS = 2
MAX_DETECTED_ITEMS = 30


def _to_int(raw: str) -> int:
    return int(raw) if raw.isdigit() else _NUMBER_WORDS[raw.lower()]


def detect_announced_list(title: str, transcript_text: str = "") -> int | None:
    """Nombre d'éléments d'une liste numérotée annoncée (« 9 pièges », « top 10 »), ou None.
    Le titre est prioritaire ; sinon on cherche dans l'introduction de la transcription."""
    for text in (title, transcript_text[:_INTRO_CHARS]):
        for pattern in (_TOP_LIST, _COUNTED_LIST):
            match = pattern.search(text)
            if match:
                count = _to_int(match.group(1))
                if MIN_LIST_ITEMS <= count <= MAX_DETECTED_ITEMS:
                    return count
    return None


def _list_hint(count: int | None, detail: DetailLevel) -> str:
    if count is None:
        return ""
    if detail == "detaille":
        target = min(count, MAX_SECTIONS)
        extra = f" Au-delà de {MAX_SECTIONS}, regroupe la fin." if count > MAX_SECTIONS else ""
        return (
            f"\n\nListe numérotée annoncée : {count} éléments. Consacre une section OU une "
            f"puce à chaque élément, dans l'ordre, sans en sauter ({target} sections cibles), "
            f"et announced_items = {count}.{extra}"
        )
    if detail == "bref":
        # Bref reste à 3 points quel que soit l'énoncé de la liste, mais on rappelle
        # le total au modèle pour qu'il choisisse ses 3 éléments intelligemment.
        return (
            f"\n\nListe numérotée annoncée : {count} éléments. Choisis les 3 plus "
            f"représentatifs pour tenir en 3 points clés, et announced_items = {count}."
        )
    target = min(count, LIST_MAX_KEY_POINTS)
    extra = (
        f" Au-delà de {LIST_MAX_KEY_POINTS}, regroupe les derniers éléments."
        if count > LIST_MAX_KEY_POINTS
        else ""
    )
    return (
        f"\n\nListe numérotée annoncée : {count} éléments. Produis un point clé par élément, "
        f"dans l'ordre, sans en sauter ni en fusionner ({target} points clés), et "
        f"announced_items = {count}.{extra}"
    )


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
    def _complete(self, instruction: str, document: str, detail: DetailLevel) -> dict[str, Any]:
        """Envoie une consigne courte et un document long ; retourne le JSON du résumé.

        Le backend choisit son system prompt et son schéma via `system_prompt_for(detail)` /
        `schema_for(detail)` — les prompts et schémas sont figés côté base pour rester
        alignés avec les tests unitaires."""

    def summarize(self, transcript: Transcript, meta: SummaryMeta) -> Summary:
        body = transcript.to_timestamped_text()
        chunks = split_transcript(body)
        announced = detect_announced_list(meta.video.title, body)
        if announced is not None:
            log.info(
                "summarize.list_detected",
                extra={"video_id": meta.video.video_id, "items": announced},
            )
        header = _video_header(meta) + _list_hint(announced, meta.detail)

        if len(chunks) == 1:
            data = self._complete(
                f"{header}\n\nRésume cette vidéo à partir de sa transcription.",
                f"<transcription>\n{body}\n</transcription>",
                meta.detail,
            )
            return to_summary(data, detail=meta.detail, list_detected=announced is not None)

        log.info(
            "summarize.chunked",
            extra={
                "video_id": meta.video.video_id,
                "chunks": len(chunks),
                "detail": meta.detail,
            },
        )
        partials: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            part = self._complete(
                f"{header}\n\nVoici la partie {i}/{len(chunks)} de la transcription. "
                "Résume uniquement cette partie.",
                f"<transcription>\n{chunk}\n</transcription>",
                meta.detail,
            )
            partials.append(_partial_as_text(i, part, meta.detail))
        joined = "\n\n".join(partials)
        data = self._complete(
            f"{header}\n\nLa transcription était trop longue : voici les résumés de ses "
            f"{len(chunks)} parties, dans l'ordre. Fusionne-les en un résumé unique de "
            "toute la vidéo, en conservant les horodatages [Ns] d'origine.",
            f"<resumes_partiels>\n{joined}\n</resumes_partiels>",
            meta.detail,
        )
        return to_summary(data, detail=meta.detail, list_detected=announced is not None)


def _partial_as_text(index: int, data: dict[str, Any], detail: DetailLevel) -> str:
    """Sérialise un résumé partiel en texte que la passe de fusion peut relire.
    Le format préserve les timestamps [Ns] et distingue les sections en mode détaillé."""
    lines = [f"## Partie {index} — {data.get('title', '')}", str(data.get("tldr", ""))]
    if detail == "detaille":
        for sec in data.get("sections", []) or []:
            lines.append(f"### [{int(sec.get('seconds', 0))}s] {sec.get('title', '')}")
            for bullet in sec.get("bullets", []) or []:
                lines.append(f"- {bullet}")
        for cite in data.get("citations", []) or []:
            lines.append(f"> [{int(cite.get('seconds', 0))}s] {cite.get('text', '')}")
        for action in data.get("actions", []) or []:
            lines.append(f"→ {action}")
        for reserve in data.get("reserves", []) or []:
            lines.append(f"! {reserve}")
        return "\n".join(lines)
    for kp in data.get("key_points", []) or []:
        lines.append(f"[{int(kp.get('seconds', 0))}s] {kp.get('text', '')}")
    if detail == "bref":
        for action in data.get("actions", []) or []:
            lines.append(f"→ {action}")
    else:
        lines.append(str(data.get("why_it_matters", "")))
    return "\n".join(lines)


def max_points_for(data: dict[str, Any], list_detected: bool = False) -> int:
    """8 points clés, ou 12 si une liste numérotée est annoncée (détectée par le code ou
    signalée par le modèle via announced_items)."""
    announced = data.get("announced_items")
    model_saw_list = isinstance(announced, int) and announced >= MIN_LIST_ITEMS
    return LIST_MAX_KEY_POINTS if (list_detected or model_saw_list) else MAX_KEY_POINTS


# --- construction de Summary depuis le JSON ---------------------------------------------


def _clean_strings(items: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(items, list):
        raise SummarizeError(
            f"Champ {field_name!r} doit être une liste (reçu : {type(items).__name__})"
        )
    out: list[str] = []
    for it in items:
        text = str(it).strip()
        if text:
            out.append(text)
    return tuple(out)


def _extract_sections(raw: Any) -> tuple[Section, ...]:
    if not isinstance(raw, list):
        raise SummarizeError("Résumé détaillé : sections doit être une liste")
    result: list[Section] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SummarizeError(f"Section {i} : objet attendu")
        try:
            title = str(item["title"]).strip()
            seconds = max(0, int(item["seconds"]))
            bullets = _clean_strings(item.get("bullets", []), f"sections[{i}].bullets")
        except (KeyError, TypeError, ValueError) as exc:
            raise SummarizeError(f"Section {i} mal formée : {exc}") from exc
        if not title:
            raise SummarizeError(f"Section {i} : titre vide")
        if not bullets:
            raise SummarizeError(f"Section {i} ({title}) : au moins une puce requise")
        result.append(Section(title=title, seconds=seconds, bullets=bullets))
    return tuple(result)


def _extract_citations(raw: Any) -> tuple[Citation, ...]:
    if not isinstance(raw, list):
        raise SummarizeError("Résumé détaillé : citations doit être une liste")
    result: list[Citation] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SummarizeError(f"Citation {i} : objet attendu")
        try:
            seconds = max(0, int(item["seconds"]))
            text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise SummarizeError(f"Citation {i} mal formée : {exc}") from exc
        if text:
            result.append(Citation(seconds=seconds, text=text))
    return tuple(result)


def _texts_for_reading_time(summary: Summary) -> list[str]:
    """Rassemble tous les textes du résumé pour recalculer la durée de lecture."""
    parts = [summary.title, summary.tldr, summary.why_it_matters]
    parts += [k.text for k in summary.key_points]
    parts += list(summary.actions)
    parts += list(summary.reserves)
    for sec in summary.sections:
        parts.append(sec.title)
        parts += list(sec.bullets)
    for cit in summary.citations:
        parts.append(cit.text)
    return [p for p in parts if p]


def to_summary(
    data: dict[str, Any],
    detail: DetailLevel = "standard",
    list_detected: bool = False,
    max_points: int | None = None,
) -> Summary:
    try:
        title = str(data["title"]).strip()
        tldr = str(data["tldr"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise SummarizeError(f"Résumé mal formé (title/tldr) : {exc}") from exc

    if detail == "detaille":
        sections = _extract_sections(data.get("sections", []))
        citations = _extract_citations(data.get("citations", []))
        actions = _clean_strings(data.get("actions", []), "actions")
        reserves = _clean_strings(data.get("reserves", []), "reserves")
        if len(sections) < MIN_SECTIONS:
            log.warning("summarize.few_sections", extra={"count": len(sections)})
        if len(sections) > MAX_SECTIONS:
            log.warning(
                "summarize.sections_truncated",
                extra={"count": len(sections), "max": MAX_SECTIONS},
            )
            sections = sections[:MAX_SECTIONS]
        if len(actions) < MIN_ACTIONS_DETAILED:
            log.warning("summarize.few_actions", extra={"count": len(actions)})
        summary = Summary(
            title=title,
            tldr=tldr,
            key_points=(),  # non utilisé en détaillé
            why_it_matters="",
            reading_time_minutes=1,
            detail="detaille",
            sections=sections,
            citations=citations,
            actions=actions,
            reserves=reserves,
        )
    elif detail == "bref":
        try:
            points = tuple(
                KeyPoint(seconds=max(0, int(kp["seconds"])), text=str(kp["text"]).strip())
                for kp in data["key_points"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SummarizeError(f"Résumé bref mal formé (key_points) : {exc}") from exc
        actions = _clean_strings(data.get("actions", []), "actions")
        if len(points) > BRIEF_KEY_POINTS:
            log.warning(
                "summarize.brief_key_points_truncated",
                extra={"count": len(points), "max": BRIEF_KEY_POINTS},
            )
        summary = Summary(
            title=title,
            tldr=tldr,
            key_points=points[:BRIEF_KEY_POINTS],
            why_it_matters="",
            reading_time_minutes=1,
            detail="bref",
            actions=actions[:BRIEF_ACTIONS] if actions else (),
        )
    else:
        try:
            points = tuple(
                KeyPoint(seconds=max(0, int(kp["seconds"])), text=str(kp["text"]).strip())
                for kp in data["key_points"]
            )
            why = str(data["why_it_matters"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise SummarizeError(f"Résumé mal formé : {exc}") from exc
        if len(points) < MIN_KEY_POINTS:
            log.warning("summarize.few_key_points", extra={"count": len(points)})
        limit = max_points if max_points is not None else max_points_for(data, list_detected)
        if len(points) > limit:
            log.warning(
                "summarize.key_points_truncated", extra={"count": len(points), "max": limit}
            )
        points = points[:limit]
        summary = Summary(
            title=title,
            tldr=tldr,
            key_points=points,
            why_it_matters=why,
            reading_time_minutes=1,
            detail="standard",
        )

    # Durée de lecture recalculée depuis TOUS les textes du résumé, quel que soit le niveau.
    return Summary(
        title=summary.title,
        tldr=summary.tldr,
        key_points=summary.key_points,
        why_it_matters=summary.why_it_matters,
        reading_time_minutes=reading_time_minutes(*_texts_for_reading_time(summary)),
        detail=summary.detail,
        sections=summary.sections,
        citations=summary.citations,
        actions=summary.actions,
        reserves=summary.reserves,
    )


# --- sérialisation ---------------------------------------------------------------------


def summary_to_json(summary: Summary) -> str:
    data: dict[str, Any] = {
        "title": summary.title,
        "tldr": summary.tldr,
        "key_points": [{"seconds": k.seconds, "text": k.text} for k in summary.key_points],
        "why_it_matters": summary.why_it_matters,
        "reading_time_minutes": summary.reading_time_minutes,
        "detail": summary.detail,
    }
    if summary.sections:
        data["sections"] = [
            {"title": s.title, "seconds": s.seconds, "bullets": list(s.bullets)}
            for s in summary.sections
        ]
    if summary.citations:
        data["citations"] = [{"seconds": c.seconds, "text": c.text} for c in summary.citations]
    if summary.actions:
        data["actions"] = list(summary.actions)
    if summary.reserves:
        data["reserves"] = list(summary.reserves)
    return json.dumps(data, ensure_ascii=False)


def summary_from_json(raw: str) -> Summary:
    data: dict[str, Any] = json.loads(raw)
    detail_raw = data.get("detail", "standard")
    valid_levels = ("bref", "standard", "detaille")
    detail: DetailLevel = detail_raw if detail_raw in valid_levels else "standard"
    if detail == "detaille":
        summary = to_summary(data, detail="detaille")
    elif detail == "bref":
        summary = to_summary(data, detail="bref")
    else:
        # Résumé déjà validé à sa création : liste jusqu'à 12 points.
        summary = to_summary(data, detail="standard", max_points=LIST_MAX_KEY_POINTS)
    if isinstance(data.get("reading_time_minutes"), int):
        summary = Summary(
            title=summary.title,
            tldr=summary.tldr,
            key_points=summary.key_points,
            why_it_matters=summary.why_it_matters,
            reading_time_minutes=data["reading_time_minutes"],
            detail=summary.detail,
            sections=summary.sections,
            citations=summary.citations,
            actions=summary.actions,
            reserves=summary.reserves,
        )
    return summary
