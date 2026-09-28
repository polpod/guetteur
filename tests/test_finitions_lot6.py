"""Tests unitaires des finitions Lot 6 (un point par section) :

- §1 : `ObsidianExporter.export_video` fait UN seul commit git (note + idées).
- §2 : `[obsidian] filename_date = "publication" | "traitement"` change le préfixe
       du nom de fichier.
- §3 : `ProjectSheet` accepte `depot` et `modules_cles` ; le prompt d'applicabilité
       les injecte et interdit d'inventer d'autres chemins.
- §4 : `build_message` ajoute une ligne « Pertinent pour : … » cliquable ; la
       commande `/idees <projet>` du bot renvoie les 5 dernières entrées."""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from guetteur.config import (
    ApplicabilityConfig,
    Config,
    ConfigError,
    ObsidianConfig,
    PlaylistConfig,
    parse_config,
)
from guetteur.export.obsidian import ObsidianExporter, parse_project_sheet
from guetteur.models import KeyPoint, Summary, Video
from guetteur.notify.telegram_bot import TelegramBot
from guetteur.pipeline import build_message
from guetteur.store import Store
from guetteur.summarize.applicability import (
    APPLICABILITY_SYSTEM_PROMPT,
    _build_user_prompt,
)
from tests.helpers import make_config
from tests.test_telegram_bot import FakeApi, FakeNotifier, FakeSummarizer

# --- fixtures ---------------------------------------------------------------------------


def _vault(tmp_path: Path, git_remote: str = "", **overrides: Any) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    obs = ObsidianConfig(
        enabled=True,
        path=vault,
        git_sync=bool(git_remote),
        git_remote=git_remote,
        **overrides,
    )
    return make_config(
        tmp_path,
        obsidian=obs,
        applicability=ApplicabilityConfig(enabled=False),
    )


def _video(published: datetime | None) -> Video:
    return Video("VID_1", "Titre", "Chaîne", published, "https://youtu.be/VID_1")


def _summary() -> Summary:
    return Summary(
        title="Résumé",
        tldr="TL;DR.",
        key_points=(KeyPoint(0, "P1"),),
        why_it_matters="X.",
        reading_time_minutes=1,
    )


# --- §1 : un seul commit git regroupé --------------------------------------------------


