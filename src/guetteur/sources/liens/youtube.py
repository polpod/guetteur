"""Lecteur YouTube hors playlist.

Pour une URL YouTube partagée au bot (vidéo isolée, pas d'une playlist suivie),
on n'extrait pas de contenu nous-mêmes : on délègue au pipeline vidéo existant
qui a déjà toute l'infrastructure (transcript, chapitres, résumé horodaté).

Le lecteur retourne un `LinkContent` « marqueur » : titre + URL seulement,
`text` vide, extras `delegate=video_pipeline`. Le dispatcher du Lot 8 le
reconnaît et route vers `Pipeline` plutôt que vers `summarize/link.py`.

Pour l'instant on se contente de l'URL et du video_id. L'enrichissement
métadonnées (titre réel, chaîne) viendra à la lecture pipeline.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from guetteur.items import LinkContent
from guetteur.sources.liens.net import LinkFetchError

_SHORT_ID_RE = re.compile(r"^/([A-Za-z0-9_-]{6,})/?$")
_EMBED_RE = re.compile(r"^/embed/([A-Za-z0-9_-]{6,})/?$")
_WATCH_PATHS = {"/watch", "/live", "/shorts"}


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        m = _SHORT_ID_RE.match(parsed.path)
        if m:
            return m.group(1)
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            vid = parse_qs(parsed.query).get("v", [""])[0]
            if vid:
                return vid
        for prefix in ("/shorts/", "/live/", "/embed/"):
            if parsed.path.startswith(prefix):
                remainder = parsed.path[len(prefix) :].strip("/").split("/", 1)[0]
                if remainder:
                    return remainder
        m2 = _EMBED_RE.match(parsed.path)
        if m2:
            return m2.group(1)
    raise LinkFetchError(f"URL YouTube sans video_id : {url}")


class YouTubeReader:
    def read(self, url: str) -> LinkContent:
        video_id = extract_video_id(url)
        return LinkContent(
            url=url,
            kind="youtube_oneshot",
            title=f"Vidéo YouTube {video_id}",
            author="",
            published_at=None,
            text="",
            extras={"delegate": "video_pipeline", "video_id": video_id},
        )
