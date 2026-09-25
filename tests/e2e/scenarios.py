"""Scénarios e2e, indépendants du backend de résumé. Chaque fichier test_pipeline*.py les
exécute avec son backend : le comportement observable doit être identique."""

from __future__ import annotations

import json

import httpx

from guetteur.config import PlaylistConfig
from guetteur.store import Status
from tests.e2e.world import NEW, NOSUB, OLD, WorldFactory


def full_pipeline(make: WorldFactory) -> None:
    world = make()
    # 1) Premier lancement : l'existant est marqué « sent » sans aucun traitement.
    stats = world.pipeline.run_cycle()
    assert stats.discovered == 0 and stats.sent == 0
    assert world.telegram == []
    assert world.llm_calls == 0
    for vid, *_ in OLD:
        rec = world.store.get(vid)
        assert rec is not None and rec.status is Status.SENT and rec.sent_at is None

    # 2) Une nouvelle vidéo apparaît : transcrite, résumée, envoyée.
    world.feed.append(NEW)
    stats = world.pipeline.run_cycle()
    assert (stats.discovered, stats.sent, stats.failed) == (1, 1, 0)
    assert world.transcriber.calls == [NEW[0]]
    assert world.llm_calls == 1

    rec = world.store.get(NEW[0])
    assert rec is not None
    assert rec.status is Status.SENT
    assert rec.sent_at is not None
    assert rec.summary is not None and json.loads(rec.summary)["title"] == "Titre résumé"
    assert rec.transcript is not None

    assert len(world.telegram) == 1
    msg = world.telegram[0]
    assert msg["parse_mode"] == "MarkdownV2"
    assert msg["chat_id"] == "42"
    assert "https://youtu.be/NEW00000001?t=60" in msg["text"]

    # 3) Cycles suivants : rien n'est renvoyé (idempotence).
    world.pipeline.run_cycle()
    world.pipeline.run_cycle()
    assert len(world.telegram) == 1
    assert world.llm_calls == 1


def restart_does_not_resend(make: WorldFactory) -> None:
    world = make()
    world.pipeline.run_cycle()
    world.feed.append(NEW)
    world.pipeline.run_cycle()
    world.store.close()

    reborn = make(world.config)  # nouveau processus sur la même base
    reborn.feed.append(NEW)
    reborn.pipeline.run_cycle()
    assert reborn.telegram == []
    assert reborn.llm_calls == 0


def missing_transcript_retries_then_notifies(make: WorldFactory) -> None:
    world = make()
    world.pipeline.run_cycle()
    world.feed.append(NOSUB)

    for expected_retries in (1, 2, 3):
        world.pipeline.run_cycle()
        rec = world.store.get(NOSUB[0])
        assert rec is not None
        assert rec.status is Status.RETRY and rec.retries == expected_retries
        assert world.telegram == []

    world.pipeline.run_cycle()  # 4e tentative (3e nouvel essai) : abandon
    rec = world.store.get(NOSUB[0])
    assert rec is not None and rec.status is Status.FAILED
    assert len(world.telegram) == 1
    assert "Pas de transcription" in world.telegram[0]["text"]
    assert world.transcriber.calls.count(NOSUB[0]) == 4
    assert world.llm_calls == 0

    world.pipeline.run_cycle()  # plus rien ensuite
    assert len(world.telegram) == 1
    assert world.transcriber.calls.count(NOSUB[0]) == 4


def max_videos_per_cycle(make: WorldFactory) -> None:
    w = make(max_videos_per_cycle=2)
    w.pipeline.run_cycle()
    w.feed += [
        (f"BATCH000000{i}", f"Lot {i}", f"2024-03-0{i + 1}T10:00:00+00:00") for i in range(5)
    ]

    assert w.pipeline.run_cycle().sent == 2
    assert w.pipeline.run_cycle().sent == 2
    assert w.pipeline.run_cycle().sent == 1
    assert len(w.telegram) == 5
    assert w.store.counts() == {"sent": 7}


def backfill_processes_skipped_videos_once(make: WorldFactory) -> None:
    world = make()
    world.pipeline.run_cycle()  # initialisation : OLD ignorées

    stats = world.pipeline.backfill("PLtest123", limit=1)
    assert stats.sent == 1
    assert len(world.telegram) == 1
    rec = world.store.get("OLD00000002")  # la plus récente
    assert rec is not None and rec.status is Status.SENT and rec.sent_at is not None

    again = world.pipeline.backfill("PLtest123", limit=1)
    assert again.sent == 0
    assert len(world.telegram) == 1


def notify_failure_is_retried_without_resummarizing(make: WorldFactory) -> None:
    w = make()
    w.pipeline.run_cycle()
    w.feed.append(NEW)

    calls = {"n": 0}
    attempts = w.config.notify.max_attempts

    def flaky(request: httpx.Request) -> httpx.Response:
        # Panne passagère qui dure toute la première série de tentatives du cycle.
        calls["n"] += 1
        if calls["n"] <= attempts:
            return httpx.Response(502, json={"ok": False, "description": "gateway"})
        w.telegram.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    w.replace_telegram(flaky)

    assert w.pipeline.run_cycle().sent == 0
    rec = w.store.get(NEW[0])
    assert rec is not None and rec.status is Status.SUMMARIZED and rec.sent_at is None

    assert w.sleeps == [2.0, 8.0]  # attentes entre les 3 tentatives, sans attente réelle

    assert w.pipeline.run_cycle().sent == 1
    assert len(w.telegram) == 1
    assert w.llm_calls == 1  # résumé réutilisé


def playlist_language_is_passed(make: WorldFactory) -> None:
    w = make(playlists=(PlaylistConfig(id="PLtest123", label="EN", language="en"),))
    w.pipeline.run_cycle()
    w.feed.append(NEW)
    w.pipeline.run_cycle()
    assert "Langue du résumé : en" in w.last_prompt()


ALL = [
    full_pipeline,
    restart_does_not_resend,
    missing_transcript_retries_then_notifies,
    max_videos_per_cycle,
    backfill_processes_skipped_videos_once,
    notify_failure_is_retried_without_resummarizing,
    playlist_language_is_passed,
]
