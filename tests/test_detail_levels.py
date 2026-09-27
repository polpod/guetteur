"""Tests des trois niveaux de détail du Lot 4 : schéma, validation, rendu et découpage.

Ces tests couvrent :
- La validation des payloads bref/standard/detaille par `to_summary`.
- Les listes annoncées respectées en mode detaille (fixture 9 items → 9 sections).
- Le découpage MarkdownV2 aux frontières de section avec numérotation « (i/N) ».
- La numérotation, le rendu, et la sérialisation JSON."""

from __future__ import annotations

from typing import Any

import pytest

from guetteur.models import DetailLevel, Video
from guetteur.summarize.base import (
    BRIEF_MAX_WORDS,
    MAX_SECTIONS,
    ChunkedSummarizer,
    SummarizeError,
    SummaryMeta,
    schema_for,
    summary_from_json,
    summary_to_json,
    system_prompt_for,
    to_summary,
)
from guetteur.summarize.format import (
    TELEGRAM_LIMIT,
    numbered,
    render_markdown_v2,
    render_plain,
    split_markdown_v2_parts,
    split_plain_parts,
)

# --- fixtures ---------------------------------------------------------------------------


def _video() -> Video:
    return Video("Q3VqYvsFo84", "Vidéo test", "Chaîne", None, "https://youtu.be/Q3VqYvsFo84")


def _detailed_payload(n_sections: int = 4) -> dict[str, Any]:
    sections = [
        {
            "title": f"Section {i + 1}",
            "seconds": (i + 1) * 60,
            "bullets": [
                f"Fait précis {i + 1}.a — 42 %",
                f"Nom d'outil {i + 1}.b : uv",
                f"Exemple {i + 1}.c : cas concret",
            ],
        }
        for i in range(n_sections)
    ]
    return {
        "title": "Titre détaillé",
        "tldr": "Phrase 1. Phrase 2. Phrase 3.",
        "sections": sections,
        "citations": [
            {"seconds": 30, "text": "Une reformulation courte du passage clé."},
            {"seconds": 240, "text": "Une seconde citation reformulée."},
        ],
        "actions": [
            "Utilise uv pour verrouiller les versions.",
            "Vérifie la couverture des tests avant de merger.",
            "Évite les commits directs sur main.",
        ],
        "reserves": ["L'auteur n'a pas justifié sa comparaison de bench."],
        "announced_items": 0,
    }


def _standard_payload() -> dict[str, Any]:
    return {
        "title": "Titre",
        "tldr": "Une phrase. Deux phrases.",
        "key_points": [{"seconds": i * 60, "text": f"Point {i}"} for i in range(6)],
        "why_it_matters": "Ça compte parce que.",
        "announced_items": 0,
    }


def _brief_payload() -> dict[str, Any]:
    return {
        "title": "Titre",
        "tldr": "Phrase A. Phrase B.",
        "key_points": [{"seconds": i * 30, "text": f"P{i}"} for i in range(3)],
        "actions": ["Fais ceci."],
        "announced_items": 0,
    }


# --- schémas ----------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["bref", "standard", "detaille"])
def test_system_prompt_and_schema_selectable_per_level(level: DetailLevel) -> None:
    prompt = system_prompt_for(level)
    schema = schema_for(level)
    assert isinstance(prompt, str) and prompt
    assert schema["type"] == "object" and "properties" in schema
    if level == "detaille":
        assert "sections" in schema["properties"]
        assert "citations" in schema["properties"]
        assert "reserves" in schema["properties"]
    if level == "bref":
        assert "actions" in schema["properties"]
        assert "why_it_matters" not in schema["properties"]
    if level == "standard":
        assert "key_points" in schema["properties"]
        assert "why_it_matters" in schema["properties"]


def test_detailed_prompt_names_the_forbidden_paraphrases() -> None:
    """Le prompt détaillé doit explicitement interdire les formules vagues."""
    prompt = system_prompt_for("detaille")
    assert "paraphrase vague" in prompt
    assert "l'auteur explique que" in prompt


def test_brief_prompt_gives_budget() -> None:
    prompt = system_prompt_for("bref")
    assert str(BRIEF_MAX_WORDS) in prompt


# --- validation to_summary --------------------------------------------------------------


