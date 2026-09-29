"""Tests d'AdaptiveApiKeySource : bascule automatique API → RSS quand le quota
approche le plafond, alertes Telegram dé-doublonnées, 403 quotaExceeded traité
comme un signal Google de plafond atteint (compteur mis à hard_limit)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from guetteur.sources.adaptive import AdaptiveApiKeySource
from guetteur.sources.api import YouTubeApiKeySource
from guetteur.sources.rss import RssSource
from guetteur.store import Store


def _fake_rss_returning(video_id: str = "RSS0000001") -> RssSource:
    """Un RssSource dont le transport mock renvoie un flux à une seule vidéo."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <id>yt:video:{video_id}</id>
  <yt:videoId>{video_id}</yt:videoId>
  <title>RSS</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>
  <author><name>C</name></author>
  <published>2026-09-01T10:00:00+00:00</published>
 </entry>
</feed>"""

    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    return RssSource(httpx.Client(transport=httpx.MockTransport(h)))


def _api_returning(video_id: str = "API0000001") -> YouTubeApiKeySource:
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "snippet": {
                            "title": "API",
                            "publishedAt": "2026-09-02T10:00:00Z",
                            "videoOwnerChannelTitle": "C",
                        },
                        "contentDetails": {
                            "videoId": video_id,
                            "videoPublishedAt": "2026-09-02T10:00:00Z",
                        },
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(h))
    return YouTubeApiKeySource("AIza", client=client)


def _api_403_quota() -> YouTubeApiKeySource:
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "errors": [{"reason": "quotaExceeded"}],
                }
            },
        )

    return YouTubeApiKeySource("AIza", client=httpx.Client(transport=httpx.MockTransport(h)))


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "adaptive.db")


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def test_uses_api_when_below_hard_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    src = AdaptiveApiKeySource(
        _api_returning("API_PICK"),
        _fake_rss_returning(),
        store,
        now=lambda: NOW,
    )
    videos = src.fetch("PLtest")
    assert [v.video_id for v in videos] == ["API_PICK"]
    # Le compteur a été incrémenté par YouTubeApiKeySource.on_call — mais on l'a
    # passé sans callback ici : on ne teste que la route "sous seuil".
    assert store.youtube_quota_used(NOW) == 0  # pas de on_call posé dans _api_returning


def test_falls_back_to_rss_when_at_or_above_hard_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # On simule un quota déjà consommé (hard_limit atteint).
    store.youtube_quota_bump(9500, now=NOW)
    src = AdaptiveApiKeySource(
        _api_returning("API_SHOULD_NOT_BE_CALLED"),
        _fake_rss_returning("RSS_PICK"),
        store,
        now=lambda: NOW,
    )
    videos = src.fetch("PLtest")
    # RSS a été appelé, pas l'API : la vidéo renvoyée porte l'ID mocké côté RSS.
    assert [v.video_id for v in videos] == ["RSS_PICK"]


def test_quota_exceeded_marks_hard_limit_and_falls_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    alerts: list[str] = []
    src = AdaptiveApiKeySource(
        _api_403_quota(),
        _fake_rss_returning("RSS_AFTER_403"),
        store,
        alert=alerts.append,
        now=lambda: NOW,
    )
    videos = src.fetch("PLtest")
    # Le compteur est monté au plafond dur.
    assert store.youtube_quota_used(NOW) == 9500
    # Et l'appel a été servi en RSS.
    assert [v.video_id for v in videos] == ["RSS_AFTER_403"]
    # Une alerte 9k5 est partie.
    assert any("9500" in a for a in alerts)


def test_soft_alert_at_8000_then_dedup_same_day(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # 7999 → 8000 après un appel API.
    store.youtube_quota_bump(7999, now=NOW)
    alerts: list[str] = []

    def bump() -> None:
        store.youtube_quota_bump(1, now=NOW)

    api = YouTubeApiKeySource(
        "AIza",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": []}))
        ),
        on_call=bump,
    )
    src = AdaptiveApiKeySource(
        api, _fake_rss_returning(), store, alert=alerts.append, now=lambda: NOW
    )
    src.fetch("PLtest")
    assert store.youtube_quota_used(NOW) == 8000
    assert any("8000" in a for a in alerts)
    # Deuxième appel dans la journée : pas de nouvelle alerte 8k.
    alerts.clear()
    src.fetch("PLtest")
    assert alerts == []


def test_switch_history_when_previous_bump_crosses_9500_in_two_calls(tmp_path: Path) -> None:
    """Cas concret : 9499 → 9500 puis 9501. Le premier appel déclenche l'alerte 9k5
    et l'appel suivant part directement en RSS."""
    store = _store(tmp_path)
    store.youtube_quota_bump(9499, now=NOW)

    calls_api = 0

    def bump() -> None:
        nonlocal calls_api
        calls_api += 1
        store.youtube_quota_bump(1, now=NOW)

    api = YouTubeApiKeySource(
        "AIza",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": []}))
        ),
        on_call=bump,
    )
    alerts: list[str] = []
    src = AdaptiveApiKeySource(
        api, _fake_rss_returning("RSS_HIT"), store, alert=alerts.append, now=lambda: NOW
    )
    src.fetch("PLtest")  # API → 9500, alerte 9k5
    assert calls_api == 1
    assert any("9500" in a for a in alerts)
    videos = src.fetch("PLtest")  # RSS
    assert calls_api == 1
    assert [v.video_id for v in videos] == ["RSS_HIT"]
