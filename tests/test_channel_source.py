"""Tests unitaires de sources/channel.py (Lot 7).

Couvre : résolution d'URL de chaîne (@handle, /channel/UC…, /c/name, id nu),
filtres durée/date/shorts, conversion PT#H#M#S → secondes, tri chronologique."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from guetteur.models import Video
from guetteur.sources.base import SourceError
from guetteur.sources.channel import (
    SHORTS_MAX_DURATION_S,
    ChannelFilters,
    ChannelResolver,
    ChannelVideoLister,
    _passes_filters,
    parse_iso8601_duration,
    uploads_playlist_id,
)


class FakeHttp:
    """Client httpx factice : renvoie les réponses en file, en enregistrant les appels."""

    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        req = httpx.Request("GET", url, params=params)
        return httpx.Response(status_code=200, json=item, request=req)


def _channel_snippet(cid: str, title: str = "Titre chaîne", handle: str = "") -> dict[str, Any]:
    return {"items": [{"id": cid, "snippet": {"title": title, "customUrl": handle}}]}


def test_uploads_playlist_id_swaps_prefix() -> None:
    assert uploads_playlist_id("UCabcdefghijklmnopqrstuv") == "UUabcdefghijklmnopqrstuv"


def test_uploads_playlist_id_rejects_non_uc() -> None:
    with pytest.raises(SourceError):
        uploads_playlist_id("PLnotUC")


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("PT10M", 600),
        ("P0D", 0),
        ("", 0),
        ("garbage", 0),
    ],
)
def test_parse_iso8601_duration(raw: str, seconds: int) -> None:
    assert parse_iso8601_duration(raw) == seconds


def test_resolver_handles_bare_channel_id() -> None:
    cid = "UCabcdefghijklmnopqrstuv"
    http = FakeHttp([_channel_snippet(cid, "Alpha")])
    info = ChannelResolver("KEY", client=http).resolve(cid)  # type: ignore[arg-type]
    assert info.channel_id == cid
    assert info.uploads_playlist_id == uploads_playlist_id(cid)
    assert info.title == "Alpha"
    # 1 seul appel — pas de search.list.
    assert len(http.calls) == 1
    assert http.calls[0][1]["id"] == cid


def test_resolver_handles_channel_url() -> None:
    cid = "UCabcdefghijklmnopqrstuv"
    url = f"https://www.youtube.com/channel/{cid}"
    http = FakeHttp([_channel_snippet(cid)])
    info = ChannelResolver("KEY", client=http).resolve(url)  # type: ignore[arg-type]
    assert info.channel_id == cid


def test_resolver_handles_handle_via_for_handle() -> None:
    cid = "UCzzzzzzzzzzzzzzzzzzzzzz"
    http = FakeHttp([_channel_snippet(cid, "Handle Chan", "handle_x")])
    info = ChannelResolver("KEY", client=http).resolve("@handle_x")  # type: ignore[arg-type]
    assert info.channel_id == cid
    assert info.handle == "handle_x"
    # forHandle en paramètre.
    assert http.calls[0][1]["forHandle"] == "handle_x"


def test_resolver_handles_full_handle_url() -> None:
    cid = "UCaaaaaaaaaaaaaaaaaaaaaa"
    http = FakeHttp([_channel_snippet(cid, "H")])
    info = ChannelResolver("KEY", client=http).resolve(  # type: ignore[arg-type]
        "https://www.youtube.com/@someHandle"
    )
    assert info.channel_id == cid


def test_resolver_legacy_username_first_then_search() -> None:
    cid = "UCbbbbbbbbbbbbbbbbbbbbbb"
    # 1) forUsername renvoie du vide → fallback search.list (channelId) → channels.list id.
    http = FakeHttp(
        [
            {"items": []},
            {"items": [{"id": {"channelId": cid}}]},
            _channel_snippet(cid, "Legacy"),
        ]
    )
    info = ChannelResolver("KEY", client=http).resolve(  # type: ignore[arg-type]
        "https://www.youtube.com/c/legacyName"
    )
    assert info.channel_id == cid
    # 3 appels dans l'ordre : channels(forUsername), search, channels(id).
    assert [c[0].split("/")[-1] for c in http.calls] == ["channels", "search", "channels"]


def test_resolver_raises_on_unknown_url_shape() -> None:
    http = FakeHttp([])
    with pytest.raises(SourceError, match="non reconnue"):
        ChannelResolver("KEY", client=http).resolve(  # type: ignore[arg-type]
            "https://example.com/pas-youtube"
        )


# --- filtres ---------------------------------------------------------------------


def _video(vid: str = "v" * 11, title: str = "Titre", published: datetime | None = None) -> Video:
    return Video(vid, title, "", published, f"https://youtu.be/{vid}")


def test_filters_reject_shorts_by_default() -> None:
    v = _video()
    assert _passes_filters(v, SHORTS_MAX_DURATION_S, ChannelFilters()) is False
    assert _passes_filters(v, SHORTS_MAX_DURATION_S + 1, ChannelFilters()) is True


def test_filters_include_shorts_when_flag_is_set() -> None:
    v = _video()
    assert _passes_filters(v, 30, ChannelFilters(include_shorts=True)) is True


def test_filters_reject_hash_shorts_in_title() -> None:
    v = _video(title="Truc #shorts")
    assert _passes_filters(v, 300, ChannelFilters()) is False


def test_filters_apply_min_max_duration() -> None:
    v = _video()
    assert _passes_filters(v, 300, ChannelFilters(min_duration_s=600)) is False
    assert _passes_filters(v, 700, ChannelFilters(min_duration_s=600)) is True
    assert _passes_filters(v, 8000, ChannelFilters(max_duration_s=3600)) is False


def test_filters_apply_since_until() -> None:
    v = _video(published=datetime(2026, 6, 1, tzinfo=UTC))
    assert (
        _passes_filters(v, 3600, ChannelFilters(since=datetime(2026, 7, 1, tzinfo=UTC)))
        is False
    )
    assert (
        _passes_filters(v, 3600, ChannelFilters(until=datetime(2026, 5, 1, tzinfo=UTC)))
        is False
    )


# --- lister ---------------------------------------------------------------------


class DummySource:
    """Remplace YouTubeApiKeySource.fetch pour tester le lister sans réseau."""

    def __init__(self, videos: list[Video]) -> None:
        self._videos = videos

    def fetch(self, playlist_id: str) -> list[Video]:
        return list(self._videos)


def test_lister_sorts_chronologically_and_caps_max_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    d1 = datetime(2026, 1, 1, tzinfo=UTC)
    d2 = datetime(2026, 6, 1, tzinfo=UTC)
    d3 = datetime(2026, 12, 1, tzinfo=UTC)
    # L'API renvoie récent → ancien ; on veut ancien → récent en sortie.
    raw = [
        _video("aaaaaaaaaaa", "Récente", d3),
        _video("bbbbbbbbbbb", "Mi", d2),
        _video("ccccccccccc", "Vieille", d1),
    ]
    lister = ChannelVideoLister("KEY", client=FakeHttp([{"items": []}]))  # type: ignore[arg-type]
    lister._source = DummySource(raw)  # type: ignore[assignment]

    def fake_durations(video_ids: list[str]) -> dict[str, int | None]:
        return dict.fromkeys(video_ids, 900)

    monkeypatch.setattr(lister, "_durations", fake_durations)
    from guetteur.sources.channel import ChannelInfo

    info = ChannelInfo(channel_id="UC" + "x" * 22, uploads_playlist_id="UU" + "x" * 22, title="C")
    result = lister.list_videos(info, ChannelFilters(max_videos=2))
    # Tri chronologique croissant + coupe à 2.
    assert [v.video_id for v, _d in result] == ["ccccccccccc", "bbbbbbbbbbb"]
