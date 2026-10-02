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


def test_lister_default_order_date_recent_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Depuis Lot 7 §tri : `order = "date"` (défaut) → plus récentes d'abord,
    `max_videos` appliqué APRÈS le tri."""
    d1 = datetime(2026, 1, 1, tzinfo=UTC)
    d2 = datetime(2026, 6, 1, tzinfo=UTC)
    d3 = datetime(2026, 12, 1, tzinfo=UTC)
    raw = [
        _video("aaaaaaaaaaa", "Récente", d3),
        _video("bbbbbbbbbbb", "Mi", d2),
        _video("ccccccccccc", "Vieille", d1),
    ]
    lister = ChannelVideoLister("KEY", client=FakeHttp([{"items": []}]))  # type: ignore[arg-type]
    lister._source = DummySource(raw)  # type: ignore[assignment]

    def fake_stats(video_ids: list[str]) -> dict[str, tuple[int | None, int | None]]:
        return dict.fromkeys(video_ids, (900, 10))

    monkeypatch.setattr(lister, "_statistics", fake_stats)
    from guetteur.sources.channel import ChannelInfo

    info = ChannelInfo(channel_id="UC" + "x" * 22, uploads_playlist_id="UU" + "x" * 22, title="C")
    result = lister.list_videos(info, ChannelFilters(max_videos=2))
    # Récentes d'abord, coupées à 2.
    assert [v.video_id for v, _d, _vc in result] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def _make_raw_videos(n: int) -> list[Video]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        _video(
            f"vid{i:04d}aa_"[:11],
            f"Vidéo #{i}",
            published=base.replace(day=1) if i == 0 else base,
        )
        for i in range(n)
    ]


def _batched_stats_handler(
    stats: dict[str, tuple[int, int]],
) -> tuple[FakeHttp, list[tuple[str, dict[str, Any]]]]:
    """Transport factice : à chaque appel videos.list, renvoie uniquement les
    items demandés via `params["id"]`. Vérifie du coup les lots de 50 côté
    assertions appelantes."""
    calls: list[tuple[str, dict[str, Any]]] = []

    responses: list[dict[str, Any] | Exception] = []

    def _responses_for(ids: list[str]) -> dict[str, Any]:
        items = []
        for vid in ids:
            d, vc = stats.get(vid, (0, 0))
            items.append(
                {
                    "id": vid,
                    "contentDetails": {"duration": f"PT{d}S"},
                    "statistics": {"viewCount": str(vc)},
                }
            )
        return {"items": items}

    # On ne connaît pas d'avance l'ordre : patch avec un callable via sous-classe.
    class _Http(FakeHttp):
        def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
            calls.append((url, dict(params or {})))
            ids = (params or {}).get("id", "").split(",") if params else []
            req = httpx.Request("GET", url, params=params)
            return httpx.Response(200, json=_responses_for(ids), request=req)

    return _Http(responses), calls


def test_lister_sort_by_views_tops_120_videos_in_three_batches() -> None:
    """120 vidéos → 3 lots de 50/50/20 videos.list (1 unité de quota chacun).
    `order=views` renvoie les 10 plus vues en tête, max_videos=10 appliqué
    APRÈS le tri (donc on garde les 10 plus populaires, pas les 10 premiers)."""
    raw = _make_raw_videos(120)
    # Pose les vues : vidéo 0 = 1 vue, vidéo 1 = 2 vues, …, 119 = 120 vues.
    # Les plus vues sont donc à la fin de la liste brute.
    stats = {v.video_id: (300 + i, i + 1) for i, v in enumerate(raw)}
    http, calls = _batched_stats_handler(stats)
    lister = ChannelVideoLister("KEY", client=http)  # type: ignore[arg-type]
    lister._source = DummySource(raw)  # type: ignore[assignment]
    from guetteur.sources.channel import ChannelInfo

    info = ChannelInfo(channel_id="UC" + "x" * 22, uploads_playlist_id="UU" + "x" * 22, title="C")
    result = lister.list_videos(info, ChannelFilters(max_videos=10, order="views"))
    # Exactement 3 appels (50 + 50 + 20) = 3 unités de quota.
    assert len(calls) == 3
    assert [len(c[1]["id"].split(",")) for c in calls] == [50, 50, 20]
    assert all(c[1]["part"] == "contentDetails,statistics" for c in calls)
    # Les 10 plus vues (ids 110 à 119) renvoyés en tête, du plus vu au moins vu.
    assert len(result) == 10
    top_views: list[int] = [vc for _v, _d, vc in result if vc is not None]
    assert len(top_views) == 10
    assert top_views == sorted(top_views, reverse=True)
    assert top_views[0] == 120  # ex-aequo = vidéo 119
    # Les 10 vidéos renvoyées sont bien les plus populaires du lot.
    returned_ids = {v.video_id for v, _d, _vc in result}
    expected_top10 = {raw[i].video_id for i in range(110, 120)}
    assert returned_ids == expected_top10


def test_lister_sort_by_duration_tops_120_videos() -> None:
    """Même scénario, tri par durée. Les 10 plus longues en tête."""
    raw = _make_raw_videos(120)
    # Durées croissantes : vidéo 0 = 60 s, … 119 = 7260 s. Plus longues à la fin.
    stats = {v.video_id: (60 + i * 60, 100) for i, v in enumerate(raw)}
    http, calls = _batched_stats_handler(stats)
    lister = ChannelVideoLister("KEY", client=http)  # type: ignore[arg-type]
    lister._source = DummySource(raw)  # type: ignore[assignment]
    from guetteur.sources.channel import ChannelInfo

    info = ChannelInfo(channel_id="UC" + "x" * 22, uploads_playlist_id="UU" + "x" * 22, title="C")
    result = lister.list_videos(info, ChannelFilters(max_videos=10, order="duration"))
    assert len(calls) == 3
    durations: list[int] = [d for _v, d, _vc in result if d is not None]
    assert len(durations) == 10
    assert durations == sorted(durations, reverse=True)
    assert durations[0] == 60 + 119 * 60
    assert len(result) == 10
    returned_ids = {v.video_id for v, _d, _vc in result}
    expected_longest = {raw[i].video_id for i in range(110, 120)}
    assert returned_ids == expected_longest


def test_lister_max_videos_applied_after_sort() -> None:
    """Vérification explicite : si on prenait les 10 PREMIERES puis qu'on triait,
    on obtiendrait les 10 moins vues. En triant d'abord puis en coupant, on a
    bien les 10 plus vues. Même propriété pour la durée."""
    raw = _make_raw_videos(60)
    stats = {v.video_id: (300, 1_000_000 - i) for i, v in enumerate(raw)}
    http, _ = _batched_stats_handler(stats)
    lister = ChannelVideoLister("KEY", client=http)  # type: ignore[arg-type]
    lister._source = DummySource(raw)  # type: ignore[assignment]
    from guetteur.sources.channel import ChannelInfo

    info = ChannelInfo(channel_id="UC" + "x" * 22, uploads_playlist_id="UU" + "x" * 22, title="C")
    result = lister.list_videos(info, ChannelFilters(max_videos=5, order="views"))
    # Les 5 premières vidéos de `raw` sont les plus vues (viewCount décroissant).
    assert [v.video_id for v, _d, _vc in result] == [raw[i].video_id for i in range(5)]
