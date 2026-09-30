"""Résolution d'une chaîne YouTube en id + listing complet de ses uploads.

Utilisé par Lot 7 (`guetteur livre`) : à partir d'une URL variée (@handle,
/channel/UC…, /c/name, /user/name) on retrouve le `channelId` UC…, on convertit
en playlist uploads UU… (le préfixe C devient U), on liste avec pagination
complète, et on filtre par durée/date/shorts. Toutes les requêtes passent par
`YouTubeApiKeySource` pour respecter le compteur de quota existant.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from guetteur.models import Video
from guetteur.sources.api import YouTubeApiKeySource, _parse_datetime
from guetteur.sources.base import SourceError, video_url

log = logging.getLogger(__name__)

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

# Régexes des URLs YouTube reconnues. `/@handle` est la forme moderne, `/channel/UC…`
# la plus fiable (id direct), `/c/name` et `/user/name` sont des alias résolus via
# channels.list forHandle ou search (fallback).
_RE_CHANNEL_ID = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"
)
_RE_HANDLE = re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/@([A-Za-z0-9_.-]+)")
_RE_LEGACY = re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/(?:c|user)/([A-Za-z0-9_.-]+)")
_RE_BARE_HANDLE = re.compile(r"^@([A-Za-z0-9_.-]+)$")
_RE_BARE_CHANNEL = re.compile(r"^(UC[A-Za-z0-9_-]{22})$")

# Un « short » YouTube = vidéo verticale de moins de 60 s côté API, plus
# éventuellement `#shorts` dans le titre. Sans champ dédié dans `videos.list`,
# on utilise la durée comme heuristique — pris en compte SEULEMENT si l'utilisateur
# a passé `--include-shorts` (par défaut on les exclut).
SHORTS_MAX_DURATION_S = 60


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    uploads_playlist_id: str
    title: str
    handle: str = ""


@dataclass(frozen=True)
class ChannelFilters:
    """Filtres appliqués côté client sur la liste des uploads."""

    min_duration_s: int | None = None
    max_duration_s: int | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_shorts: bool = False
    max_videos: int | None = None


def uploads_playlist_id(channel_id: str) -> str:
    """UC…xyz → UU…xyz (playlist d'uploads systématique côté YouTube)."""
    if not channel_id.startswith("UC"):
        raise SourceError(f"channel_id inattendu : {channel_id!r}")
    return "UU" + channel_id[2:]


class _HttpGetter(Protocol):
    def get(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:  # pragma: no cover
        ...


class ChannelResolver:
    """Résout une URL de chaîne en `ChannelInfo`. Utilise l'API `channels.list`
    (1 unité de quota) puis, en dernier recours pour les alias `/c/` ou `/user/`
    non reconnus, `search.list` (100 unités — coûteux, à éviter si possible).

    Le compteur de quota YouTube existant (store.youtube_quota_bump) est
    incrémenté par l'appelant via `on_call` — mêmes règles que YouTubeApiKeySource,
    pour rester alignés avec le tableau de bord `guetteur status`."""

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        on_call: Any = None,
    ) -> None:
        if not api_key:
            raise SourceError("YOUTUBE_API_KEY vide : résolution de chaîne impossible")
        self._api_key = api_key
        self._client: _HttpGetter = client or httpx.Client(timeout=20.0)
        self._on_call = on_call or (lambda: None)

    def resolve(self, url_or_handle: str) -> ChannelInfo:
        raw = url_or_handle.strip()
        # 1. UC… déjà donné : un seul appel channels.list pour récupérer le titre.
        m = _RE_CHANNEL_ID.search(raw) or _RE_BARE_CHANNEL.match(raw)
        if m:
            return self._by_channel_id(m.group(1))
        # 2. @handle (URL ou nu) : channels.list forHandle — 1 unité.
        m = _RE_HANDLE.search(raw) or _RE_BARE_HANDLE.match(raw)
        if m:
            return self._by_handle(m.group(1))
        # 3. /c/ ou /user/ : channels.list forUsername d'abord (gratuit sur les
        #    vieilles chaînes), puis search.list en dernier recours (100 unités).
        m = _RE_LEGACY.search(raw)
        if m:
            name = m.group(1)
            info = self._by_username(name)
            if info is not None:
                return info
            return self._by_search(name)
        raise SourceError(
            f"URL de chaîne non reconnue : {url_or_handle!r} — attendu /@handle, "
            "/channel/UC…, /c/name ou /user/name"
        )

    # --- variantes ------------------------------------------------------------

    def _by_channel_id(self, channel_id: str) -> ChannelInfo:
        data = self._call(CHANNELS_URL, {"part": "snippet", "id": channel_id})
        items = data.get("items", [])
        if not items:
            raise SourceError(f"channel_id introuvable : {channel_id}")
        snippet = items[0].get("snippet", {})
        return ChannelInfo(
            channel_id=channel_id,
            uploads_playlist_id=uploads_playlist_id(channel_id),
            title=str(snippet.get("title", "")),
            handle=str(snippet.get("customUrl", "")).lstrip("@"),
        )

    def _by_handle(self, handle: str) -> ChannelInfo:
        data = self._call(CHANNELS_URL, {"part": "snippet", "forHandle": handle})
        items = data.get("items", [])
        if not items:
            raise SourceError(f"handle @{handle} introuvable via channels.list forHandle")
        item = items[0]
        cid = str(item.get("id", ""))
        if not cid.startswith("UC"):
            raise SourceError(f"channels.list a renvoyé un id inattendu : {cid!r}")
        snippet = item.get("snippet", {})
        return ChannelInfo(
            channel_id=cid,
            uploads_playlist_id=uploads_playlist_id(cid),
            title=str(snippet.get("title", "")),
            handle=handle,
        )

    def _by_username(self, username: str) -> ChannelInfo | None:
        data = self._call(CHANNELS_URL, {"part": "snippet", "forUsername": username})
        items = data.get("items", [])
        if not items:
            return None
        item = items[0]
        cid = str(item.get("id", ""))
        if not cid.startswith("UC"):
            return None
        snippet = item.get("snippet", {})
        return ChannelInfo(
            channel_id=cid,
            uploads_playlist_id=uploads_playlist_id(cid),
            title=str(snippet.get("title", "")),
            handle=str(snippet.get("customUrl", "")).lstrip("@"),
        )

    def _by_search(self, query: str) -> ChannelInfo:
        # search.list coûte 100 unités : on ne l'utilise qu'en dernier recours,
        # avec type=channel pour n'obtenir que des chaînes en résultats.
        data = self._call(
            SEARCH_URL,
            {"part": "snippet", "type": "channel", "q": query, "maxResults": 1},
        )
        items = data.get("items", [])
        if not items:
            raise SourceError(f"aucune chaîne trouvée via search : {query!r}")
        cid = str(items[0].get("id", {}).get("channelId", ""))
        if not cid.startswith("UC"):
            raise SourceError(f"search a renvoyé un channelId inattendu : {cid!r}")
        return self._by_channel_id(cid)

    def _call(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "key": self._api_key}
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as exc:  # pragma: no cover - dépend du réseau
            raise SourceError(f"channel resolver : {exc}") from exc
        self._on_call()
        if resp.status_code != 200:
            raise SourceError(
                f"channel resolver HTTP {resp.status_code} : {resp.text[:200]}"
            )
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}


