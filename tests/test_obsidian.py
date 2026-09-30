"""Tests unitaires du module export/obsidian.py (Lot 6).

Couvre : slugify, frontmatter YAML, écriture atomique + idempotence via l'index,
déplacement Inbox → thème et Inbox → _ecartes, taxonomie contrainte, parsing des
fiches projet, validation du chemin du vault (section config), append idempotent
dans IDEES.md."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from guetteur.config import ApplicabilityConfig, ObsidianConfig, parse_config
from guetteur.export.obsidian import (
    GUETTEUR_MARKER,
    ObsidianExporter,
    Taxonomy,
    _extract_frontmatter,
    build_frontmatter,
    load_taxonomy,
    parse_project_sheet,
    slugify_title,
)
from guetteur.models import KeyPoint, Summary, Video
from guetteur.store import Store
from tests.helpers import make_config

# --- slugify --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bonjour le monde", "bonjour-le-monde"),
        ("Vidéo à écouter ! ★★★", "video-a-ecouter"),
        ("  espaces   nombreux  ", "espaces-nombreux"),
        ("", "sans-titre"),
    ],
)
def test_slugify_title(title: str, expected: str) -> None:
    assert slugify_title(title) == expected


def test_slugify_title_respects_max_len_and_cuts_on_word_boundary() -> None:
    long = "un-mot-tres-tres-tres-tres-tres-tres-tres-tres-long-encore-plus"
    out = slugify_title(long, max_len=40)
    assert len(out) <= 40
    assert not out.endswith("-")


# --- frontmatter ----------------------------------------------------------------------


def test_build_frontmatter_marks_guetteur_and_lists() -> None:
    v = Video("VID", "Titre", "Chaîne", datetime(2026, 1, 15, tzinfo=UTC), "https://youtu.be/VID")
    front = build_frontmatter(
        v,
        detail="detaille",
        theme="LLM",
        tags=["claude", "context"],
        tags_proposes=["nouveau-mot"],
        projets=["coder", "guetteur"],
        status="inbox",
        archive_notebooklm="nb_1",
    )
    assert GUETTEUR_MARKER in front
    assert "video_id: VID" in front
    assert "date_publication: 2026-01-15" in front
    assert "niveau: detaille" in front
    assert "theme: LLM" in front
    assert "tags: [claude, context]" in front
    assert "nouveau-mot" in front  # quoting YAML autorisé
    assert "projets: [coder, guetteur]" in front
    assert "statut: inbox" in front
    assert "archive_notebooklm: nb_1" in front


def test_extract_frontmatter_parses_and_rejects_absent() -> None:
    text = "---\nnom: X\nstack:\n  - Python\n  - uv\n---\ncorps\n"
    front, body = _extract_frontmatter(text)
    assert front["nom"] == "X"
    assert front["stack"] == ["Python", "uv"]
    assert body.startswith("corps")
    # Sans frontmatter, tout est corps.
    front2, body2 = _extract_frontmatter("juste du corps\n")
    assert front2 == {}
    assert body2.startswith("juste du corps")


# --- taxonomie -----------------------------------------------------------------------


def test_load_taxonomy_parses_sections_and_bullets(tmp_path: Path) -> None:
    path = tmp_path / "tax.md"
    path.write_text(
        "# Ma taxo\n\n## LLM\n- claude\n- context\n\n## Outils\n- uv\n- ruff\n",
        encoding="utf-8",
    )
    tax = load_taxonomy(path)
    assert set(tax.themes) == {"LLM", "Outils"}
    assert tax.allowed_tags("LLM") == {"claude", "context"}


def test_load_taxonomy_returns_empty_when_absent(tmp_path: Path) -> None:
    tax = load_taxonomy(tmp_path / "missing.md")
    assert tax.themes == {}


def test_split_tags_separates_allowed_and_proposed() -> None:
    tax = Taxonomy(themes={"LLM": ("claude", "context")})
    kept, proposed = tax.split_tags("LLM", ["Claude", "prompt", "context"])
    assert kept == ["claude", "context"]
    assert proposed == ["prompt"]


# --- écriture + index ----------------------------------------------------------------


def _config_with_vault(tmp_path: Path) -> Any:
    vault = tmp_path / "vault"
    vault.mkdir()
    obs = ObsidianConfig(enabled=True, path=vault, git_sync=False, git_remote="")
    return make_config(tmp_path, obsidian=obs, applicability=ApplicabilityConfig(enabled=False))


def _sample_video() -> Video:
    return Video(
        "VID_A",
        "Titre de la vidéo",
        "Chaîne",
        datetime(2026, 2, 10, tzinfo=UTC),
        "https://youtu.be/VID_A",
    )


def _sample_summary() -> Summary:
    return Summary(
        title="Résumé standard",
        tldr="Une phrase.",
        key_points=(KeyPoint(0, "P1"), KeyPoint(30, "P2")),
        why_it_matters="Ça compte.",
        reading_time_minutes=1,
    )


def test_ensure_vault_layout_creates_files_and_projects(tmp_path: Path) -> None:
    config = _config_with_vault(tmp_path)
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
    finally:
        store.close()
    vault = config.obsidian.path
    assert (vault / "Veille" / "Inbox").is_dir()
    assert (vault / "Veille" / "_taxonomie.md").exists()
    assert (vault / "Veille" / "_index.md").exists()
    for slug in ("coder", "eagle", "vigie", "console", "guetteur"):
        assert (vault / "Projets" / f"{slug}.md").exists()


def test_export_note_is_idempotent_via_index(tmp_path: Path) -> None:
    config = _config_with_vault(tmp_path)
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        result1 = exporter.export_note(
            video=_sample_video(),
            summary=_sample_summary(),
            detail="standard",
            theme="",
            tags=[],
            tags_proposes=[],
        )
        assert result1.created is True
        # Second export : mêmes video_id → même fichier, pas de doublon.
        result2 = exporter.export_note(
            video=_sample_video(),
            summary=_sample_summary(),
            detail="standard",
            theme="",
            tags=[],
            tags_proposes=[],
        )
        assert result2.path == result1.path
        assert result2.created is False
        assert store.obsidian_note("VID_A") is not None
        # L'index vault a bien enregistré le mapping.
        idx_file = config.obsidian.path / "Veille" / ".guetteur-index.json"
        import json as _json

        idx = _json.loads(idx_file.read_text(encoding="utf-8"))
        assert idx["VID_A"].endswith(".md")
    finally:
        store.close()


def test_move_to_theme_moves_file_and_updates_index(tmp_path: Path) -> None:
    config = _config_with_vault(tmp_path)
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.export_note(
            _sample_video(),
            _sample_summary(),
            "standard",
            "",
            [],
            [],
        )
        target = exporter.move_to_theme("VID_A", "LLM")
        assert target is not None
        assert target.parent.name == "LLM"
        # Le fichier original dans Inbox a disparu.
        assert not (config.obsidian.path / "Veille" / "Inbox" / target.name).exists()
        # Le frontmatter reflète le nouveau statut/thème.
        text = target.read_text(encoding="utf-8")
        assert "statut: garde" in text
        assert "theme: LLM" in text
        # Store à jour.
        note = store.obsidian_note("VID_A")
        assert note is not None
        assert note[1] == "garde" and note[2] == "LLM"
    finally:
        store.close()


def test_move_to_discarded_puts_note_in_ecartes(tmp_path: Path) -> None:
    config = _config_with_vault(tmp_path)
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.export_note(_sample_video(), _sample_summary(), "standard", "", [], [])
        target = exporter.move_to_discarded("VID_A")
        assert target is not None
        assert target.parent.name == "_ecartes"
        note = store.obsidian_note("VID_A")
        assert note is not None and note[1] == "ecarte"
    finally:
        store.close()


def test_append_idea_is_idempotent(tmp_path: Path) -> None:
    config = _config_with_vault(tmp_path)
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        # Écrire la note d'abord (pour que le wiki link fonctionne).
        exporter.export_note(_sample_video(), _sample_summary(), "standard", "", [], [])
        p1 = exporter.append_idea(
            _sample_video(), "coder", 3, "Idée A", "Integrer via foo", "S", "PROMPT"
        )
        assert p1 is not None and p1.name == "IDEES.md"
        # Second append : idempotent, aucune écriture.
        p2 = exporter.append_idea(
            _sample_video(), "coder", 3, "Idée A bis", "autre", "M", "PROMPT bis"
        )
        assert p2 is None
        text = p1.read_text(encoding="utf-8")
        assert "Idée A" in text
        # Le second append n'a rien laissé.
        assert "Idée A bis" not in text
    finally:
        store.close()


# --- parsing des fiches projet -------------------------------------------------------


def test_load_project_sheets_skips_underscore_files_and_filters_statut(tmp_path: Path) -> None:
    """Filtres : `_index.md` sort par le préfixe ; « abandonne » sort par le statut
    (défaut `[applicability] statuts = ["actif", "pause"]`). Seules `actif` et
    `pause` remontent au modèle."""
    config = _config_with_vault(tmp_path)
    projets = config.obsidian.path / "Projets"
    projets.mkdir(parents=True, exist_ok=True)
    (projets / "_index.md").write_text(
        "---\nguetteur: true\nnom: Sommaire\nstatut: actif\n---\ncorps\n", encoding="utf-8"
    )
    (projets / "actif_a.md").write_text(
        "---\nguetteur: true\nnom: A\nstatut: actif\n---\n", encoding="utf-8"
    )
    (projets / "pause_b.md").write_text(
        "---\nguetteur: true\nnom: B\nstatut: pause\n---\n", encoding="utf-8"
    )
    (projets / "abandonne_c.md").write_text(
        "---\nguetteur: true\nnom: C\nstatut: abandonne\n---\n", encoding="utf-8"
    )
    (projets / "sans_marker_d.md").write_text("---\nnom: D\nstatut: actif\n---\n", encoding="utf-8")
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        slugs = {s.slug for s in exporter.load_project_sheets()}
    finally:
        store.close()
    assert slugs == {"actif_a", "pause_b"}


def test_load_project_sheets_honors_configured_statuts(tmp_path: Path) -> None:
    """La liste `statuts` est configurable — un projet marqué `dev` n'est retenu
    que si `dev` figure dans `[applicability] statuts` (config.toml)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    obs = ObsidianConfig(enabled=True, path=vault, git_sync=False, git_remote="")
    config = make_config(
        tmp_path,
        obsidian=obs,
        applicability=ApplicabilityConfig(enabled=False, statuts=("dev", "prod")),
    )
    (vault / "Projets").mkdir()
    (vault / "Projets" / "one.md").write_text(
        "---\nguetteur: true\nnom: One\nstatut: dev\n---\n", encoding="utf-8"
    )
    (vault / "Projets" / "two.md").write_text(
        "---\nguetteur: true\nnom: Two\nstatut: actif\n---\n", encoding="utf-8"
    )
    store = Store(tmp_path / "guetteur.db")
    try:
        slugs = {s.slug for s in ObsidianExporter(config, store).load_project_sheets()}
    finally:
        store.close()
    # `actif` n'est plus dans la liste configurée : `two.md` sort.
    assert slugs == {"one"}


