"""Lecteur GitHub : description + README via l'API publique (sans clé).

L'API renvoie 60 req/h sans token : largement suffisant au volume d'un bot
Telegram. Un token facultatif en variable d'env `GITHUB_TOKEN` est reconnu
(utile pour les repos privés d'un futur usage) mais pas requis.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from guetteur.items import LinkContent
from guetteur.sources.liens.net import LinkFetchError, safe_json

GITHUB_API = "https://api.github.com"
MAX_README = 50_000


def _parse_repo(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise LinkFetchError(f"URL GitHub non reconnue : {url}")
    return parts[0], re.sub(r"\.git$", "", parts[1])


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitHubReader:
    def __init__(self, api: str = GITHUB_API) -> None:
        self._api = api.rstrip("/")

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def read(self, url: str) -> LinkContent:
        owner, repo = _parse_repo(url)
        meta = self._fetch(f"{self._api}/repos/{owner}/{repo}")
        readme = self._fetch_readme(owner, repo)

        description = str(meta.get("description") or "").strip()
        topics = meta.get("topics") or []
        stars = meta.get("stargazers_count") or 0
        language = meta.get("language") or ""
        homepage = meta.get("homepage") or ""

        header_bits = [description or "(sans description)"]
        if language:
            header_bits.append(f"Langage principal : {language}")
        if isinstance(topics, list) and topics:
            header_bits.append("Topics : " + ", ".join(str(t) for t in topics))
        if stars:
            header_bits.append(f"Stars : {stars}")
        if homepage:
            header_bits.append(f"Site : {homepage}")

        text_parts = ["\n".join(header_bits)]
        if readme:
            text_parts.append("\n---\n")
            text_parts.append(readme[:MAX_README])
            if len(readme) > MAX_README:
                text_parts.append("\n\n[README tronqué à 50 000 caractères]")
        text = "\n".join(text_parts).strip()

        return LinkContent(
            url=url,
            kind="github",
            title=f"{owner}/{repo}",
            author=str((meta.get("owner") or {}).get("login") or owner),
            published_at=_parse_iso(meta.get("pushed_at") or meta.get("updated_at")),
            text=text,
            extras={"stars": str(stars), "language": str(language)},
        )

    def _fetch(self, url: str) -> dict[str, Any]:
        data = safe_json(url, headers=self._headers())
        if not isinstance(data, dict):
            raise LinkFetchError(f"Réponse GitHub inattendue depuis {url}")
        return data

    def _fetch_readme(self, owner: str, repo: str) -> str:
        try:
            data = self._fetch(f"{self._api}/repos/{owner}/{repo}/readme")
        except LinkFetchError:
            return ""
        encoding = data.get("encoding")
        raw = data.get("content") or ""
        if encoding == "base64" and raw:
            try:
                return base64.b64decode(raw).decode("utf-8", errors="replace")
            except (ValueError, UnicodeDecodeError):
                return ""
        if isinstance(raw, str):
            return raw
        return ""
