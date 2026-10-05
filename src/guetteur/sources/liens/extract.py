"""Extraction d'URL depuis un message texte et détection du kind.

Les URL non http(s) sont ignorées (mailto:, tel:, javascript:, data:…). Les
suffixes de ponctuation français courants (`.`, `,`, `;`, `)`, `]`, `»`) sont
retirés en bout pour que « Regarde https://x.com/foo. » ne pollue pas l'URL."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from guetteur.items import ItemKind

# Regex volontairement permissive sur le corps, stricte sur le schéma.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}»\"'"

# Domaines reconnus. Les sous-domaines sont acceptés via endswith "." + domaine.
_TWEET_HOSTS = {"twitter.com", "www.twitter.com", "x.com", "www.x.com", "mobile.twitter.com"}
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "music.youtube.com",
}
_GITHUB_HOSTS = {"github.com", "www.github.com"}


def extract_urls(text: str) -> list[str]:
    """Retourne les URL http(s) trouvées dans `text`, dans l'ordre d'apparition,
    sans doublons. Les ponctuations finales sont nettoyées."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        raw = match.group(0)
        while raw and raw[-1] in _TRAILING:
            raw = raw[:-1]
        if not raw or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def detect_kind(url: str) -> ItemKind:
    """Détecte le type d'un lien à partir du host + path. Fallback : article."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "article"
    host = (parsed.hostname or "").lower()
    if host in _TWEET_HOSTS and _looks_like_tweet(parsed.path):
        return "tweet"
    if host in _YOUTUBE_HOSTS:
        return "youtube_oneshot"
    if host in _GITHUB_HOSTS and _looks_like_repo(parsed.path):
        return "github"
    return "article"


def _looks_like_tweet(path: str) -> bool:
    # /<user>/status/<numeric_id> éventuellement avec /photo/N ou /video/N
    parts = [p for p in path.strip("/").split("/") if p]
    return len(parts) >= 3 and parts[1] == "status" and parts[2].isdigit()


def _looks_like_repo(path: str) -> bool:
    parts = [p for p in path.strip("/").split("/") if p]
    return len(parts) >= 2 and parts[0] not in {
        "settings",
        "notifications",
        "pulls",
        "issues",
        "marketplace",
        "topics",
        "trending",
        "explore",
        "sponsors",
        "orgs",
    }
