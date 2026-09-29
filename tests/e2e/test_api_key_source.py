"""E2E : détection d'une nouvelle vidéo par la source YouTube Data API par clé,
en moins de 60 s (poll_interval par défaut en mode API).

Le pipeline est identique aux autres e2e (RSS habituellement) : transcription
mockée, backend claude_api mocké, Telegram mocké. Seule la source change : on
substitue un AdaptiveApiKeySource qui répond en JSON YouTube-API-shaped."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from guetteur.config import Secrets
from guetteur.notify.telegram import TelegramNotifier
from guetteur.pipeline import Pipeline
from guetteur.sources.adaptive import AdaptiveApiKeySource
from guetteur.sources.api import YouTubeApiKeySource
from guetteur.sources.rss import RssSource
from guetteur.store import Status, Store
from guetteur.summarize.claude_api import ClaudeApiSummarizer
from tests.helpers import FakeTranscriber, fake_anthropic, make_config


def _api_item(vid: str, title: str, published: str) -> dict[str, Any]:
    return {
        "snippet": {
            "title": title,
            "publishedAt": published,
            "videoOwnerChannelTitle": "Chaîne",
        },
        "contentDetails": {"videoId": vid, "videoPublishedAt": published},
    }


def test_api_key_source_new_video_seen_within_60s(tmp_path: Path) -> None:
    """Cycle 1 : deux vidéos existantes (bootstrap silencieux).
    Cycle 2 : ajout d'une nouvelle vidéo côté API → transcrite, résumée, envoyée.
    poll_interval_seconds vaut 60 par défaut en mode API : `guetteur run` détecte
    dans les 60 s au maximum."""
    # Feed API modifiable en cours de test.
    feed: list[tuple[str, str, str]] = [
        ("OLD00000001", "Ancienne 1", "2026-09-01T10:00:00Z"),
        ("OLD00000002", "Ancienne 2", "2026-09-02T10:00:00Z"),
    ]
    api_calls: list[str] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.googleapis.com"
        params = dict(request.url.params)
        assert params.get("key") == "AIza-test-key"
        api_calls.append(params.get("playlistId", ""))
        return httpx.Response(
            200,
            json={"items": [_api_item(vid, title, pub) for vid, title, pub in feed]},
        )

    telegram_messages: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        telegram_messages.append(json.loads(request.content))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1000 + len(telegram_messages)}}
        )

    cfg = make_config(
        tmp_path,
        source="api",
        # Défaut du mode API : 60 s. Passé explicitement ici pour que le pipeline
        # planifie bien un cycle par minute — le défaut est validé au niveau
        # parse_config par test_source_selection.
        poll_interval_seconds=60,
        secrets=Secrets(
            youtube_api_key="AIza-test-key",
            telegram_bot_token="TOKEN",
            telegram_chat_id="42",
        ),
    )
    assert cfg.poll_interval_seconds == 60

    store = Store(cfg.db_path)
    api_client = httpx.Client(transport=httpx.MockTransport(api_handler))

    def bump_quota() -> None:
        store.youtube_quota_bump(1)

    api = YouTubeApiKeySource("AIza-test-key", client=api_client, on_call=bump_quota)
    adaptive = AdaptiveApiKeySource(api, RssSource(), store)
    telegram_client = httpx.Client(transport=httpx.MockTransport(telegram_handler))
    telegram = TelegramNotifier("TOKEN", "42", telegram_client)

    pipeline = Pipeline(
        config=cfg,
        store=store,
        source_factory=lambda _p: adaptive,
        transcriber=FakeTranscriber(),
        summarizer=ClaudeApiSummarizer(fake_anthropic(), cfg.claude_model),
        notifier_factory=lambda _c: telegram,
        sleep=lambda _s: None,
    )

    # Cycle 1 : bootstrap.
    stats = pipeline.run_cycle()
    assert stats.discovered == 0 and stats.sent == 0
    assert telegram_messages == []
    assert store.youtube_quota_used() == 1

    # Une nouvelle vidéo publiée « il y a 30 secondes » côté YouTube.
    feed.append(("NEW00000001", "Nouvelle vidéo utile", "2026-09-29T11:59:30Z"))

    # Cycle 2 : détection.
    stats = pipeline.run_cycle()
    assert (stats.discovered, stats.sent, stats.failed) == (1, 1, 0)
    # 2 appels API cumulés (un par cycle) → 2 unités de quota consommées.
    assert store.youtube_quota_used() == 2
    rec = store.get("NEW00000001")
    assert rec is not None and rec.status is Status.SENT and rec.sent_at is not None
    assert len(telegram_messages) == 1
    # Le message Telegram contient le lien vers la NOUVELLE vidéo (le résumé est
    # une doublure `Titre résumé` ; ce qui compte ici, c'est l'id vidéo découvert
    # via l'API, pas via le RSS).
    assert "NEW00000001" in telegram_messages[0]["text"]


def test_api_falls_back_to_rss_when_quota_exhausted(tmp_path: Path) -> None:
    """Si le quota est déjà à 9500, on ne tape même pas l'API : la source
    délègue au RSS. Vérifie que le pipeline continue à voir les vidéos."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <id>yt:video:RSS0000001</id>
  <yt:videoId>RSS0000001</yt:videoId>
  <title>Vidéo RSS</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=RSS0000001"/>
  <author><name>C</name></author>
  <published>2026-09-29T12:00:00+00:00</published>
 </entry>
</feed>"""

    api_hit = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal api_hit
        api_hit += 1
        return httpx.Response(500, text="ne devrait pas être appelée")

    def rss_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    cfg = make_config(
        tmp_path,
        source="api",
        secrets=Secrets(youtube_api_key="AIza"),
    )
    store = Store(cfg.db_path)
    store.youtube_quota_bump(9500)  # déjà au plafond

    api = YouTubeApiKeySource(
        "AIza", client=httpx.Client(transport=httpx.MockTransport(api_handler))
    )
    rss = RssSource(httpx.Client(transport=httpx.MockTransport(rss_handler)))
    adaptive = AdaptiveApiKeySource(api, rss, store)

    videos = adaptive.fetch(cfg.playlists[0].id)
    assert api_hit == 0
    assert [v.video_id for v in videos] == ["RSS0000001"]
