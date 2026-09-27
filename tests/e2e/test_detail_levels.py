"""E2E : pipeline complet avec une playlist en mode « detaille » et test unitaire de la
commande `guetteur compare`. Le backend LLM est mocké (fake Anthropic) : aucun appel réseau."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from guetteur.config import PlaylistConfig
from guetteur.main import cli
from guetteur.models import Video
from guetteur.store import Status, Store
from tests.e2e.world import NEW, World
from tests.helpers import make_config

# --- payloads factices du backend LLM ---------------------------------------------------


def _detailed_response(n_sections: int = 6) -> dict[str, Any]:
    """Payload « detaille » assez gros pour dépasser 4096 caractères une fois rendu."""
    sections = []
    for i in range(n_sections):
        sections.append(
            {
                "title": f"Section thématique {i + 1}",
                "seconds": (i + 1) * 90,
                "bullets": [
                    f"Puce {i + 1}.a — chiffre précis : {(i + 1) * 11} %",
                    f"Puce {i + 1}.b — outil mentionné : uv",
                    f"Puce {i + 1}.c — cas concret {i + 1} " + "x" * 200,
                    f"Puce {i + 1}.d — comparaison " + "y" * 200,
                ],
            }
        )
    return {
        "title": "Résumé détaillé de la nouvelle vidéo",
        "tldr": (
            "Ce résumé détaillé couvre les points importants. Il liste les outils cités. "
            "Il donne aussi les précautions à prendre."
        ),
        "sections": sections,
        "citations": [
            {"seconds": 120, "text": "Une reformulation précise du passage."},
            {"seconds": 480, "text": "Une seconde reformulation."},
        ],
        "actions": [
            "Utilise uv pour verrouiller.",
            "Vérifie la couverture des tests.",
            "Évite les commits sur main.",
        ],
        "reserves": ["L'auteur n'a pas justifié un bench cité."],
        "announced_items": 0,
    }


# --- e2e pipeline ----------------------------------------------------------------------


def test_pipeline_detaille_sends_numbered_multi_part_message(
    tmp_path: Path,
) -> None:
    """Une playlist en « detaille » : le message est découpé en plusieurs parts numérotées
    « (i/N) », les sections sont dans l'ordre, les timestamps croissants, aucune coupe au
    milieu d'une puce."""
    detailed = _detailed_response(6)
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille IA", detail="detaille"),),
    )
    world = World(config, "claude_api")
    # Injecte un fake anthropic qui retourne toujours le payload détaillé.
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    def create(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(detailed))],
        )

    world.claude.messages.create = MagicMock(side_effect=create)
    world.pipeline.run_cycle()  # initialisation de la playlist
    world.feed.append(NEW)

    stats = world.pipeline.run_cycle()
    assert stats.sent == 1

    rec = world.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SENT
    # Le résumé persisté indique bien le niveau de détail utilisé.
    assert rec.summary is not None
    assert '"detail": "detaille"' in rec.summary

    # Plusieurs messages Telegram envoyés (au moins 2).
    assert len(world.telegram) >= 2
    # Chaque part porte la numérotation (i/N).
    for i, payload in enumerate(world.telegram, start=1):
        text = payload["text"]
        assert text.startswith(f"\\({i}/{len(world.telegram)}\\)"), (
            f"Part {i} ne commence pas par (i/N) : {text[:80]}"
        )
        # Aucune part ne dépasse la limite Telegram.
        assert len(text) <= 4096
    # Sections dans l'ordre : Section 1 apparaît avant Section 2 dans le corpus global.
    corpus = "\n".join(p["text"] for p in world.telegram)
    positions = [corpus.index(f"*Section thématique {i}*") for i in range(1, 7)]
    assert positions == sorted(positions)
    # Les timestamps DES SECTIONS sont croissants (les citations peuvent pointer ailleurs).
    section_line_re = re.compile(r"\*Section thématique \d+\*[^\n]*\?t=(\d+)")
    section_ts = [int(m.group(1)) for m in section_line_re.finditer(corpus)]
    assert len(section_ts) == 6
    assert section_ts == sorted(section_ts)


def test_pipeline_bref_stays_in_a_single_message(tmp_path: Path) -> None:
    """En mode bref, un seul message envoyé, pas de numérotation."""
    brief = {
        "title": "Résumé bref",
        "tldr": "Phrase A. Phrase B.",
        "key_points": [
            {"seconds": 0, "text": "Point 1"},
            {"seconds": 30, "text": "Point 2"},
            {"seconds": 60, "text": "Point 3"},
        ],
        "actions": ["Fais ceci."],
        "announced_items": 0,
    }
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille", detail="bref"),),
    )
    world = World(config, "claude_api")

    from types import SimpleNamespace
    from unittest.mock import MagicMock

    world.claude.messages.create = MagicMock(
        return_value=SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(brief))],
        )
    )
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    assert world.pipeline.run_cycle().sent == 1
    assert len(world.telegram) == 1
    text = world.telegram[0]["text"]
    # Pas de numérotation.
    assert not text.startswith("\\(1/")
    # Titre en gras, TL;DR, points clés, action.
    assert "*Points clés*" in text
    assert "*À faire*" in text


