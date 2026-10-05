"""Lecteur GitHub mocké."""

from __future__ import annotations

import base64
import socket
from unittest.mock import patch

import httpx

from guetteur.sources.liens.github import GitHubReader


def _gai(*args, **kwargs):  # type: ignore[no-untyped-def]
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("140.82.114.5", 0))]


REPO_META = {
    "name": "awesome",
    "owner": {"login": "octo"},
    "description": "Un dépôt super intéressant.",
    "topics": ["python", "security"],
    "stargazers_count": 1234,
    "language": "Python",
    "homepage": "https://awesome.example",
    "pushed_at": "2026-05-01T12:00:00Z",
}

README_CONTENT = "# Awesome\n\nDes trucs à lire."


def test_github_reader_combines_description_and_readme() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/readme"):
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(README_CONTENT.encode()).decode(),
                },
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200, json=REPO_META, headers={"content-type": "application/json"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_json(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_json as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.github.safe_json", side_effect=fake_safe_json
    ):
        content = GitHubReader().read("https://github.com/octo/awesome")
    assert content.title == "octo/awesome"
    assert "dépôt super intéressant" in content.text
    assert "Awesome" in content.text  # README
    assert content.extras.get("stars") == "1234"
    assert content.extras.get("language") == "Python"


def test_github_reader_without_readme_still_works() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/readme"):
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200, json=REPO_META, headers={"content-type": "application/json"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def fake_safe_json(url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("client", client)
        from guetteur.sources.liens.net import safe_json as real

        return real(url, **kwargs)

    with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=_gai), patch(
        "guetteur.sources.liens.github.safe_json", side_effect=fake_safe_json
    ):
        content = GitHubReader().read("https://github.com/octo/awesome.git")
    assert content.title == "octo/awesome"
    assert "dépôt super intéressant" in content.text
