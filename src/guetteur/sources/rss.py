"""Source RSS : https://www.youtube.com/feeds/videos.xml?playlist_id=ID (playlists publiques ou
non répertoriées). YouTube ne renvoie que les 15 entrées les plus récentes."""

from __future__ import annotations

import logging
from calendar import timegm
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from guetteur.models import Video
from guetteur.sources.base import SourceError, video_url

log = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml"


def parse_feed(content: bytes | str) -> list[Video]:
    feed: Any = feedparser.parse(content)
    videos: list[Video] = []
    for entry in feed.entries:
        video_id = entry.get("yt_videoid")
        if not video_id:
            continue
        published = None
        parsed = entry.get("published_parsed")
        if parsed is not None:
            published = datetime.fromtimestamp(timegm(parsed), tz=UTC)
        videos.append(
            Video(
                video_id=str(video_id),
                title=str(entry.get("title", "")),
                channel=str(entry.get("author", "")),
                published=published,
                url=str(entry.get("link") or video_url(video_id)),
            )
        )
    videos.sort(key=lambda v: v.published or datetime.min.replace(tzinfo=UTC), reverse=True)
    return videos


class RssSource:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=True)

    def fetch(self, playlist_id: str) -> list[Video]:
        try:
            resp = self._client.get(FEED_URL, params={"playlist_id": playlist_id})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError(f"Flux RSS inaccessible pour {playlist_id} : {exc}") from exc
        videos = parse_feed(resp.content)
        log.debug("rss.fetched", extra={"playlist_id": playlist_id, "count": len(videos)})
        return videos
