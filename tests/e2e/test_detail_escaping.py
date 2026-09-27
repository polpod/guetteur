"""E2E : pipeline mocké en mode `detaille` avec un résumé contenant tous les caractères
réservés MarkdownV2. Les trois parties partent en MarkdownV2 sans qu'aucune ne soit
rejetée par Telegram — le bug de production (partie 3/3 rejetée pour `!` nu) doit rester
corrigé de bout en bout."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from guetteur.config import PlaylistConfig
from guetteur.store import Status
from guetteur.summarize.format import validate_markdown_v2
from tests.e2e.world import NEW, World
from tests.helpers import make_config


def _payload_with_all_reserved_chars() -> dict[str, Any]:
    """Résumé détaillé où chaque champ contient tous les caractères réservés MarkdownV2.

    Les puces sont assez longues pour forcer un découpage en plusieurs parts (le bug
    production concernait justement la 3/3 avec `!` en tête des réserves)."""
    reserved = "_*[]()~`>#+-=|{}.!"
    sections = [
        {
            "title": f"Section {i + 1} — chars {reserved}",
            "seconds": (i + 1) * 90,
            "bullets": [
                f"Puce {i + 1}.a — attention ! chiffre 3.14 {reserved}",
                f"Puce {i + 1}.b — outil (uv) v0.8+ {reserved} " + "x" * 250,
                f"Puce {i + 1}.c — comparaison A vs B {reserved} " + "y" * 250,
                f"Puce {i + 1}.d — précision {reserved} conclusion.",
            ],
        }
        for i in range(8)
    ]
    return {
        "title": f"Vidéo test ! avec {reserved} partout.",
        "tldr": (
            f"Phrase 1 avec ! et . et > {reserved}. "
            f"Phrase 2 : autre point {reserved} ! "
            f"Phrase 3 : conclusion {reserved}."
        ),
        "sections": sections,
        "citations": [
            {"seconds": 60, "text": f"Citation ! reformulée {reserved} concise."},
            {"seconds": 300, "text": f"Autre reformulation {reserved} exemplaire."},
        ],
        "actions": [
            f"Utilise uv ! {reserved}",
            f"Vérifie la couverture . {reserved}",
            f"Évite les commits directs > main {reserved}",
        ],
        "reserves": [
            f"Un bench cité sans source {reserved} ! attention.",
            f"Une affirmation discutable . {reserved}",
        ],
        "announced_items": 0,
    }


def test_pipeline_detaille_bombshell_all_parts_pass_validation(tmp_path: Path) -> None:
    """Pipeline complet en mode `detaille` avec un résumé bourré de caractères réservés :
    - Plusieurs parts envoyées à Telegram (le résumé dépasse 4096 caractères).
    - Chaque part passe la pré-validation → toutes envoyées en MarkdownV2.
    - Chaque part est bien formée selon `validate_markdown_v2` : le bug production
      (`!` nu en tête des réserves) est corrigé de bout en bout."""
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille IA", detail="detaille"),),
    )
    world = World(config, "claude_api")
    payload = _payload_with_all_reserved_chars()

    def create(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        )

    world.claude.messages.create = MagicMock(side_effect=create)
    world.pipeline.run_cycle()  # initialisation playlist
    world.feed.append(NEW)

    stats = world.pipeline.run_cycle()
    assert stats.sent == 1

    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SENT

    # Plusieurs parts envoyées à Telegram (le résumé est assez long pour dépasser 4096).
    assert len(world.telegram) >= 3, (
        f"Attendu ≥ 3 parts, obtenu {len(world.telegram)} — payload trop court ?"
    )

    # Chaque envoi Telegram porte parse_mode=MarkdownV2 (aucun fallback n'a été déclenché).
    for i, sent in enumerate(world.telegram, start=1):
        assert sent.get("parse_mode") == "MarkdownV2", (
            f"Part {i}/{len(world.telegram)} envoyée sans MarkdownV2 : {sent}"
        )
        # Numérotation (i/N) présente en tête.
        text = sent["text"]
        assert text.startswith(f"\\({i}/{len(world.telegram)}\\)")
        # Chaque part est un MarkdownV2 valide selon notre validateur.
        ok, offset = validate_markdown_v2(text)
        assert ok, (
            f"Part {i}/{len(world.telegram)} invalide à l'offset {offset} : "
            f"...{text[max(0, offset - 30) : offset + 30]!r}..."
        )
        # Aucun `!` ou `>` nu (bug production).
        assert "\n!" not in text, f"Part {i} : `!` nu en début de ligne"
        assert "\n>" not in text, f"Part {i} : `>` nu en début de ligne"
        # Chaque part ≤ 4096 (limite Telegram).
        assert len(text) <= 4096


def test_pipeline_detaille_short_payload_stays_single_part(tmp_path: Path) -> None:
    """Un résumé détaillé court reste sur une seule part, sans numérotation, et passe
    tout de même la pré-validation MarkdownV2."""
    payload = {
        "title": "Court ! avec un point.",
        "tldr": "Phrase courte. Deuxième !",
        "sections": [
            {
                "title": "Section unique",
                "seconds": 30,
                "bullets": ["Puce ! une", "Puce . deux", "Puce > trois"],
            },
        ],
        "citations": [{"seconds": 15, "text": "Citation !"}],
        "actions": ["Fais X."],
        "reserves": ["Attention !"],
        "announced_items": 0,
    }
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille", detail="detaille"),),
    )
    world = World(config, "claude_api")
    world.claude.messages.create = MagicMock(
        return_value=SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        )
    )
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    assert world.pipeline.run_cycle().sent == 1

    assert len(world.telegram) == 1
    sent = world.telegram[0]
    assert sent.get("parse_mode") == "MarkdownV2"
    text = sent["text"]
    # Pas de numérotation quand une seule part.
    assert not text.startswith("\\(1/")
    ok, offset = validate_markdown_v2(text)
    assert ok, (
        f"Rendu invalide à l'offset {offset} : ...{text[max(0, offset - 20) : offset + 20]!r}..."
    )
    # Bug production absent : ni `!` ni `>` nu.
    assert "\n!" not in text
    assert "\n>" not in text
