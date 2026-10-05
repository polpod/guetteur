"""Lecteur article : extraction stdlib, paywall détecté."""

from __future__ import annotations

import socket
from unittest.mock import patch

import httpx

from guetteur.sources.liens.article import ArticleReader


def _gai(*args, **kwargs):  # type: ignore[no-untyped-def]
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


ARTICLE_HTML = """\
<html>
<head>
  <title>Un article intéressant</title>
  <meta property="article:author" content="Jane Doe"/>
  <meta property="article:published_time" content="2026-02-01T09:00:00Z"/>
</head>
<body>
<nav>menu</nav>
<article>
<h1>Un article intéressant</h1>
<p>Premier paragraphe du corps.</p>
<p>Second paragraphe un peu plus long qui parle de choses importantes.</p>
<script>var x=1</script>
</article>
<footer>pied de page</footer>
</body></html>
"""


def test_article_reader_extracts_main_and_meta() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=ARTICLE_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_fetch(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_fetch as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.article.safe_fetch", side_effect=fake_safe_fetch
    ):
        content = ArticleReader().read("https://example.com/post")
    assert content.kind == "article"
    assert content.title.startswith("Un article intéressant")
    assert content.author == "Jane Doe"
    assert "Premier paragraphe" in content.text
    assert "Second paragraphe" in content.text
    assert "menu" not in content.text
    assert "pied de page" not in content.text


PAYWALL_HTML = """\
<html><head><title>Article payant</title>
<meta name="description" content="Résumé public." />
<script type="application/ld+json">{"isAccessibleForFree": false}</script>
</head><body><article><p>...</p></article></body></html>
"""


def test_paywall_detection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=PAYWALL_HTML, headers={"content-type": "text/html"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_fetch(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_fetch as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.article.safe_fetch", side_effect=fake_safe_fetch
    ):
        content = ArticleReader().read("https://premium.example/post")
    assert content.extras.get("paywall") == "true"
    assert "Résumé public" in content.text


def test_truncation_at_50k() -> None:
    long_body = "paragraphe. " * 10_000
    html = f"<html><body><article>{long_body}</article></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_fetch(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_fetch as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.article.safe_fetch", side_effect=fake_safe_fetch
    ):
        content = ArticleReader().read("https://long.example/post")
    assert content.extras.get("truncated") == "true"
    assert len(content.text) < 51_000
