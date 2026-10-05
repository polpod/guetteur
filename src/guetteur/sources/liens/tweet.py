"""Lecteur Twitter/X : fxtwitter → vxtwitter en secours.

Les deux miroirs exposent la même API JSON publique (pas de clé) qui résout
les t.co, inclut l'alt text des médias, le tweet cité et le fil de l'auteur.
Format fxtwitter 2024 : { "code": 200, "tweet": { … } }.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from guetteur.items import LinkContent
from guetteur.sources.liens.net import LinkFetchError, safe_json

log = logging.getLogger(__name__)

FXTWITTER = "https://api.fxtwitter.com"
VXTWITTER = "https://api.vxtwitter.com"


_TWEET_URL_RE = re.compile(r"^/(?P<user>[^/]+)/status/(?P<id>\d+)")


def _parse_tweet_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    m = _TWEET_URL_RE.match(parsed.path)
    if not m:
        raise LinkFetchError(f"URL de tweet non reconnue : {url}")
    return m.group("user"), m.group("id")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_tweet(block: dict[str, Any]) -> list[str]:
    """Formatte un tweet (ou un maillon de fil) en lignes de texte lisibles."""
    author = block.get("author", {}) or {}
    user = (
        author.get("screen_name") or author.get("username") or author.get("name") or "?"
    )
    text = str(block.get("text", "")).strip()
    lines = [f"@{user} : {text}" if text else f"@{user}"]
    for media in block.get("media", {}).get("all", []) or []:
        alt = media.get("altText") or media.get("alt_text") or ""
        kind = media.get("type", "media")
        lines.append(f"  [{kind}] {alt}".rstrip())
    quoted = block.get("quote") or block.get("quoted_tweet")
    if isinstance(quoted, dict):
        lines.append("  ↳ Cité :")
        for sub in _format_tweet(quoted):
            lines.append(f"    {sub}")
    return lines


def _tweet_from_payload(payload: dict[str, Any], url: str) -> LinkContent:
    tweet = payload.get("tweet") or payload.get("data")
    if not isinstance(tweet, dict):
        raise LinkFetchError(f"Payload tweet vide ou inattendu pour {url}")
    author = tweet.get("author", {}) or {}
    author_handle = author.get("screen_name") or author.get("username") or ""
    author_name = author.get("name") or author_handle
    published = _parse_iso(tweet.get("created_at") or tweet.get("date"))

    chunks = _format_tweet(tweet)
    thread_count = 0
    for extra in tweet.get("thread", []) or []:
        if isinstance(extra, dict):
            chunks.append("---")
            chunks.extend(_format_tweet(extra))
            thread_count += 1
    text = "\n".join(chunks)

    title = tweet.get("text") or ""
    first_line = title.strip().splitlines()[0] if title.strip() else ""
    title_short = (first_line[:120] + "…") if len(first_line) > 120 else first_line
    if not title_short:
        title_short = f"Tweet de @{author_handle}"

    extras: dict[str, str] = {}
    if thread_count:
        extras["thread_tweets"] = str(thread_count + 1)
    if tweet.get("quote"):
        extras["has_quote"] = "true"

    return LinkContent(
        url=url,
        kind="tweet",
        title=title_short,
        author=f"@{author_handle}" if author_handle else author_name,
        published_at=published,
        text=text,
        extras=extras,
    )


class TweetReader:
    def __init__(self, fx: str = FXTWITTER, vx: str = VXTWITTER) -> None:
        self._fx = fx.rstrip("/")
        self._vx = vx.rstrip("/")

    def read(self, url: str) -> LinkContent:
        user, tweet_id = _parse_tweet_url(url)
        last_err: Exception | None = None
        for base in (self._fx, self._vx):
            api_url = f"{base}/{user}/status/{tweet_id}"
            try:
                payload = safe_json(api_url)
            except LinkFetchError as exc:
                last_err = exc
                log.info("tweet.reader.fallback", extra={"base": base, "error": str(exc)})
                continue
            if not isinstance(payload, dict):
                last_err = LinkFetchError(f"Payload non-dict depuis {api_url}")
                continue
            code = payload.get("code")
            if code not in (None, 200):
                last_err = LinkFetchError(f"{api_url} : code {code}")
                continue
            return _tweet_from_payload(payload, url)
        raise LinkFetchError(
            f"Tweet introuvable (fxtwitter puis vxtwitter) : {last_err}"
        ) from last_err