# --- durées ISO 8601 (PT#H#M#S) -----------------------------------------------


_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def parse_iso8601_duration(raw: str) -> int:
    """Convertit `PT1H2M3S` en secondes. Retourne 0 si le format est illisible —
    plus sûr que de lever : les vidéos live sans durée renvoient parfois `P0D`."""
    if not raw:
        return 0
    match = _ISO_DURATION_RE.match(raw)
    if not match:
        return 0
    d = int(match.group("d") or 0)
    h = int(match.group("h") or 0)
    m = int(match.group("m") or 0)
    s = int(match.group("s") or 0)
    return d * 86400 + h * 3600 + m * 60 + s


# --- listing complet des uploads ---------------------------------------------


class ChannelVideoLister:
    """Liste les uploads d'une chaîne avec pagination complète, puis enrichit
    chaque vidéo avec `contentDetails.duration` (par lots de 50 ids : 1 unité par
    lot). Filtres appliqués côté client à partir de `ChannelFilters`."""

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        on_call: Any = None,
    ) -> None:
        if not api_key:
            raise SourceError("YOUTUBE_API_KEY vide : listing de chaîne impossible")
        self._api_key = api_key
        self._client: _HttpGetter = client or httpx.Client(timeout=20.0)
        self._on_call = on_call or (lambda: None)
        # Réutilise YouTubeApiKeySource pour la pagination (max_items élevé pour
        # ne pas la couper avant le filtre client).
        self._source = YouTubeApiKeySource(
            api_key, client=client, max_items=10_000, on_call=on_call
        )

    def list_videos(
        self, channel: ChannelInfo, filters: ChannelFilters
    ) -> list[tuple[Video, int | None]]:
        """Retourne des (Video, duration_s) triés du plus ancien au plus récent —
        ordre de lecture naturel pour la compilation en livre. `duration_s` peut
        être None si la vidéo n'a pas de durée (live en cours)."""
        raw_videos = self._source.fetch(channel.uploads_playlist_id)
        durations = self._durations([v.video_id for v in raw_videos])
        keep: list[tuple[Video, int | None]] = []
        for v in raw_videos:
            duration = durations.get(v.video_id)
            if not _passes_filters(v, duration, filters):
                continue
            keep.append((v, duration))
        # Tri chronologique (ancien → récent) : le lecteur du livre attend
        # l'ordre naturel de publication ; l'API renvoie récent → ancien.
        keep.sort(key=lambda pair: pair[0].published.timestamp() if pair[0].published else 0.0)
        if filters.max_videos is not None:
            keep = keep[: filters.max_videos]
        log.info(
            "channel.listed",
            extra={
                "channel_id": channel.channel_id,
                "raw": len(raw_videos),
                "kept": len(keep),
            },
        )
        return keep

    def _durations(self, video_ids: list[str]) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for chunk_start in range(0, len(video_ids), 50):
            chunk = video_ids[chunk_start : chunk_start + 50]
            params: dict[str, Any] = {
                "part": "contentDetails",
                "id": ",".join(chunk),
                "maxResults": 50,
                "key": self._api_key,
            }
            try:
                resp = self._client.get(VIDEOS_URL, params=params)
            except httpx.HTTPError as exc:  # pragma: no cover
                raise SourceError(f"videos.list : {exc}") from exc
            self._on_call()
            if resp.status_code != 200:
                raise SourceError(
                    f"videos.list HTTP {resp.status_code} : {resp.text[:200]}"
                )
            data = resp.json() if isinstance(resp.json(), dict) else {}
            for item in data.get("items", []):
                vid = str(item.get("id", ""))
                details = item.get("contentDetails", {})
                dur_raw = str(details.get("duration", ""))
                seconds = parse_iso8601_duration(dur_raw)
                out[vid] = seconds or None
        return out


