"""E2E flow Obsidian complet (Lot 6) : vault temporaire, dépôt git bare comme remote,
pipeline mocké, bot Telegram mocké, applicabilité mockée.

Scénario :
1. Une vidéo est traitée par le pipeline (résumé standard mocké).
2. Une note est créée dans Veille/Inbox/ avec frontmatter complet et guetteur: true.
3. Le commit local est fait + push vers un dépôt bare local.
4. Le fake bot Telegram déclenche « Garder » → note déplacée dans Veille/<thème>/.
5. Une question posée est ajoutée dans la section « ## Questions » de la note.
6. La fiche projet coder reçoit score 3 → ligne dans Projets/coder/IDEES.md et
   bouton « Idée pour CODER » qui envoie le méga-prompt en texte brut."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

from guetteur.config import (
    ApplicabilityConfig,
    ObsidianConfig,
    PlaylistConfig,
)
from guetteur.export.obsidian import ObsidianExporter
from guetteur.models import KeyPoint, Summary
from guetteur.notify.telegram_bot import TelegramBot, build_summary_keyboard
from guetteur.summarize.applicability import Pertinence
from tests.e2e.world import NEW, World
from tests.helpers import make_config
from tests.test_telegram_bot import FakeApi, FakeNotifier, FakeSummarizer


def _init_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    return remote


def _vault_config(tmp_path: Path, remote_url: str = "") -> Any:
    vault = tmp_path / "vault"
    vault.mkdir()
    obs = ObsidianConfig(enabled=True, path=vault, git_sync=bool(remote_url), git_remote=remote_url)
    # Applicability activée mais on la mocke via un stub inline pour l'e2e.
    app = ApplicabilityConfig(enabled=True)
    return make_config(
        tmp_path,
        obsidian=obs,
        applicability=app,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille", detail="standard"),),
    )


def _stub_summary_from_world() -> Summary:
    return Summary(
        title="Résumé standard",
        tldr="Une phrase. Deux.",
        key_points=(KeyPoint(0, "P1"), KeyPoint(30, "P2")),
        why_it_matters="Ça compte.",
        reading_time_minutes=1,
    )


def test_obsidian_flow_pipeline_then_keep_then_question_then_idea(tmp_path: Path) -> None:
    remote = _init_bare_remote(tmp_path)
    config = _vault_config(tmp_path, remote_url=str(remote))
    world = World(config, "claude_api")

    # === 1. Pipeline : la vidéo est traitée. On mocke Claude (summarize et applicability).
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    call_log: list[str] = []

    def create(**kwargs: Any) -> SimpleNamespace:
        # Ce mock est appelé pour SUMMARIZE ; l'applicabilité est mockée à part.
        call_log.append("summary")
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "title": "Résumé standard",
                            "tldr": "Une phrase. Deux.",
                            "key_points": [
                                {"seconds": 0, "text": "P1"},
                                {"seconds": 30, "text": "P2"},
                            ],
                            "why_it_matters": "Ça compte.",
                            "announced_items": 0,
                        }
                    ),
                )
            ],
        )

    world.claude.messages.create = MagicMock(side_effect=create)

    # Applicabilité : coder = 3, guetteur = 0.
    def fake_evaluate(video: Any, summary: Any, projects: Any) -> list[Pertinence]:
        return [
            Pertinence(
                projet="coder",
                score=3,
                idee="Utiliser XYZ pour Y",
                integration="Ajouter un handler dans src/coder/xyz.py",
                effort="S",
                risques="aucun",
                prompt_claude_code="PROMPT CODER",
            ),
            Pertinence("guetteur", 0, "", "", "", "", ""),
        ]

    with patch("guetteur.summarize.applicability.build_evaluator_from_summarizer") as mock_build:
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate.side_effect = fake_evaluate
        mock_build.return_value = mock_evaluator
        world.pipeline.run_cycle()  # init playlist
        world.feed.append(NEW)
        stats = world.pipeline.run_cycle()
    assert stats.sent == 1

    # === 2. La note est dans Veille/Inbox/ avec frontmatter guetteur: true.
    inbox = config.obsidian.path / "Veille" / "Inbox"
    notes = list(inbox.glob("*.md"))
    assert len(notes) == 1
    note_path = notes[0]
    text = note_path.read_text(encoding="utf-8")
    assert "guetteur: true" in text
    assert f"video_id: {NEW[0]}" in text
    assert "## Applicabilité" in text
    assert "coder" in text  # au moins mentionné dans la section

    # Fiche projet coder a reçu la ligne d'idée dans IDEES.md.
    ideas = (config.obsidian.path / "Projets" / "coder" / "IDEES.md").read_text(encoding="utf-8")
    assert "PROMPT CODER" in ideas
    assert NEW[0] in ideas or "Résumé standard" in ideas or "coder" in ideas

    # === 3. Le commit local + push a été fait.
    # Un dépôt bare a maintenant au moins un ref.
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=str(remote), capture_output=True, text=True
    )
    assert log.returncode == 0
    assert "GUETTEUR" in log.stdout

    # === 4. Fake bot : appui sur « Garder » → déplacement vers Veille/<thème>.
    # Comme aucun thème n'est encore posé, le bouton « Garder » enverra vers « Inbox » par
    # défaut (comportement du bot Lot 6, invitant à corriger avec /theme). On force donc
    # un thème via set_theme d'abord, puis on déclenche le déplacement.
    store = world.store  # même Store SQLite (partagé)
    exporter = ObsidianExporter(config, store)
    exporter.set_theme(NEW[0], "LLM")

    api = FakeApi()
    bot = TelegramBot(
        config=config,
        store=store,
        summarizer=FakeSummarizer(),
        question_answerer=lambda t, q, h: f"Réponse à « {q} » — voir https://youtu.be/{NEW[0]}?t=0",
        claude_lock=threading.Lock(),
        notifier=FakeNotifier(api),  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        get_playlist=lambda pid: PlaylistConfig(id=pid or "PL", label="V"),
    )
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "data": f"v:{NEW[0]}:g",  # Garder
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        }
    )
    moved = (config.obsidian.path / "Veille" / "LLM").glob("*.md")
    moved_files = list(moved)
    assert len(moved_files) == 1
    assert moved_files[0].name == note_path.name

    # === 5. Question sur la vidéo → append dans « ## Questions ».
    store.link_message(500, NEW[0], "summary:auto")
    bot.handle_update(
        {
            "message": {
                "chat": {"id": 42},
                "text": "Quelle est la conclusion ?",
                "reply_to_message": {"message_id": 500},
            }
        }
    )
    # On ré-exporte pour capturer la nouvelle question dans le fichier (l'export
    # automatique du pipeline se fait à l'envoi de la vidéo — pas à chaque QA).
    from guetteur.summarize.base import summary_from_json

    record = store.get(NEW[0])
    assert record is not None and record.summary is not None
    summary = summary_from_json(record.summary)
    exporter.export_note(
        record.to_video(),
        summary,
        "standard",
        "LLM",
        [],
        [],
        projets_scores=[("coder", 3, "Utiliser XYZ pour Y"), ("guetteur", 0, "")],
    )
    final_text = moved_files[0].read_text(encoding="utf-8")
    assert "## Questions" in final_text
    assert "Quelle est la conclusion ?" in final_text

    # === 6. Bouton « Idée pour CODER » → envoie le méga-prompt en texte brut.
    api.messages_sent.clear()
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb2",
                "data": f"v:{NEW[0]}:i:coder",
                "message": {"chat": {"id": 42}, "message_id": 1},
            }
        }
    )
    # Un message texte brut envoyé contenant le méga-prompt.
    assert any("PROMPT CODER" in m["text"] for m in api.messages_sent)
    # Envoyé sans parse_mode (texte brut copiable).
    assert all(
        m.get("parse_mode") is None for m in api.messages_sent if "PROMPT CODER" in m["text"]
    )

    # Sanity : le clavier généré pour cette vidéo inclut le bouton « Idée pour CODER ».
    kb = build_summary_keyboard(NEW[0], "standard", project_slugs=["coder"])
    labels = [r[0]["text"] for r in kb["inline_keyboard"] if r]
    assert any(label == "Idée pour CODER" for label in labels)