def _init_bare(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    return remote


def _pertinences_for(
    slugs_scores: list[tuple[str, int]],
) -> list[tuple[str, int, str, str, str, str, str]]:
    return [
        (slug, score, "idée", "intégration", "S", "aucun", "PROMPT " + slug)
        for slug, score in slugs_scores
    ]


def test_export_video_makes_exactly_one_commit_with_ideas_suffix(tmp_path: Path) -> None:
    remote = _init_bare(tmp_path)
    config = _vault(tmp_path, git_remote=str(remote))
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        exporter.export_video(
            video=_video(datetime(2026, 2, 15, tzinfo=UTC)),
            summary=_summary(),
            detail="standard",
            theme="",
            tags=[],
            tags_proposes=[],
            pertinences=_pertinences_for([("coder", 3), ("guetteur", 2), ("eagle", 0)]),
        )
    finally:
        store.close()
    log = subprocess.run(
        ["git", "log", "--pretty=%s", "HEAD"],
        cwd=str(remote),
        capture_output=True,
        text=True,
    )
    guetteur_commits = [c for c in log.stdout.strip().splitlines() if c.startswith("GUETTEUR")]
    # Exactement UN commit pour cette vidéo (note + idées coder + idées guetteur).
    assert len(guetteur_commits) == 1
    # Le suffixe liste les projets score ≥ idea_threshold (par défaut 2).
    assert "(+ idées : coder, guetteur)" in guetteur_commits[0]


def test_export_video_without_ideas_omits_suffix(tmp_path: Path) -> None:
    remote = _init_bare(tmp_path)
    config = _vault(tmp_path, git_remote=str(remote))
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        exporter.export_video(
            video=_video(datetime(2026, 2, 15, tzinfo=UTC)),
            summary=_summary(),
            detail="standard",
            theme="",
            tags=[],
            tags_proposes=[],
            pertinences=_pertinences_for([("coder", 1), ("guetteur", 0)]),  # aucun score ≥ 2
        )
    finally:
        store.close()
    log = subprocess.run(
        ["git", "log", "--pretty=%s", "HEAD"],
        cwd=str(remote),
        capture_output=True,
        text=True,
    )
    commit = log.stdout.strip().splitlines()[0]
    assert commit.startswith("GUETTEUR : Titre")
    assert "idées" not in commit  # pas de suffixe


def test_export_video_second_call_is_idempotent(tmp_path: Path) -> None:
    """Deux appels export_video sur la même vidéo : IDEES.md contient une seule
    entrée (mark_idea_written l'a bloqué)."""
    config = _vault(tmp_path)  # sans git
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        for _ in range(2):
            exporter.export_video(
                video=_video(datetime(2026, 2, 15, tzinfo=UTC)),
                summary=_summary(),
                detail="standard",
                theme="",
                tags=[],
                tags_proposes=[],
                pertinences=_pertinences_for([("coder", 3)]),
            )
        ideas = (config.obsidian.path / "Projets" / "coder" / "IDEES.md").read_text(
            encoding="utf-8"
        )
    finally:
        store.close()
    # Une seule occurrence du prompt (idempotent).
    assert ideas.count("PROMPT coder") == 1


# --- §2 : filename_date ----------------------------------------------------------------


def test_filename_date_publication_uses_video_published(tmp_path: Path) -> None:
    config = _vault(tmp_path, filename_date="publication")
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        exporter.export_note(
            video=_video(datetime(2024, 1, 15, tzinfo=UTC)),
            summary=_summary(),
            detail="standard",
            theme="",
            tags=[],
            tags_proposes=[],
        )
        inbox = config.obsidian.path / "Veille" / "Inbox"
        notes = list(inbox.glob("*.md"))
        assert len(notes) == 1
        assert notes[0].name.startswith("2024-01-15 - ")
    finally:
        store.close()


def test_filename_date_traitement_uses_today(tmp_path: Path) -> None:
    config = _vault(tmp_path, filename_date="traitement")
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        exporter.export_note(
            video=_video(datetime(2024, 1, 15, tzinfo=UTC)),  # date d'il y a longtemps
            summary=_summary(),
            detail="standard",
            theme="",
            tags=[],
            tags_proposes=[],
        )
        inbox = config.obsidian.path / "Veille" / "Inbox"
        notes = list(inbox.glob("*.md"))
        assert len(notes) == 1
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert notes[0].name.startswith(f"{today} - ")
    finally:
        store.close()


def test_filename_date_falls_back_to_today_when_published_missing(tmp_path: Path) -> None:
    """Sans date de publication, on utilise aujourd'hui même en mode « publication »."""
    config = _vault(tmp_path, filename_date="publication")
    store = Store(tmp_path / "guetteur.db")
    try:
        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        exporter.export_note(
            video=_video(published=None),
            summary=_summary(),
            detail="standard",
            theme="",
            tags=[],
            tags_proposes=[],
        )
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert list((config.obsidian.path / "Veille" / "Inbox").glob(f"{today} - *.md"))
    finally:
        store.close()


def test_config_refuses_invalid_filename_date() -> None:
    with pytest.raises(ConfigError, match="filename_date"):
        parse_config({"obsidian": {"enabled": True, "path": "/tmp/vault", "filename_date": "wtf"}})


# --- §3 : depot + modules_cles ----------------------------------------------------------


def test_project_sheet_parses_depot_and_modules_cles() -> None:
    text = (
        "---\nnom: CODER\nstatut: dev\n"
        "depot: git@github.com:polpod/coder.git\n"
        "modules_cles:\n"
        "  - src/coder/orchestrator.py : boucle principale\n"
        "  - src/coder/tools.py : registre des outils\n"
        "---\n\ncorps\n"
    )
    sheet = parse_project_sheet("coder", text)
    assert sheet.depot == "git@github.com:polpod/coder.git"
    assert sheet.modules_cles == (
        "src/coder/orchestrator.py : boucle principale",
        "src/coder/tools.py : registre des outils",
    )
    ctx = sheet.as_context()
    assert "Dépôt : git@github.com:polpod/coder.git" in ctx
    assert "orchestrator.py : boucle principale" in ctx


def test_applicability_system_prompt_forbids_inventing_paths() -> None:
    prompt = APPLICABILITY_SYSTEM_PROMPT
    # Le prompt système mentionne explicitement `modules_cles` et interdit l'invention.
    assert "modules_cles" in prompt
    assert "N'invente" in prompt
    assert "Fichiers probables à toucher" in prompt


def test_user_prompt_includes_depot_and_modules() -> None:
    from guetteur.export.obsidian import ProjectSheet

    sheet = ProjectSheet(
        slug="coder",
        nom="CODER",
        depot="git@github.com:polpod/coder.git",
        modules_cles=("src/coder/orchestrator.py : boucle",),
    )
    video = _video(datetime(2026, 2, 1, tzinfo=UTC))
    prompt = _build_user_prompt(video, _summary(), [sheet])
    assert "git@github.com:polpod/coder.git" in prompt
    assert "src/coder/orchestrator.py" in prompt


# --- §4 : ligne « Pertinent pour » + /idees --------------------------------------------


def test_build_message_adds_pertinent_line_when_pertinences_given() -> None:
    video = _video(datetime(2026, 3, 1, tzinfo=UTC))
    msg = build_message(_summary(), video, "Veille", pertinences=[("coder", 3), ("eagle", 2)])
    # Plain : lisible tel quel.
    assert "Pertinent pour : CODER (3), EAGLE (2)" in msg.plain
    # MarkdownV2 : slugs en gras et parenthèses échappées.
    assert "*CODER*" in msg.markdown_v2 and "\\(3\\)" in msg.markdown_v2
    assert "*EAGLE*" in msg.markdown_v2


def test_build_message_no_pertinent_line_when_empty() -> None:
    msg = build_message(_summary(), _video(None), "Veille", pertinences=None)
    assert "Pertinent pour" not in msg.plain
    assert "Pertinent pour" not in msg.markdown_v2


def _bot_with_vault(tmp_path: Path, ideas_content: str | None) -> tuple[TelegramBot, FakeApi]:
    config = _vault(tmp_path)
    exporter_store = Store(tmp_path / "guetteur.db")
    if ideas_content is not None:
        ideas_dir = config.obsidian.path / "Projets" / "coder"
        ideas_dir.mkdir(parents=True)
        (ideas_dir / "IDEES.md").write_text(ideas_content, encoding="utf-8")
    api = FakeApi()
    bot = TelegramBot(
        config=config,
        store=exporter_store,
        summarizer=FakeSummarizer(),
        question_answerer=lambda t, q, h: "",
        claude_lock=threading.Lock(),
        notifier=FakeNotifier(api),  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        get_playlist=lambda pid: PlaylistConfig(id=pid or "PL", label="V"),
    )
    return bot, api


def test_cmd_ideas_returns_last_five_entries(tmp_path: Path) -> None:
    # 7 entrées datées, on attend les 5 dernières.
    entries = []
    for i in range(7):
        entries.append(f"## 2026-01-{i + 1:02d} — [[note{i}]]\ncontenu {i}\n")
    content = "# Idées\n\n" + "\n".join(entries)
    bot, api = _bot_with_vault(tmp_path, content)
    bot.handle_update({"message": {"chat": {"id": 42}, "text": "/idees coder"}})
    text = "\n".join(m["text"] for m in api.messages_sent)
    # Les 5 derniers (indices 2..6) sont présents ; les 2 premiers ne sont pas.
    for i in range(2, 7):
        assert f"contenu {i}" in text
    assert "contenu 0" not in text
    assert "contenu 1" not in text


def test_cmd_ideas_reports_missing_file(tmp_path: Path) -> None:
    bot, api = _bot_with_vault(tmp_path, ideas_content=None)
    bot.handle_update({"message": {"chat": {"id": 42}, "text": "/idees coder"}})
    assert any("Aucune idée" in m["text"] for m in api.messages_sent)


def test_cmd_ideas_requires_project_arg(tmp_path: Path) -> None:
    bot, api = _bot_with_vault(tmp_path, ideas_content="# Idées\n")
    bot.handle_update({"message": {"chat": {"id": 42}, "text": "/idees"}})
    assert any("Usage" in m["text"] for m in api.messages_sent)