def test_to_summary_detaille_extracts_sections_citations_actions_reserves() -> None:
    summary = to_summary(_detailed_payload(4), detail="detaille")
    assert summary.detail == "detaille"
    assert len(summary.sections) == 4
    assert summary.sections[0].title == "Section 1"
    assert summary.sections[0].seconds == 60
    assert len(summary.sections[0].bullets) == 3
    assert len(summary.citations) == 2
    assert summary.actions[0].startswith("Utilise")
    assert summary.reserves == ("L'auteur n'a pas justifié sa comparaison de bench.",)
    # Champs de compat vides.
    assert summary.key_points == ()
    assert summary.why_it_matters == ""


def test_to_summary_bref_keeps_only_three_points_and_one_action() -> None:
    payload = _brief_payload()
    payload["key_points"] = [{"seconds": i, "text": f"P{i}"} for i in range(10)]  # trop
    payload["actions"] = ["A", "B", "C"]  # trop
    summary = to_summary(payload, detail="bref")
    assert summary.detail == "bref"
    assert len(summary.key_points) == 3
    assert len(summary.actions) == 1
    assert summary.why_it_matters == ""


def test_to_summary_standard_unchanged_behaviour() -> None:
    summary = to_summary(_standard_payload(), detail="standard")
    assert summary.detail == "standard"
    assert len(summary.key_points) == 6
    assert summary.why_it_matters == "Ça compte parce que."
    assert summary.sections == ()


def test_to_summary_detaille_rejects_empty_bullets() -> None:
    payload = _detailed_payload(4)
    payload["sections"][0]["bullets"] = []
    with pytest.raises(SummarizeError, match="au moins une puce"):
        to_summary(payload, detail="detaille")


def test_to_summary_detaille_truncates_beyond_max_sections() -> None:
    payload = _detailed_payload(MAX_SECTIONS + 3)
    summary = to_summary(payload, detail="detaille")
    assert len(summary.sections) == MAX_SECTIONS


def test_reading_time_recomputed_for_detailed() -> None:
    """La durée de lecture inclut TOUS les textes du résumé (sections, citations…)."""
    summary_short = to_summary(_detailed_payload(1), detail="detaille")
    summary_long = to_summary(_detailed_payload(8), detail="detaille")
    assert summary_long.reading_time_minutes >= summary_short.reading_time_minutes


# --- sérialisation JSON ----------------------------------------------------------------


@pytest.mark.parametrize("level", ["bref", "standard", "detaille"])
def test_summary_json_roundtrip_preserves_detail(level: DetailLevel) -> None:
    payload = {
        "bref": _brief_payload(),
        "standard": _standard_payload(),
        "detaille": _detailed_payload(4),
    }[level]
    original = to_summary(payload, detail=level)
    restored = summary_from_json(summary_to_json(original))
    assert restored.detail == level
    assert restored.title == original.title
    assert restored.tldr == original.tldr
    assert len(restored.sections) == len(original.sections)
    assert len(restored.citations) == len(original.citations)
    assert restored.actions == original.actions
    assert restored.reserves == original.reserves


def test_summary_from_json_defaults_to_standard_for_legacy_payload() -> None:
    """Un résumé du Lot 1/2/3 (sans champ « detail ») est relu comme « standard »."""
    legacy = summary_to_json(to_summary(_standard_payload(), detail="standard"))
    # simule un lot antérieur en retirant le champ detail
    import json as _json

    data = _json.loads(legacy)
    del data["detail"]
    restored = summary_from_json(_json.dumps(data))
    assert restored.detail == "standard"


# --- liste annoncée respectée en detaille ----------------------------------------------


