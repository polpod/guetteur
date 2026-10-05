"""URL extraction + kind detection."""

from __future__ import annotations

from guetteur.sources.liens.extract import detect_kind, extract_urls


def test_extract_urls_strips_trailing_punctuation() -> None:
    text = "Regarde https://x.com/foo/status/1234567. Et aussi (https://example.com/a)."
    urls = extract_urls(text)
    assert urls == ["https://x.com/foo/status/1234567", "https://example.com/a"]


def test_extract_urls_dedup_and_skip_non_http() -> None:
    text = "mailto:a@b, javascript:x, http://a.com, http://a.com"
    assert extract_urls(text) == ["http://a.com"]


def test_extract_empty_and_none() -> None:
    assert extract_urls("") == []
    assert extract_urls("rien d'intéressant") == []


def test_detect_kind_tweet() -> None:
    assert detect_kind("https://x.com/elonmusk/status/1234567890") == "tweet"
    assert detect_kind("https://twitter.com/foo/status/42/photo/1") == "tweet"
    # pas de status → article
    assert detect_kind("https://x.com/elonmusk") == "article"


def test_detect_kind_github() -> None:
    assert detect_kind("https://github.com/anthropics/anthropic-sdk-python") == "github"
    # /settings n'est pas un repo
    assert detect_kind("https://github.com/settings") == "article"


def test_detect_kind_youtube() -> None:
    assert detect_kind("https://youtu.be/abc12345") == "youtube_oneshot"
    assert detect_kind("https://www.youtube.com/watch?v=abc12345") == "youtube_oneshot"
    assert detect_kind("https://www.youtube.com/shorts/xyz1234") == "youtube_oneshot"


def test_detect_kind_fallback_article() -> None:
    assert detect_kind("https://blog.example.com/post") == "article"