def test_frontmatter_preserves_quoted_commas_via_yaml() -> None:
    """Régression de l'ancien parseur maison : `[a, "b, c", d]` était coupé sur
    chaque virgule → `["a", "b", "c", "d"]`. Avec safe_load, la virgule protégée
    par les guillemets reste dans son élément."""
    text = (
        "---\nnom: X\nstatut: actif\n"
        'recherche: ["MCP", "hooks, sub-agents", "orchestration"]\n---\ncorps\n'
    )
    sheet = parse_project_sheet("x", text)
    assert sheet.recherche == ("MCP", "hooks, sub-agents", "orchestration")


def test_frontmatter_falls_back_to_legacy_on_invalid_yaml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Si le YAML est invalide (indentation cassée, tabs mélangés à des espaces…),
    on retombe sur le parseur maison plutôt que de tout perdre, et on logue un
    warning nommant le fichier pour que l'admin puisse aller le corriger."""
    text = (
        "---\n"
        "nom: X\n"
        "statut: actif\n"
        # ligne cassée volontairement : `foo: {` non fermé — YAMLError garanti.
        "recherche: {non-terminé\n"
        "---\n"
    )
    with caplog.at_level("WARNING"):
        sheet = parse_project_sheet("x", text, source="broken.md")
    # Le repli récupère au moins « nom » et « statut ».
    assert sheet.nom == "X"
    assert sheet.statut == "actif"
    # Le warning cite le fichier pour permettre à l'admin de le corriger.
    assert any(
        rec.name == "guetteur.export.obsidian" and "frontmatter_yaml_invalid" in rec.message
        for rec in caplog.records
    ), [(r.name, r.message) for r in caplog.records]
    assert any(getattr(rec, "file", None) == "broken.md" for rec in caplog.records)