def _passes_filters(
    video: Video, duration_s: int | None, filters: ChannelFilters
) -> bool:
    if not filters.include_shorts:
        if duration_s is not None and duration_s <= SHORTS_MAX_DURATION_S:
            return False
        if "#shorts" in video.title.lower():
            return False
    if filters.min_duration_s is not None and (
        duration_s is None or duration_s < filters.min_duration_s
    ):
        return False
    if filters.max_duration_s is not None and (
        duration_s is None or duration_s > filters.max_duration_s
    ):
        return False
    if filters.since is not None and (
        video.published is None or video.published < filters.since
    ):
        return False
    return not (
        filters.until is not None
        and (video.published is None or video.published > filters.until)
    )


# --- Rétro-compatibilité : helpers utilisables par la CLI livre --------------


def resolve_channel(
    api_key: str,
    url_or_handle: str,
    client: httpx.Client | None = None,
    on_call: Any = None,
) -> ChannelInfo:
    return ChannelResolver(api_key, client=client, on_call=on_call).resolve(url_or_handle)


def list_channel_videos(
    api_key: str,
    channel: ChannelInfo,
    filters: ChannelFilters,
    client: httpx.Client | None = None,
    on_call: Any = None,
) -> list[tuple[Video, int | None]]:
    return ChannelVideoLister(api_key, client=client, on_call=on_call).list_videos(
        channel, filters
    )


# Ré-exporte parse_datetime pour les tests
__all__ = [
    "SHORTS_MAX_DURATION_S",
    "ChannelFilters",
    "ChannelInfo",
    "ChannelResolver",
    "ChannelVideoLister",
    "_parse_datetime",
    "list_channel_videos",
    "parse_iso8601_duration",
    "resolve_channel",
    "uploads_playlist_id",
    "video_url",
]
