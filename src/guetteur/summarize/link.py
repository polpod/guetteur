"""Résumé d'un lien partagé au bot (Lot 8).

Les liens n'ont pas de timeline : les `KeyPoint.seconds` restent à 0. Le prompt
est distinct de celui des vidéos — pas de recherche de citation horodatée, pas
d'inférence d'actions audio ; on reste sur titre, TL;DR 2 phrases, points
clés, « pourquoi c'est intéressant » et liens cités dans le contenu.

Le résumé est obtenu via `raw_call` sur le backend sélectionné (claude_code ou
claude_api) pour éviter d'embarquer la logique vidéo (chunking, découpage
transcript, prompts horodatés). Lien court → un seul appel.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from guetteur.items import LinkContent
from guetteur.models import DetailLevel, KeyPoint, Summary
from guetteur.summarize.base import SummarizeError
from guetteur.summarize.format import reading_time_minutes

log = logging.getLogger(__name__)


class SupportsRawCall(Protocol):
    def raw_call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        timeout_s: float,
    ) -> str: ...


@dataclass(frozen=True)
class LinkSummaryMeta:
    link: LinkContent
    language: str = "fr"
    detail: DetailLevel = "standard"


_SYSTEM_BASE = """\
Tu es GUETTEUR, un assistant de veille qui résume des liens partagés par l'utilisateur \
(tweets, articles, repos GitHub).

On te fournit l'URL, le kind (tweet, article, github), des métadonnées (auteur, date) et le \
contenu brut extrait.

Règles :
- Pas d'horodatage, pas d'inférence sur l'audio ; on résume le texte fourni.
- Pas de fabrication : si une information manque (auteur, date), laisse le champ vide.
- Toujours en français, même si le lien est en anglais.
- Pour un fil Twitter/X, un point par tweet marquant.
- `why_it_matters` dit en une phrase pourquoi le lecteur de GUETTEUR voudrait voir ce lien.
- `tldr` : 2 phrases maximum.
- Les URL présentes dans le contenu peuvent apparaître dans les points clés ou dans \
why_it_matters si pertinentes.
"""

_DETAIL_HINT = {
    "bref": "Niveau bref : 3 points clés maximum, pas de sections, pas de citations.",
    "standard": "Niveau standard : 3 à 6 points clés.",
    "detaille": (
        "Niveau détaillé : 4 à 8 points clés, et jusqu'à 3 citations reformulées depuis le texte."
    ),
}


def system_prompt_for_link(detail: DetailLevel) -> str:
    return _SYSTEM_BASE + "\n" + _DETAIL_HINT.get(detail, _DETAIL_HINT["standard"])


# Schéma strict mais sans timestamp. `key_points` est une liste de strings
# (pas de secondes) ; côté Python on construit des KeyPoint(seconds=0, text=…).
def schema_for_link(detail: DetailLevel) -> dict[str, Any]:
    key_point_max = 3 if detail == "bref" else (6 if detail == "standard" else 8)
    base: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "tldr": {"type": "string"},
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": key_point_max,
            },
            "why_it_matters": {"type": "string"},
            "actions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
        },
        "required": ["title", "tldr", "key_points", "why_it_matters"],
        "additionalProperties": False,
    }
    if detail == "detaille":
        base["properties"]["citations"] = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        }
    return base


def _build_user_prompt(link: LinkContent, language: str) -> str:
    header = [
        f"URL : {link.url}",
        f"Kind : {link.kind}",
        f"Titre d'origine : {link.title or '(sans titre)'}",
        f"Auteur : {link.author or '(inconnu)'}",
        f"Publié le : {link.published_at.isoformat() if link.published_at else '(inconnu)'}",
        f"Langue cible du résumé : {language}",
    ]
    if link.extras:
        extras = ", ".join(f"{k}={v}" for k, v in sorted(link.extras.items()))
        header.append(f"Extras : {extras}")
    header.append("")
    header.append("--- Contenu brut ---")
    header.append(link.text or "(contenu vide)")
    return "\n".join(header)


def summarize_link(backend: SupportsRawCall, meta: LinkSummaryMeta, timeout_s: float) -> Summary:
    """Résume un lien via le backend donné. Lève SummarizeError si la réponse
    JSON est invalide ou vide."""
    system = system_prompt_for_link(meta.detail)
    user = _build_user_prompt(meta.link, meta.language)
    schema = schema_for_link(meta.detail)
    raw = backend.raw_call(system, user, schema, timeout_s)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SummarizeError(f"Réponse Claude non JSON pour un lien : {exc}") from exc
    if not isinstance(data, dict):
        raise SummarizeError(f"Réponse Claude pour un lien n'est pas un objet : {type(data)}")

    title = str(data.get("title") or meta.link.title or meta.link.url).strip()
    tldr = str(data.get("tldr") or "").strip()
    why = str(data.get("why_it_matters") or "").strip()
    kps_raw = data.get("key_points") or []
    if not isinstance(kps_raw, list):
        raise SummarizeError("key_points doit être une liste")
    kps = tuple(KeyPoint(0, str(x).strip()) for x in kps_raw if str(x).strip())
    actions = tuple(str(x).strip() for x in (data.get("actions") or []) if str(x).strip())
    citations_raw = data.get("citations") or []
    from guetteur.models import Citation

    citations = tuple(
        Citation(0, str(x).strip()) for x in citations_raw if str(x).strip()
    )
    summary = Summary(
        title=title[:160],
        tldr=tldr,
        key_points=kps,
        why_it_matters=why,
        reading_time_minutes=0,  # recomputé juste après
        detail=meta.detail,
        sections=(),
        citations=citations,
        actions=actions,
        reserves=(),
    )
    # Durée de lecture estimée à partir du contenu résumé (titre + tldr + points + why).
    minutes = reading_time_minutes(
        title, tldr, why, *(k.text for k in kps), *actions, *(c.text for c in citations)
    )
    return Summary(
        title=summary.title,
        tldr=summary.tldr,
        key_points=summary.key_points,
        why_it_matters=summary.why_it_matters,
        reading_time_minutes=minutes,
        detail=summary.detail,
        sections=summary.sections,
        citations=summary.citations,
        actions=summary.actions,
        reserves=summary.reserves,
    )
