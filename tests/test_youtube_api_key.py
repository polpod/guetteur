"""Tests unitaires de la source YouTube Data API par clé.

Mock httpx (MockTransport) : une page, deux pages avec nextPageToken, 403 quota,
404 playlist. Vérifie aussi que `on_call` est incrémenté une fois par appel HTTP,
et que la sortie est ordonnée par date de publication décroissante."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from guetteur.sources.api import (
    PLAYLIST_ITEMS_URL,
    PlaylistNotFoundError,
    QuotaExceededError,
    YouTubeApiKeySource,
)
from guetteur.sources.base import SourceError


def _item(vid: str, title: str, published: str, channel: str = "Chaîne") -> dict[str, Any]:
    return {
        "snippet": {
            "title": title,
            "publishedAt": published,
            "videoOwnerChannelTitle": channel,
        },
        "contentDetails": {"videoId": vid, "videoPublishedAt": published},
    }


def _mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_single_page_returns_videos_and_bumps_counter() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path.endswith("/playlistItems")
        params = dict(request.url.params)
        assert params["key"] == "AIza-test"
        assert params["playlistId"] == "PLtest"
        assert params["part"] == "snippet,contentDetails"
        return httpx.Response(
            200,
            json={
                "items": [
                    _item("v1", "Vidéo 1", "2026-09-01T10:00:00Z"),
                    _item("v2", "Vidéo 2", "2026-09-02T10:00:00Z"),
                ]
            },
        )

    bumps = 0

    def bump() -> None:
        nonlocal bumps
        bumps += 1

    src = YouTubeApiKeySource("AIza-test", client=_mock_client(handler), on_call=bump)
    videos = src.fetch("PLtest")
    assert calls == 1
    assert bumps == 1
    # Ordre : la plus récente en tête.
    assert [v.video_id for v in videos] == ["v2", "v1"]
    assert videos[0].url == "https://www.youtube.com/watch?v=v2"


def test_two_pages_follows_next_page_token_and_counts_two_calls() -> None:
    def _page(rng: range) -> list[dict[str, Any]]:
        return [_item(f"v{i}", f"Vidéo {i}", "2026-09-01T10:00:00Z") for i in rng]

    pages = iter(
        [
            {"items": _page(range(1, 51)), "nextPageToken": "PAGE2"},
            {"items": _page(range(51, 56))},
        ]
    )
    call_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        call_tokens.append(params.get("pageToken"))
        return httpx.Response(200, json=next(pages))

    bumps = 0

    def bump() -> None:
        nonlocal bumps
        bumps += 1

    # max_items=60 → 2 pages nécessaires ; 50 + 5 items.
    src = YouTubeApiKeySource("AIza-test", client=_mock_client(handler), max_items=60, on_call=bump)
    videos = src.fetch("PLtest")
    assert call_tokens == [None, "PAGE2"]
    assert bumps == 2
    assert len(videos) == 55


def test_stops_at_max_items_even_if_more_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [_item(f"v{i}", "T", "2026-09-01T10:00:00Z") for i in range(50)],
                "nextPageToken": "MORE",
            },
        )

    # max_items=50 = une page unique, on ne doit PAS enchaîner sur nextPageToken.
    calls = 0

    def _counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return handler(request)

    src = YouTubeApiKeySource("AIza-test", client=_mock_client(_counting), max_items=50)
    videos = src.fetch("PLtest")
    assert calls == 1
    assert len(videos) == 50


@pytest.mark.parametrize("reason", ["quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"])
def test_403_with_quota_reason_raises_quota_exceeded(reason: str) -> None:
    bumps = 0

    def bump() -> None:
        nonlocal bumps
        bumps += 1

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "quota",
                    "errors": [{"reason": reason, "domain": "youtube.quota"}],
                }
            },
        )

    src = YouTubeApiKeySource("AIza-test", client=_mock_client(handler), on_call=bump)
    with pytest.raises(QuotaExceededError):
        src.fetch("PLtest")
    # Le compteur avance même sur 403 : Google a compté l'appel.
    assert bumps == 1


def test_403_other_reason_raises_source_error_not_quota() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "forbidden",
                    "errors": [{"reason": "keyInvalid"}],
                }
            },
        )

    src = YouTubeApiKeySource("AIza-test", client=_mock_client(handler))
    with pytest.raises(SourceError) as exc_info:
        src.fetch("PLtest")
    assert not isinstance(exc_info.value, QuotaExceededError)
    assert "keyInvalid" in str(exc_info.value)


def test_404_raises_playlist_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"errors": [{"reason": "playlistNotFound"}]}})

    src = YouTubeApiKeySource("AIza-test", client=_mock_client(handler))
    with pytest.raises(PlaylistNotFoundError, match="PLmissing"):
        src.fetch("PLmissing")


def test_deleted_and_private_items_are_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    _item("v1", "Vidéo utile", "2026-09-01T10:00:00Z"),
                    {
                        "snippet": {"title": "Deleted video"},
                        "contentDetails": {"videoId": "vDEL"},
                    },
                    {
                        "snippet": {"title": "Private video"},
                        "contentDetails": {"videoId": "vPRIV"},
                    },
                ]
            },
        )

    src = YouTubeApiKeySource("AIza-test", client=_mock_client(handler))
    videos = src.fetch("PLtest")
    assert [v.video_id for v in videos] == ["v1"]


def test_empty_api_key_is_refused() -> None:
    with pytest.raises(SourceError, match="YOUTUBE_API_KEY"):
        YouTubeApiKeySource("")


def test_url_contains_api_and_key_param() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"items": []})

    src = YouTubeApiKeySource("AIza-SECRET", client=_mock_client(handler))
    src.fetch("PLtest")
    assert captured[0].startswith(PLAYLIST_ITEMS_URL)
    assert "key=AIza-SECRET" in captured[0]
