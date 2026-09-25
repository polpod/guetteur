"""Erreur de configuration (403) sur le canal principal : aucune nouvelle tentative, statut
« failed » avec une raison lisible, le cycle continue avec les autres vidéos ; puis
`guetteur retry` remet la vidéo en file et elle part au cycle suivant."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from guetteur.main import cli
from guetteur.models import Video
from guetteur.store import Status, Store
from tests.e2e.world import World
from tests.helpers import make_config

FIRST = ("CFG00000001", "Première vidéo", "2024-02-01T10:00:00+00:00")
SECOND = ("CFG00000002", "Seconde vidéo", "2024-02-02T10:00:00+00:00")


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[general]\ndata_dir = "{tmp_path.as_posix()}"\n\n'
        '[[playlists]]\nid = "PLtest123"\nlabel = "Veille"\n',
        encoding="utf-8",
    )
    return path


def test_403_is_not_retried_then_guetteur_retry_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(make_config(tmp_path), "claude_api")
    world.pipeline.run_cycle()
    world.feed += [FIRST, SECOND]

    calls = {"n": 0}

    def forbidden_once(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
            )
        world.telegram.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    world.replace_telegram(forbidden_once)
    stats = world.pipeline.run_cycle()

    # 403 : une seule tentative, aucune attente, « failed » avec la raison en base.
    first = world.store.get(FIRST[0])
    assert first is not None and first.status is Status.FAILED
    assert "403" in (first.last_error or "") and "bot was blocked" in (first.last_error or "")
    assert "définitive" in (first.last_error or "")
    assert world.sleeps == []
    assert [(d.attempt, d.ok) for d in world.store.deliveries(FIRST[0])] == [(1, False)]
    # Le cycle a continué : la seconde vidéo est partie.
    second = world.store.get(SECOND[0])
    assert second is not None and second.status is Status.SENT
    assert (stats.sent, stats.failed) == (1, 1)

    world.pipeline.run_cycle()  # pas de nouvelle tentative automatique
    assert len(world.store.deliveries(FIRST[0])) == 1

    # L'utilisateur corrige la configuration puis lance `guetteur retry`.
    config_path = _write_config(tmp_path)
    assert cli(["--config", str(config_path), "retry", "--video-id", FIRST[0]]) == 0
    assert "1 vidéo(s) remise(s) en file" in capsys.readouterr().out
    first = world.store.get(FIRST[0])
    assert first is not None
    assert (first.status, first.retries, first.last_error) == (Status.SUMMARIZED, 0, None)

    assert world.pipeline.run_cycle().sent == 1
    first = world.store.get(FIRST[0])
    assert first is not None and first.status is Status.SENT
    assert world.llm_calls == 2  # un résumé par vidéo, jamais refait
    assert [d.ok for d in world.store.deliveries(FIRST[0])] == [False, True]


def test_retry_all_via_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path)
    store = Store(tmp_path / "guetteur.db")
    for vid in ("A", "B"):
        store.add_new(Video(vid, vid, "c", None, "u"), "PL")
        store.mark_failed(vid, "HTTP 400 : chat not found")
    store.close()

    assert cli(["--config", str(config_path), "retry", "--all"]) == 0
    assert "2 vidéo(s) remise(s) en file" in capsys.readouterr().out
    store = Store(tmp_path / "guetteur.db")
    assert store.counts() == {"new": 2}
    store.close()
