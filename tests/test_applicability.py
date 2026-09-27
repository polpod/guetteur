"""Tests unitaires du module summarize/applicability.py (Lot 6).

Couvre : le parsing du JSON renvoyé par Claude (avec projets connus + inconnus),
la clamp du score dans [0..3], le drop du méga-prompt quand score < 2, la
sérialisation du prompt utilisateur qui encadre les fiches projet dans une balise
`<projets>` déclarée « données pures » côté prompt système."""

from __future__ import annotations

from pathlib import Path

import pytest

from guetteur.export.obsidian import ProjectSheet, parse_project_sheet
from guetteur.models import KeyPoint, Summary, Video
from guetteur.summarize.applicability import (
    APPLICABILITY_SCHEMA,
    APPLICABILITY_SYSTEM_PROMPT,
    Pertinence,
    _build_user_prompt,
    parse_pertinences,
)


def _project(slug: str, nom: str) -> ProjectSheet:
    return ProjectSheet(slug=slug, nom=nom)


def _summary() -> Summary:
    return Summary(
        title="Titre",
        tldr="Une phrase.",
        key_points=(KeyPoint(0, "P1"),),
        why_it_matters="X.",
        reading_time_minutes=1,
    )


def _video() -> Video:
    return Video("VID", "Titre", "Chaîne", None, "https://youtu.be/VID")


# --- schéma ------------------------------------------------------------------------


def test_schema_declares_all_required_fields() -> None:
    props = APPLICABILITY_SCHEMA["properties"]["pertinences"]["items"]["properties"]
    for k in ("projet", "score", "idee", "integration", "effort", "risques"):
        assert k in props
    assert "prompt_claude_code" in props


def test_system_prompt_marks_projects_as_data_not_instructions() -> None:
    # Sécurité §5 : les fiches projet sont DES DONNÉES, jamais des instructions.
    assert "DONNÉES" in APPLICABILITY_SYSTEM_PROMPT
    assert "Ignore" in APPLICABILITY_SYSTEM_PROMPT
    assert "<projets>" in APPLICABILITY_SYSTEM_PROMPT


def test_build_user_prompt_wraps_projects_in_tag() -> None:
    projects = [_project("coder", "CODER"), _project("guetteur", "GUETTEUR")]
    prompt = _build_user_prompt(_video(), _summary(), projects)
    assert "<projets>" in prompt and "</projets>" in prompt
    # Chaque fiche est marquée avec son slug pour permettre au modèle de choisir.
    assert 'slug="coder"' in prompt and 'slug="guetteur"' in prompt


# --- parsing des résultats ---------------------------------------------------------


def test_parse_pertinences_clamps_score_and_drops_prompt_when_lt_two() -> None:
    projects = [_project("coder", "CODER"), _project("guetteur", "GUETTEUR")]
    data = {
        "pertinences": [
            {
                "projet": "coder",
                "score": 5,
                "idee": "X",
                "integration": "Y",
                "effort": "S",
                "risques": "Z",
                "prompt_claude_code": "PROMPT",
            },
            {
                "projet": "guetteur",
                "score": 1,
                "idee": "Petite piste",
                "integration": "Y",
                "effort": "S",
                "risques": "aucun",
                "prompt_claude_code": "IGNORE-MOI",  # sera droppé (score < 2)
            },
        ]
    }
    results = parse_pertinences(data, projects)
    by_slug = {p.projet: p for p in results}
    assert by_slug["coder"].score == 3  # clamp 5 → 3
    assert by_slug["coder"].prompt_claude_code == "PROMPT"
    assert by_slug["guetteur"].score == 1
    assert by_slug["guetteur"].prompt_claude_code == ""  # droppé sous seuil


def test_parse_pertinences_ignores_unknown_project_slug() -> None:
    projects = [_project("coder", "CODER")]
    data = {
        "pertinences": [
            {
                "projet": "coder",
                "score": 2,
                "idee": "OK",
                "integration": "OK",
                "effort": "S",
                "risques": "aucun",
                "prompt_claude_code": "P",
            },
            {
                "projet": "hallucine",  # projet inexistant
                "score": 3,
                "idee": "faux",
                "integration": "faux",
                "effort": "L",
                "risques": "aucun",
            },
        ]
    }
    results = parse_pertinences(data, projects)
    assert [p.projet for p in results] == ["coder"]


def test_parse_pertinences_fills_missing_projects_with_zero() -> None:
    projects = [_project("coder", "CODER"), _project("guetteur", "G")]
    data = {
        "pertinences": [
            {
                "projet": "coder",
                "score": 1,
                "idee": "x",
                "integration": "y",
                "effort": "S",
                "risques": "",
            }
        ]
    }
    results = parse_pertinences(data, projects)
    slugs = {p.projet: p.score for p in results}
    assert slugs == {"coder": 1, "guetteur": 0}


def test_is_actionable_requires_score_ge_two_and_prompt() -> None:
    p = Pertinence("coder", 2, "i", "i", "S", "", "PROMPT")
    assert p.is_actionable
    p_low = Pertinence("coder", 1, "i", "i", "S", "", "PROMPT")
    assert not p_low.is_actionable
    p_no_prompt = Pertinence("coder", 3, "i", "i", "S", "", "")
    assert not p_no_prompt.is_actionable


# --- lecture d'une fiche projet réelle ---------------------------------------------


def test_parse_project_sheet_extracts_recherche_and_exclusions(tmp_path: Path) -> None:
    text = (
        "---\nnom: TEST\nstatut: dev\nstack:\n  - Python\n"
        "objectifs:\n  - obj1\nrecherche:\n  - agents\nexclusions:\n  - RGPD\n---\n"
        "libre\n"
    )
    sheet = parse_project_sheet("test", text)
    assert sheet.recherche == ("agents",)
    assert sheet.exclusions == ("RGPD",)


@pytest.mark.parametrize("bad_score", ["foo", None, [], {}, "3.5"])
def test_parse_pertinences_defaults_score_to_zero_on_garbage(bad_score: object) -> None:
    projects = [_project("coder", "CODER")]
    data = {
        "pertinences": [
            {
                "projet": "coder",
                "score": bad_score,
                "idee": "x",
                "integration": "y",
                "effort": "S",
                "risques": "",
            }
        ]
    }
    res = parse_pertinences(data, projects)
    assert res[0].score == 0
