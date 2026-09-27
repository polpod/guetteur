"""E2E applicabilité (Lot 6) : quand aucun projet n'est pertinent, la note est
créée SANS ligne « Pertinent pour », le fichier IDEES.md reste intact (ou absent),
et le clavier du bot n'affiche AUCUN bouton « Idée pour <projet> »."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from guetteur.config import (
    ApplicabilityConfig,
    ObsidianConfig,
    PlaylistConfig,
)
from guetteur.notify.telegram_bot import build_summary_keyboard
from guetteur.summarize.applicability import Pertinence
from tests.e2e.world import NEW, World
from tests.helpers import make_config


def test_applicability_none_leaves_ideas_intact(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille", detail="standard"),),
        obsidian=ObsidianConfig(enabled=True, path=vault, git_sync=False, git_remote=""),
        applicability=ApplicabilityConfig(enabled=True),
    )
    world = World(config, "claude_api")

    def create(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "title": "Vidéo hors sujet",
                            "tldr": "Une vidéo. Sans rapport.",
                            "key_points": [{"seconds": i, "text": f"P{i}"} for i in range(5)],
                            "why_it_matters": "Rien pour nos projets.",
                            "announced_items": 0,
                        }
                    ),
                )
            ],
        )

    world.claude.messages.create = MagicMock(side_effect=create)

    # Applicabilité : tous les projets à score 0.
    def fake_evaluate_zero(video: Any, summary: Any, projects: Any) -> list[Pertinence]:
        return [Pertinence(p.slug, 0, "", "", "", "", "") for p in projects]

    with patch("guetteur.summarize.applicability.build_evaluator_from_summarizer") as mock_build:
        evaluator = MagicMock()
        evaluator.evaluate.side_effect = fake_evaluate_zero
        mock_build.return_value = evaluator
        world.pipeline.run_cycle()
        world.feed.append(NEW)
        world.pipeline.run_cycle()

    # Note créée dans Inbox.
    notes = list((vault / "Veille" / "Inbox").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    # Aucun projet mentionné en applicabilité (aucune ligne « - **<slug>** »).
    assert "**coder**" not in text
    assert "**guetteur**" not in text

    # IDEES.md par projet : soit inexistant, soit sans nouvelle entrée pour cette vidéo.
    for slug in ("coder", "guetteur", "eagle", "vigie", "console"):
        ideas = vault / "Projets" / slug / "IDEES.md"
        if ideas.exists():
            assert NEW[0] not in ideas.read_text(encoding="utf-8")

    # Bot : le clavier construit avec 0 projet actionnable n'affiche AUCUN bouton
    # « Idée pour <projet> ».
    kb = build_summary_keyboard(NEW[0], "standard", project_slugs=[])
    labels = [r[0]["text"] for r in kb["inline_keyboard"] if r]
    assert not any("Idée pour" in label for label in labels)
