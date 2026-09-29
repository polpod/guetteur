"""Sources YouTube Data API v3.

Deux variantes :

- `YouTubeApiSource` (OAuth) : pour les playlists privées (playlist.private = true).
  Le jeton OAuth est stocké dans data/token.json ; il est créé une fois par
  `guetteur auth` à partir de data/client_secret.json.
- `YouTubeApiKeySource` (clé API) : pour les playlists publiques et non répertoriées.
  Nécessite YOUTUBE_API_KEY dans .env. Détection en 60 s au lieu de l'heure de latence
  du flux RSS ; consomme 1 unité de quota par appel `playlistItems.list`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from guetteur.models import Video
from guetteur.sources.base import SourceError, video_url

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"


class QuotaExceededError(SourceError):
    """Levée quand Google renvoie 403 quotaExceeded : la journée est cuite,
    l'appelant (AdaptiveApiKeySource) doit basculer sur RSS."""


class PlaylistNotFoundError(SourceError):
    """404 : la playlist n'existe pas, est privée, ou l'ID est mal formé."""


def run_oauth_flow(
    client_secret: Path, token_path: Path, port: int = 8765, bind_addr: str = "127.0.0.1"
) -> None:
    """Flux OAuth interactif (serveur local). La redirection Google vise toujours
    http://localhost:PORT ; bind_addr=0.0.0.0 permet de l'atteindre dans un conteneur.
    Voir README pour l'usage via tunnel SSH."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secret.exists():
        raise SourceError(f"Fichier OAuth introuvable : {client_secret}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(
        host="localhost", bind_addr=bind_addr, port=port, open_browser=False
    )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_items(items: list[dict[str, Any]]) -> list[Video]:
    videos: list[Video] = []
    for item in items:
        snippet = item.get("snippet", {})
        details = item.get("contentDetails", {})
        video_id = details.get("videoId") or snippet.get("resourceId", {}).get("videoId")
        # Les vidéos supprimées/privées d'autres chaînes apparaissent sans titre exploitable.
        if not video_id or snippet.get("title") in ("Deleted video", "Private video"):
            continue
        videos.append(
            Video(
                video_id=str(video_id),
                title=str(snippet.get("title", "")),
                channel=str(snippet.get("videoOwnerChannelTitle", "")),
                published=_parse_datetime(
                    details.get("videoPublishedAt") or snippet.get("publishedAt")
                ),
                url=video_url(str(video_id)),
            )
        )
    return videos


class YouTubeApiSource:
    def __init__(
        self, token_path: Path, client: httpx.Client | None = None, max_items: int = 50
    ) -> None:
        self._token_path = token_path
        self._client = client or httpx.Client(timeout=20.0)
        self._max_items = max_items
        self._creds: Credentials | None = None

    def _credentials(self) -> Credentials:
        if self._creds is None:
            if not self._token_path.exists():
                raise SourceError(
                    f"Jeton OAuth absent ({self._token_path}) : lancez `guetteur auth`"
                )
            self._creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                str(self._token_path), SCOPES
            )
        creds = self._creds
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self._token_path.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise SourceError("Jeton OAuth invalide : relancez `guetteur auth`")
        return creds

    def fetch(self, playlist_id: str) -> list[Video]:
        creds = self._credentials()
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(items) < self._max_items:
            params: dict[str, str | int] = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = self._client.get(
                    PLAYLIST_ITEMS_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {creds.token}"},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise SourceError(f"YouTube Data API : échec pour {playlist_id} : {exc}") from exc
            data: dict[str, Any] = resp.json()
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        videos = parse_items(items[: self._max_items])
        # Les playlists sont ordonnées par position ; on veut les plus récentes d'abord.
        videos.sort(key=lambda v: v.published.timestamp() if v.published else 0.0, reverse=True)
        return videos


def _extract_error_reason(resp: httpx.Response) -> str:
    """Renvoie le champ `reason` de la première erreur Google, ou une chaîne vide."""
    try:
        payload = resp.json()
    except ValueError:
        return ""
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    errors = err.get("errors") if isinstance(err, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("reason", ""))
    return str(err.get("status", "")) if isinstance(err, dict) else ""


class YouTubeApiKeySource:
    """`playlistItems.list` par clé API (public / non répertorié). Un appel HTTP
    = 1 unité de quota Google ; l'appelant enregistre la consommation via `on_call`."""

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        max_items: int = 50,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        if not api_key:
            raise SourceError("YOUTUBE_API_KEY vide : pas de source API par clé")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=20.0)
        self._max_items = max_items
        self._on_call = on_call or (lambda: None)

    def fetch(self, playlist_id: str) -> list[Video]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(items) < self._max_items:
            params: dict[str, str | int] = {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
                "key": self._api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = self._client.get(PLAYLIST_ITEMS_URL, params=params)
            except httpx.HTTPError as exc:
                raise SourceError(
                    f"YouTube Data API (clé) : échec réseau pour {playlist_id} : {exc}"
                ) from exc
            # On enregistre l'appel dès qu'il est parti — même en cas de 403 quota,
            # Google l'a compté côté serveur.
            self._on_call()
            if resp.status_code == 403:
                reason = _extract_error_reason(resp)
                if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    raise QuotaExceededError(f"YouTube Data API : quota épuisé ({reason})")
                raise SourceError(f"YouTube Data API : 403 {reason or 'refus'} sur {playlist_id}")
            if resp.status_code == 404:
                raise PlaylistNotFoundError(
                    f"YouTube Data API : playlist introuvable ({playlist_id})"
                )
            try:
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise SourceError(
                    f"YouTube Data API (clé) : {resp.status_code} sur {playlist_id} : {exc}"
                ) from exc
            data: dict[str, Any] = resp.json()
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        videos = parse_items(items[: self._max_items])
        videos.sort(key=lambda v: v.published.timestamp() if v.published else 0.0, reverse=True)
        log.debug(
            "youtube_api_key.fetched",
            extra={"playlist_id": playlist_id, "count": len(videos)},
        )
        return videos