class _EchoSummarizer(ChunkedSummarizer):
    """Retourne un payload figé quel que soit l'appel. Utilisé pour vérifier que la
    consigne d'orchestration mentionne bien N éléments quand une liste est annoncée."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.detail_seen: list[str] = []
        self.instructions: list[str] = []

    def _complete(self, instruction: str, document: str, detail: DetailLevel) -> dict[str, Any]:
        self.instructions.append(instruction)
        self.detail_seen.append(detail)
        return self.payload


def test_announced_list_9_items_produces_9_sections_in_detaille() -> None:
    """Fixture « 9 pièges » : le prompt doit demander 9 sections, et le résumé
    détaillé conserver les 9 items du modèle."""
    from guetteur.models import Segment, Transcript

    payload = _detailed_payload(9)
    summarizer = _EchoSummarizer(payload)
    transcript = Transcript(
        "X",
        "fr",
        "youtube",
        (Segment(0, "Voici les 9 pièges à éviter en Python."),),
    )
    video = Video("X", "9 pièges à éviter en Python", "C", None, "https://youtu.be/X")
    summary = summarizer.summarize(transcript, SummaryMeta(video, "fr", "detaille"))
    # 9 est < MAX_SECTIONS (10) : rien n'est tronqué.
    assert len(summary.sections) == 9
    # Le prompt d'orchestration a bien mentionné la liste.
    assert any("9 éléments" in ins for ins in summarizer.instructions)
    # Le detail est bien propagé au backend.
    assert summarizer.detail_seen == ["detaille"]


# --- rendu MarkdownV2 -------------------------------------------------------------------


def test_render_markdown_v2_detailed_has_section_headers_and_timestamps() -> None:
    summary = to_summary(_detailed_payload(4), detail="detaille")
    rendered = render_markdown_v2(summary, _video(), "Veille")
    # Titre de section en gras (Markdown *…*), avec timestamp cliquable.
    assert "*Section 1*" in rendered
    assert "1:00" in rendered  # 60 s → "1:00"
    assert "https://youtu.be/Q3VqYvsFo84?t=60" in rendered
    # Sections d'ordre chronologique croissant.
    positions = [
        rendered.index("*Section 1*"),
        rendered.index("*Section 2*"),
        rendered.index("*Section 3*"),
        rendered.index("*Section 4*"),
    ]
    assert positions == sorted(positions)


def test_render_plain_detailed_lists_sections_and_actions() -> None:
    summary = to_summary(_detailed_payload(4), detail="detaille")
    rendered = render_plain(summary, _video(), "Veille")
    assert "Section 1 — 1:00" in rendered
    assert "À faire :" in rendered
    assert "Réserves :" in rendered
    # Timestamps croissants.
    for i in range(1, 4):
        earlier = rendered.index(f"Section {i} —")
        later = rendered.index(f"Section {i + 1} —")
        assert earlier < later


# --- découpage aux frontières de section -----------------------------------------------


def test_split_markdown_v2_never_splits_mid_bullet() -> None:
    """Une section très grosse doit rester d'un bloc — jamais coupée au milieu d'une puce."""
    # Générer un résumé qui dépasse 4096 chars : 9 sections avec 6 puces longues.
    payload = _detailed_payload(9)
    for sec in payload["sections"]:
        sec["bullets"] = [f"Puce longue {j} — {'x' * 200}" for j in range(6)]
    summary = to_summary(payload, detail="detaille")
    rendered = render_markdown_v2(summary, _video(), "Veille")
    parts = split_markdown_v2_parts(rendered, TELEGRAM_LIMIT)
    assert len(parts) >= 2
    for part in parts:
        # Aucune puce n'est coupée : chaque "• " ouvre une puce complète (finit par un \n
        # ou par la fin du bloc).
        assert not part.endswith("• "), "Puce coupée en fin de part"
        # Chaque part ≤ limite (le préfixe (i/N) sera ajouté ensuite).
        assert len(part) <= TELEGRAM_LIMIT


def test_split_markdown_v2_short_stays_single_part() -> None:
    """Un rendu qui tient dans la limite ne doit produire qu'une seule part."""
    summary = to_summary(_detailed_payload(2), detail="detaille")
    rendered = render_markdown_v2(summary, _video(), "Veille")
    parts = split_markdown_v2_parts(rendered, TELEGRAM_LIMIT)
    assert len(parts) == 1


def test_numbered_prefixes_when_multiple_parts() -> None:
    parts = ["Alpha", "Beta", "Gamma"]
    numbered_out = numbered(parts, escape=False)
    assert numbered_out[0].startswith("(1/3) ")
    assert numbered_out[1].startswith("(2/3) ")
    assert numbered_out[2].startswith("(3/3) ")


def test_numbered_leaves_single_part_alone() -> None:
    assert numbered(["seul"], escape=False) == ["seul"]


def test_numbered_escapes_markdown_v2() -> None:
    """La numérotation Telegram échappe les caractères sensibles (`(`, `)`)."""
    parts = ["A", "B"]
    out = numbered(parts, escape=True)
    # `(`, `)` sont dans _MDV2_SPECIAL ; `/` ne l'est pas et reste tel quel.
    assert out[0].startswith("\\(1/2\\) ")
    assert out[1].startswith("\\(2/2\\) ")


def test_split_plain_parts_respects_double_newlines() -> None:
    """En texte brut on coupe sur les doubles retours (frontières de section)."""
    text = "\n\n".join([f"Section {i}\n{'x' * 800}" for i in range(10)])
    parts = split_plain_parts(text, 2000)
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= 2000
