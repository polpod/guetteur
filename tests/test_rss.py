from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from guetteur.sources.base import SourceError
from guetteur.sources.rss import FEED_URL, RssSource, parse_feed
from tests.helpers import FIXTURES


def test_parse_feed_extracts_fields() -> None:
    videos = parse_feed((FIXTURES / "playlist_feed.xml").read_bytes())

    assert [v.video_id for v in videos] == ["BBBBBBBBBB2", "AAAAAAAAAA1", "CCCCCCCCCC3"]
    first = videos[1]
    assert first.title == "Les LLM expliqués & démystifiés"
    assert first.channel == "Chaîne Alpha"
    assert first.url == "https://www.youtube.com/watch?v=AAAAAAAAAA1"
    assert first.published == datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


def test_parse_feed_sorted_most_recent_first() -> None:
    videos = parse_feed((FIXTURES / "playlist_feed.xml").read_bytes())
    dates = [v.published for v in videos]
    assert dates == sorted(dates, reverse=True)  # type: ignore[type-var]


def test_parse_empty_feed() -> None:
    xml = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
    assert parse_feed(xml) == []


def test_rss_source_queries_playlist_url() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=(FIXTURES / "playlist_feed.xml").read_bytes())

    source = RssSource(httpx.Client(transport=httpx.MockTransport(handler)))
    videos = source.fetch("PLtest123")

    assert len(videos) == 3
    assert str(seen[0]) == f"{FEED_URL}?playlist_id=PLtest123"


def test_rss_source_http_error() -> None:
    source = RssSource(httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(404))))
    with pytest.raises(SourceError):
        source.fetch("PLnope")