def test_playlist_detail_wins_over_standard_default(tmp_path: Path) -> None:
    """Une playlist « detaille » avec CLI --detail=bref : le CLI l'emporte."""
    brief = {
        "title": "Résumé bref forcé",
        "tldr": "T1. T2.",
        "key_points": [{"seconds": 0, "text": f"P{i}"} for i in range(3)],
        "actions": ["Une action"],
        "announced_items": 0,
    }
    config = make_config(
        tmp_path,
        playlists=(PlaylistConfig(id="PLtest123", label="Veille", detail="detaille"),),
    )
    world = World(config, "claude_api")
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    seen_prompts: list[str] = []

    def create(**kwargs: Any) -> SimpleNamespace:
        seen_prompts.append(kwargs.get("system", ""))
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(brief))],
        )

    world.claude.messages.create = MagicMock(side_effect=create)
    # Reconfigure le pipeline pour appliquer un detail_override
    from guetteur.pipeline import Pipeline

    world.pipeline = Pipeline(
        config=world.config,
        store=world.store,
        source_factory=world.pipeline._source_factory,
        transcriber=world.transcriber,
        summarizer=world.pipeline._summarizer,
        notifier_factory=world._notifier_for,
        sleep=world.sleeps.append,
        detail_override="bref",
    )
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    world.pipeline.run_cycle()
    # Le system prompt utilisé était celui du niveau bref.
    assert any("30 secondes" in p or "120 mots" in p for p in seen_prompts)


# --- test unitaire de `guetteur compare` -----------------------------------------------


def _seed_transcribed(store: Store, video_id: str, title: str) -> None:
    """Vidéo avec transcription en base (pré-requis pour `guetteur compare`)."""
    v = Video(video_id, title, "Chaîne", None, f"https://youtu.be/{video_id}")
    store.add_new(v, "PL")
    store.set_transcript(
        video_id,
        json.dumps({"language": "fr", "source": "youtube", "segments": [[0, "Salut."]]}),
    )


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[general]\ndata_dir = "{tmp_path.as_posix()}"\n\n'
        '[summarize]\nprovider = "claude_api"\n\n'
        '[[playlists]]\nid = "PL"\nlabel = "Veille"\ndetail = "standard"\n',
        encoding="utf-8",
    )
    return path


def test_cli_compare_sends_three_headers_without_touching_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`guetteur compare --video-id X` envoie 3 messages « [BREF] », « [STANDARD] »,
    « [DETAILLE] » sur Telegram, sans modifier le statut de la vidéo en base."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    store = Store(tmp_path / "guetteur.db")
    _seed_transcribed(store, "VID_CMP", "Vidéo à comparer")
    store.close()

    # Payloads renvoyés par le fake summarizer, adaptés au niveau demandé.
    def summarize_stub(transcript: Any, meta: Any) -> Any:
        from guetteur.summarize.base import to_summary

        if meta.detail == "bref":
            payload = {
                "title": "T bref",
                "tldr": "A. B.",
                "key_points": [{"seconds": i, "text": f"P{i}"} for i in range(3)],
                "actions": ["Fais X."],
                "announced_items": 0,
            }
        elif meta.detail == "detaille":
            payload = _detailed_response(4)
        else:
            payload = {
                "title": "T standard",
                "tldr": "Une phrase. Deux.",
                "key_points": [{"seconds": i * 30, "text": f"P{i}"} for i in range(6)],
                "why_it_matters": "Ça compte.",
                "announced_items": 0,
            }
        return to_summary(payload, detail=meta.detail)

    sent_messages: list[Any] = []

    class _RecordingNotifier:
        name = "telegram"

        def send(self, message: Any) -> str | None:
            sent_messages.append(message)
            return "ok"

    with (
        patch("guetteur.summarize.build_summarizer") as bs,
        patch("guetteur.main.build_notifier") as bn,
    ):
        bs.return_value.summarize.side_effect = summarize_stub
        bn.return_value = _RecordingNotifier()
        code = cli(
            [
                "--config",
                str(_config_file(tmp_path)),
                "compare",
                "--video-id",
                "VID_CMP",
            ]
        )
    out = capsys.readouterr().out
    assert code == 0
    assert len(sent_messages) == 3, "les 3 niveaux doivent être envoyés"
    # Chaque message porte son en-tête [BREF], [STANDARD] ou [DETAILLE].
    headers_seen = {m.plain.split("\n", 1)[0] for m in sent_messages}
    assert headers_seen == {"[BREF]", "[STANDARD]", "[DETAILLE]"}
    # Le statut en base n'a PAS bougé : `transcribed` (issu du seed), pas de summary
    # persisté, aucun envoi horodaté. Le résumé de comparaison est éphémère.
    store = Store(tmp_path / "guetteur.db")
    try:
        rec = store.get("VID_CMP")
        assert rec is not None
        assert rec.status is Status.TRANSCRIBED
        assert rec.summary is None  # résumé de comparaison JAMAIS persisté
        assert rec.sent_at is None
    finally:
        store.close()
    assert "[BREF]" in out and "[STANDARD]" in out and "[DETAILLE]" in out


def test_cli_compare_requires_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Une vidéo sans transcription en base est refusée par `compare` (message clair)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    store = Store(tmp_path / "guetteur.db")
    v = Video("VID_NO_TR", "Sans transcription", "C", None, "https://youtu.be/x")
    store.add_new(v, "PL")
    store.close()
    code = cli(
        [
            "--config",
            str(_config_file(tmp_path)),
            "compare",
            "--video-id",
            "VID_NO_TR",
        ]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "transcription" in err


def test_cli_compare_unknown_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    Store(tmp_path / "guetteur.db").close()
    code = cli(
        [
            "--config",
            str(_config_file(tmp_path)),
            "compare",
            "--video-id",
            "INCONNUE",
        ]
    )
    assert code == 1
    assert "inconnue" in capsys.readouterr().err.lower()
