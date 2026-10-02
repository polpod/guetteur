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
from typing import Any, Literal, Protocol

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


# Clé de tri appliquée après filtrage et AVANT la coupe `max_videos`.
# - `date` : plus récentes d'abord (ordre naturel pour une veille).
# - `views` : plus vues d'abord (classement par popularité).
# - `duration` : plus longues d'abord (sélection des formats longs).
VideoOrder = Literal["date", "views", "duration"]
VIDEO_ORDERS: tuple[VideoOrder, ...] = ("date", "views", "duration")


@dataclass(frozen=True)
class ChannelFilters:
    """Filtres appliqués côté client sur la liste des uploads."""

    min_duration_s: int | None = None
    max_duration_s: int | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_shorts: bool = False
    max_videos: int | None = None
    order: VideoOrder = "date"


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
    chaque vidéo avec `contentDetails.duration` et `statistics.viewCount`
    (par lots de 50 ids : 1 unité de quota par lot). Filtres et tri appliqués
    côté client à partir de `ChannelFilters`."""

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
    ) -> list[tuple[Video, int | None, int | None]]:
        """Retourne des (Video, duration_s, view_count) ordonnés selon
        `filters.order` (desc) : plus récentes / plus vues / plus longues
        d'abord. `max_videos` est appliqué APRÈS le tri, pour garder le top-N
        selon l'intention de l'utilisateur. `duration_s` et `view_count`
        peuvent être None (live en cours, compteur de vues désactivé)."""
        raw_videos = self._source.fetch(channel.uploads_playlist_id)
        stats = self._statistics([v.video_id for v in raw_videos])
        keep: list[tuple[Video, int | None, int | None]] = []
        for v in raw_videos:
            duration, views = stats.get(v.video_id, (None, None))
            if not _passes_filters(v, duration, filters):
                continue
            keep.append((v, duration, views))
        # Tri par la clé choisie puis coupe à max_videos. L'ordre de tri est
        # desc pour les trois clés : récent / le plus vu / le plus long d'abord.
        keep.sort(key=_sort_key(filters.order), reverse=True)
        if filters.max_videos is not None:
            keep = keep[: filters.max_videos]
        log.info(
            "channel.listed",
            extra={
                "channel_id": channel.channel_id,
                "raw": len(raw_videos),
                "kept": len(keep),
                "order": filters.order,
            },
        )
        return keep

    def _statistics(
        self, video_ids: list[str]
    ) -> dict[str, tuple[int | None, int | None]]:
        """Renvoie `{video_id: (duration_s, view_count)}` en interrogeant
        `videos.list?part=contentDetails,statistics` par lots de 50 ids (1
        unité de quota par lot — même coût que fetcher la seule durée)."""
        out: dict[str, tuple[int | None, int | None]] = {}
        for chunk_start in range(0, len(video_ids), 50):
            chunk = video_ids[chunk_start : chunk_start + 50]
            params: dict[str, Any] = {
                "part": "contentDetails,statistics",
                "id": ",".join(chunk),
                "maxResults": 50,
                "key": self._api_key,
            }
            try:
                resp = self._client.get(VIDEOS_URL, params=params)
            except httpx.HTTPError as exc:  # pragma: no cover
                raise SourceError(f"videos.list : {type(exc).__name__}") from exc
            self._on_call()
            if resp.status_code != 200:
                raise SourceError(
                    f"videos.list HTTP {resp.status_code} : {resp.text[:200]}"
                )
            payload = resp.json()
            data: dict[str, Any] = payload if isinstance(payload, dict) else {}
            for item in data.get("items", []):
                vid = str(item.get("id", ""))
                if not vid:
                    continue
                details = item.get("contentDetails", {}) or {}
                statistics = item.get("statistics", {}) or {}
                duration = parse_iso8601_duration(str(details.get("duration", ""))) or None
                views_raw = statistics.get("viewCount")
                views: int | None
                try:
                    views = int(views_raw) if views_raw is not None else None
                except (TypeError, ValueError):
                    views = None
                out[vid] = (duration, views)
        return out


def _sort_key(
    order: VideoOrder,
) -> Any:
    """Clé de tri `(primaire, tie-breaker)` pour l'ordre choisi. `None` est
    renvoyé en dernier via un sentinel négatif en premier élément : trié
    descendant, les valeurs présentes passent devant les absences."""
    if order == "views":
        return lambda entry: (
            entry[2] is not None,
            entry[2] or 0,
            entry[0].published.timestamp() if entry[0].published else 0.0,
        )
    if order == "duration":
        return lambda entry: (
            entry[1] is not None,
            entry[1] or 0,
            entry[0].published.timestamp() if entry[0].published else 0.0,
        )
    # order == "date"
    return lambda entry: (
        entry[0].published is not None,
        entry[0].published.timestamp() if entry[0].published else 0.0,
    )


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
) -> list[tuple[Video, int | None, int | None]]:
    return ChannelVideoLister(api_key, client=client, on_call=on_call).list_videos(
        channel, filters
    )


# Ré-exporte parse_datetime pour les tests
__all__ = [
    "SHORTS_MAX_DURATION_S",
    "VIDEO_ORDERS",
    "ChannelFilters",
    "ChannelInfo",
    "ChannelResolver",
    "ChannelVideoLister",
    "VideoOrder",
    "_parse_datetime",
    "list_channel_videos",
    "parse_iso8601_duration",
    "resolve_channel",
    "uploads_playlist_id",
    "video_url",
]
