"""Lecteur tweet : fxtwitter → vxtwitter en secours, fil, cité."""

from __future__ import annotations

import socket
from unittest.mock import patch

import httpx
import pytest

from guetteur.sources.liens.net import LinkFetchError
from guetteur.sources.liens.tweet import TweetReader


def _gai(*args, **kwargs):  # type: ignore[no-untyped-def]
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


FXTW_TWEET = {
    "code": 200,
    "tweet": {
        "author": {"screen_name": "foo", "name": "Foo"},
        "text": "Hello world",
        "created_at": "2026-01-02T10:00:00Z",
        "media": {
            "all": [{"type": "photo", "altText": "Chat sur un piano"}],
        },
    },
}


def test_tweet_reader_fxtwitter_simple() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "fxtwitter" in request.url.host
        return httpx.Response(
            200, json=FXTW_TWEET, headers={"content-type": "application/json"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_json(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_json as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.tweet.safe_json", side_effect=fake_safe_json
    ):
        content = TweetReader().read("https://x.com/foo/status/1234567890")
    assert content.kind == "tweet"
    assert "Hello world" in content.text
    assert "Chat sur un piano" in content.text
    assert content.author == "@foo"


def test_tweet_reader_falls_back_to_vxtwitter() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if "fxtwitter" in request.url.host:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json=FXTW_TWEET, headers={"content-type": "application/json"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_json(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_json as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.tweet.safe_json", side_effect=fake_safe_json
    ):
        content = TweetReader().read("https://x.com/foo/status/42")
    assert any("fxtwitter" in c for c in calls)
    assert any("vxtwitter" in c for c in calls)
    assert "Hello world" in content.text


def test_tweet_reader_with_thread_and_quote() -> None:
    payload = {
        "code": 200,
        "tweet": {
            "author": {"screen_name": "foo"},
            "text": "Head",
            "created_at": "2026-01-01T00:00:00Z",
            "thread": [
                {"author": {"screen_name": "foo"}, "text": "Tweet 2"},
                {"author": {"screen_name": "foo"}, "text": "Tweet 3"},
            ],
            "quote": {
                "author": {"screen_name": "bar"},
                "text": "Original",
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_json(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_json as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.tweet.safe_json", side_effect=fake_safe_json
    ):
        content = TweetReader().read("https://x.com/foo/status/42")
    assert "Head" in content.text
    assert "Tweet 2" in content.text
    assert "Tweet 3" in content.text
    assert "Cité" in content.text
    assert "Original" in content.text
    assert content.extras.get("has_quote") == "true"
    assert content.extras.get("thread_tweets") == "3"


def test_tweet_reader_bad_url() -> None:
    with pytest.raises(LinkFetchError):
        TweetReader().read("https://x.com/foo")