def test_parse_project_sheet_reads_frontmatter() -> None:
    text = (
        "---\nnom: CODER\nstatut: dev\nstack:\n  - Python\n"
        "  - Claude Code\nrecherche:\n  - Claude Code\n  - Hooks\n"
        "exclusions:\n  - Données utilisateur\n---\n\n"
        "Fiche libre.\n"
    )
    sheet = parse_project_sheet("coder", text)
    assert sheet.slug == "coder"
    assert sheet.nom == "CODER"
    assert sheet.stack == ("Python", "Claude Code")
    assert "Claude Code" in sheet.recherche
    assert "Données utilisateur" in sheet.exclusions
    assert "Fiche libre" in sheet.as_context()


# --- validation du chemin du vault (config) ------------------------------------------


def test_config_refuses_relative_vault_path() -> None:
    from guetteur.config import ConfigError

    with pytest.raises(ConfigError, match="absolu"):
        parse_config({"obsidian": {"enabled": True, "path": "relatif/vault"}})


@pytest.mark.parametrize("forbidden", ["/opt/guetteur/data/nlm/sub", "/root/vault", "/etc/vault"])
def test_config_refuses_vault_in_secret_dirs(forbidden: str) -> None:
    from guetteur.config import ConfigError

    with pytest.raises(ConfigError, match="interdit"):
        parse_config({"obsidian": {"enabled": True, "path": forbidden}})
