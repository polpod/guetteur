"""Garde SSRF du client HTTP partagé."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from unittest.mock import patch

import httpx
import pytest

from guetteur.sources.liens.net import LinkFetchError, safe_fetch


def _fake_getaddrinfo(addr: str) -> callable:  # type: ignore[valid-type]
    def _f(*args, **kwargs):  # type: ignore[no-untyped-def]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0))]

    return _f


@pytest.mark.parametrize(
    "resolved",
    ["127.0.0.1", "169.254.169.254", "10.0.0.1", "192.168.1.1", "fd00::1"],
)
def test_rejects_private_and_metadata_hosts(resolved: str) -> None:
    with patch(
        "guetteur.sources.liens.net.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo(resolved),
    ), pytest.raises(LinkFetchError):
        safe_fetch("https://evil.example")


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(LinkFetchError):
        safe_fetch("ftp://example.com/file")


def test_accepts_public_host_and_returns_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, follow_redirects=False)
    with patch(
        "guetteur.sources.liens.net.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo("93.184.216.34"),
    ):
        result = safe_fetch("https://example.com/x", client=client)
    assert result.status_code == 200
    assert result.text == "hello"


def test_redirect_revalidated_each_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    # Première validation OK (public), redirection vers loopback refusée.
    seq: Iterator[str] = iter(["93.184.216.34", "127.0.0.1"])

    def gai(*args, **kwargs):  # type: ignore[no-untyped-def]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (next(seq), 0))]

    with patch(
        "guetteur.sources.liens.net.socket.getaddrinfo", side_effect=gai
    ), pytest.raises(LinkFetchError):
        safe_fetch("https://example.com/go", client=client)


def test_size_cap_enforced() -> None:
    big = "x" * (3 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big, headers={"content-type": "text/plain"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with patch(
        "guetteur.sources.liens.net.socket.getaddrinfo",
        side_effect=_fake_getaddrinfo("93.184.216.34"),
    ), pytest.raises(LinkFetchError, match="plafond"):
        safe_fetch("https://example.com/big", max_bytes=1024, client=client)
