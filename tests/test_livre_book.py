"""Tests unitaires de summarize/book.py et export/livre_output.py (Lot 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from guetteur.export.livre_output import (
    build_glossary,
    render_book_markdown,
    render_index,
    run_pandoc,
    write_book,
)
from guetteur.models import KeyPoint, Summary
from guetteur.summarize.base import SummarizeError
from guetteur.summarize.book import (
    BookPlan,
    ChapterSpec,
    build_chapter_prompt,
    build_plan_prompt,
    chunk_chapter_videos,
    parse_plan,
    render_chapter,
)


def _s(title: str) -> Summary:
    return Summary(
        title=title,
        tldr=f"TL;DR de {title}",
        key_points=(KeyPoint(0, "P1"), KeyPoint(60, "P2")),
        why_it_matters="Pourquoi.",
        reading_time_minutes=1,
    )


# --- parse_plan --------------------------------------------------------------


def test_parse_plan_reads_valid_plan() -> None:
    data = {
        "titre": "Titre livre",
        "introduction": "Intro courte.",
        "chapitres": [
            {
                "titre": "Chapitre 1",
                "fil_conducteur": "Fil 1",
                "video_ids": ["v" * 11, "w" * 11],
            },
            {
                "titre": "Chapitre 2",
                "fil_conducteur": "Fil 2",
                "video_ids": ["x" * 11],
            },
        ],
        "conclusion": "",
    }
    plan = parse_plan(data, [c * 11 for c in "vwx"])
    assert plan.titre == "Titre livre"
    assert len(plan.chapitres) == 2
    assert plan.chapitres[0].video_ids == ("v" * 11, "w" * 11)


def test_parse_plan_missing_videos_are_placed_in_divers() -> None:
    data = {
        "titre": "L",
        "introduction": "",
        "chapitres": [
            {"titre": "C1", "fil_conducteur": "F", "video_ids": ["a" * 11]},
        ],
    }
    plan = parse_plan(data, ["a" * 11, "b" * 11, "c" * 11])
    assert plan.chapitres[-1].titre == "Divers"
    assert set(plan.chapitres[-1].video_ids) == {"b" * 11, "c" * 11}


def test_parse_plan_ignores_unknown_and_duplicate_video_ids() -> None:
    data = {
        "titre": "L",
        "introduction": "",
        "chapitres": [
            {"titre": "C1", "fil_conducteur": "F", "video_ids": ["a" * 11, "a" * 11, "z" * 11]},
            {"titre": "C2", "fil_conducteur": "F", "video_ids": ["a" * 11, "b" * 11]},
        ],
    }
    plan = parse_plan(data, ["a" * 11, "b" * 11])
    # `a` en C1, `b` en C2, doublons droppés, `z` inconnu ignoré.
    assert plan.chapitres[0].video_ids == ("a" * 11,)
    assert plan.chapitres[1].video_ids == ("b" * 11,)


def test_parse_plan_raises_on_empty_chapters() -> None:
    with pytest.raises(SummarizeError, match="chapitres"):
        parse_plan({"titre": "T", "introduction": "", "chapitres": []}, [])


def test_parse_plan_raises_on_missing_title() -> None:
    payload = {
        "introduction": "",
        "chapitres": [{"titre": "C", "fil_conducteur": "", "video_ids": []}],
    }
    with pytest.raises(SummarizeError, match="titre"):
        parse_plan(payload, [])


# --- build prompts -----------------------------------------------------------


def test_build_plan_prompt_lists_all_videos() -> None:
    summaries = [(c * 11, _s(f"Titre {c}")) for c in "abc"]
    prompt = build_plan_prompt("MaChaîne", summaries)
    assert "MaChaîne" in prompt
    for vid, s in summaries:
        assert f'video_id="{vid}"' in prompt
        assert s.title in prompt
        assert s.tldr in prompt


def test_build_chapter_prompt_wraps_resumes_in_tags() -> None:
    chapter = ChapterSpec(titre="C", fil_conducteur="Fil", video_ids=("a" * 11, "b" * 11))
    videos = [
        ("a" * 11, "https://youtu.be/a", _s("A")),
        ("b" * 11, "https://youtu.be/b", _s("B")),
    ]
    prompt = build_chapter_prompt(chapter, videos)
    assert 'video_id="aaaaaaaaaaa"' in prompt
    assert "https://youtu.be/a" in prompt
    # 2 balises <resume …> + une balise ouvrante <resumes> englobante = 3.
    assert prompt.count("<resume ") == 2
    assert "<resumes>" in prompt and "</resumes>" in prompt


# --- chunking ----------------------------------------------------------------


def test_chunk_chapter_videos_splits_when_too_large() -> None:
    # 3 vidéos, chacune ~140 chars de JSON → seuil 300 chars forcera 2 puis 1.
    videos = [(c * 11, f"https://y/{c}", _s(f"Titre {c}")) for c in "abc"]
    lots = chunk_chapter_videos(videos, max_chars=300)
    assert sum(len(lot) for lot in lots) == 3
    assert len(lots) >= 2


def test_render_chapter_appends_pour_aller_plus_loin() -> None:
    ch = ChapterSpec(titre="Ch", fil_conducteur="Fil", video_ids=("a" * 11,))
    parts = ["## Sous 1\n\nCorps riche."]
    videos = [("a" * 11, "https://youtu.be/aaaaaaaaaaa", "Titre A")]
    out = render_chapter(ch, parts, videos)
    assert out.startswith("# Ch")
    assert "> **Pour aller plus loin**" in out
    assert "[Titre A](https://youtu.be/aaaaaaaaaaa)" in out


# --- livre_output ------------------------------------------------------------


def _plan() -> BookPlan:
    return BookPlan(
        titre="Livre",
        introduction="Intro.",
        chapitres=(
            ChapterSpec("Chapitre 1", "Fil 1", ("a" * 11,)),
            ChapterSpec("Chapitre 2", "Fil 2", ("b" * 11,)),
        ),
        conclusion="Conclusion.",
    )


def test_render_book_markdown_has_frontmatter_intro_chapters(tmp_path: Path) -> None:
    plan = _plan()
    chapters = {"Chapitre 1": "# Chapitre 1\n\nCorps 1.", "Chapitre 2": "# Chapitre 2\n\nCorps 2."}
    videos_meta: dict[str, tuple[str, str, str | None]] = {
        "a" * 11: ("Titre A", "https://youtu.be/a", "2026-01-01T00:00:00+00:00"),
        "b" * 11: ("Titre B", "https://youtu.be/b", "2026-06-01T00:00:00+00:00"),
    }
    md = render_book_markdown(plan, chapters, videos_meta, "Chan", "https://y/c")
    assert "guetteur_livre: true" in md
    assert "titre: Livre" in md
    assert "chaine: Chan" in md
    assert "nb_videos: 2" in md
    assert "Introduction" in md
    assert "Corps 1." in md and "Corps 2." in md
    assert "Conclusion" in md and "Conclusion." in md


def test_render_index_lists_chapters_and_dataview() -> None:
    idx = render_index(_plan(), "Chan")
    assert "01-chapitre-1" in idx
    assert "```dataview" in idx


def test_write_book_creates_files(tmp_path: Path) -> None:
    plan = _plan()
    chapters = {"Chapitre 1": "# Chapitre 1\n\nCorps.", "Chapitre 2": "# Chapitre 2\n\nCorps."}
    videos_meta: dict[str, tuple[str, str, str | None]] = {
        "a" * 11: ("A", "u", None),
        "b" * 11: ("B", "u", None),
    }
    livres_dir = tmp_path / "Livres"
    out = write_book(plan, chapters, videos_meta, "Chan", "https://y/c", livres_dir)
    assert out.livre_md.exists()
    assert out.index_md.exists()
    assert len(out.chapter_files) == 2


def test_run_pandoc_calls_binary_and_captures_paths(tmp_path: Path) -> None:
    """Le vrai `pandoc` est mocké : on vérifie l'invocation et la présence
    des drapeaux (TOC, xelatex, fontes DejaVu)."""
    import subprocess

    calls: list[list[str]] = []

    def fake_pandoc(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        out_index = cmd.index("-o")
        Path(cmd[out_index + 1]).write_bytes(b"produit")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    src = tmp_path / "livre.md"
    src.write_text("---\ntitle: X\n---\n# X\ncontenu.\n", encoding="utf-8")
    epub, pdf = run_pandoc(
        src,
        tmp_path / "livre.epub",
        tmp_path / "livre.pdf",
        "Titre",
        pandoc=fake_pandoc,
    )
    assert epub is not None and epub.exists()
    assert pdf is not None and pdf.exists()
    assert len(calls) == 2
    assert calls[0][0] == "pandoc"
    assert "--toc" in calls[0]
    assert "--pdf-engine=xelatex" in calls[1]
    assert any("DejaVu" in arg for arg in calls[1])


def test_run_pandoc_survives_pdf_failure(tmp_path: Path) -> None:
    """L'échec de la génération PDF (pas de LaTeX installé) ne doit pas casser
    la sortie EPUB, qui est le format principal."""
    import subprocess

    def fake(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "--pdf-engine=xelatex" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no xelatex")
        out = cmd[cmd.index("-o") + 1]
        Path(out).write_bytes(b"epub")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    src = tmp_path / "livre.md"
    src.write_text("# X", encoding="utf-8")
    epub, pdf = run_pandoc(src, tmp_path / "livre.epub", tmp_path / "livre.pdf", "T", pandoc=fake)
    assert epub is not None
    assert pdf is None


def test_build_glossary_picks_terms_across_chapters() -> None:
    chapters = {
        "C1": "MCP est un protocole. On voit MCP partout. Anthropic invente MCP.",
        "C2": "MCP tourne bien avec Claude. Le MCP standard...",
        "C3": "Claude adore MCP.",
    }
    glossary = build_glossary(chapters, min_occurrences=3)
    assert "MCP" in glossary
    # La définition est la phrase de première occurrence.
    assert "protocole" in glossary["MCP"]
