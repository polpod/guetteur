"""Archiveur NotebookLM : ajoute chaque vidéo envoyée comme source dans un notebook
et y attache une note « <AAAA-MM-JJ> - <titre> » qui contient le résumé Markdown.

Idempotent : la source n'est pas réajoutée si son URL existe déjà, la note n'est
pas recréée si le titre existe déjà. Quand le nombre de sources d'un notebook
dépasse archive.max_sources_per_notebook, on ouvre automatiquement « Veille
YouTube (2) », « (3) »… et l'ID courant est mémorisé dans la table meta.

Conditions de sécurité verrouillées ici (audit §8) :
- Import paresseux de notebooklm-py, erreur explicite si extra non installé.
- Refus de démarrer si l'une des variables de FORBIDDEN_ENV_VARS est présente.
- Injection d'un NOTEBOOKLM_HOME dédié, permissions 0700/0600 vérifiées.
- Jamais d'objet interne de la bibliothèque dans les logs ; redaction des
  messages d'erreur avant journalisation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any

from guetteur.archive.base import (
    FORBIDDEN_ENV_VARS,
    ArchiveError,
    ArchiveOutcome,
    redact,
)
from guetteur.config import ArchiveConfig
from guetteur.models import Video
from guetteur.store import Store

log = logging.getLogger(__name__)

# Un rate limit ou une panne 5xx peut survenir : on attend puis on retente 2 fois.
_RETRY_SLEEP_S = 60.0
_RETRY_MAX_ATTEMPTS = 2

# La note pour un notebook nouvellement créé sait mémoriser son id ici.
_META_NOTEBOOK_ID = "archive_notebook_id"
_META_NOTEBOOK_INDEX = "archive_notebook_index"

# Le nom de base d'un notebook est complété en « <base> (2) », « (3) »… quand
# il déborde. Cette regex remonte l'index depuis un nom qu'on aurait déjà créé.
_INDEX_RE = re.compile(r"\((\d+)\)\s*$")


ClientContextFactory = Callable[[], AbstractAsyncContextManager[Any]]
Sleeper = Callable[[float], Any]  # awaitable ou None ; on await si nécessaire


class NotebookLMArchiver:
    """Archivage dans un notebook Google NotebookLM (voir docstring de module)."""

    enabled: bool = True

    def __init__(
        self,
        config: ArchiveConfig,
        store: Store,
        client_factory: ClientContextFactory | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._client_factory_override = client_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep

    # --- API publique -----------------------------------------------------------------------

    def archive(self, video: Video, summary_markdown: str) -> ArchiveOutcome:
        """Point d'entrée synchrone : archive une vidéo, retourne (notebook_id, note_id)."""
        self._check_forbidden_env()
        self._check_home_permissions()
        return asyncio.run(self._archive_async(video, summary_markdown))

    def auth_check(self) -> str | None:
        """`guetteur doctor` : retourne l'email du compte Google renvoyé par l'API,
        ou None si l'auth n'est pas résolue. Ne lève jamais."""
        self._check_forbidden_env()
        self._check_home_permissions()
        try:
            return asyncio.run(self._auth_check_async())
        except ArchiveError:
            raise
        except Exception as exc:
            raise ArchiveError(redact(f"{type(exc).__name__}: {exc}"), retryable=False) from exc

    # --- vérifications de démarrage --------------------------------------------------------

    def _check_forbidden_env(self) -> None:
        present = sorted(v for v in FORBIDDEN_ENV_VARS if v in os.environ)
        if present:
            log.error("archive.forbidden_env", extra={"variables": present})
            raise ArchiveError(
                "archivage refusé : variables interdites présentes dans l'environnement : "
                + ", ".join(present)
                + " (voir audit §8.3 — retirez-les du service).",
                retryable=False,
            )

    def _check_home_permissions(self) -> None:
        home = self._config.home
        if not home.exists():
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = home.stat().st_mode & 0o777
        if mode != 0o700:
            raise ArchiveError(
                f"NOTEBOOKLM_HOME ({home}) doit être en 0700 : "
                f"chmod 700 {home} (mode actuel {mode:o}). "
                "Le lot 3 ne modifie jamais les permissions d'un chemin qui n'est pas ce dossier.",
                retryable=False,
            )
        for filename in ("storage_state.json", "master_token.json"):
            for path in home.rglob(filename):
                m = path.stat().st_mode & 0o777
                if m != 0o600:
                    raise ArchiveError(
                        f"{path} doit être en 0600 : chmod 600 {path} (mode actuel {m:o}).",
                        retryable=False,
                    )

    # --- environnement contrôlé + fabrique de client ---------------------------------------

    @contextmanager
    def _controlled_env(self) -> Any:
        """Retire les variables interdites et injecte NOTEBOOKLM_HOME, puis restaure."""
        saved: dict[str, str] = {}
        for var in FORBIDDEN_ENV_VARS:
            if var in os.environ:
                saved[var] = os.environ.pop(var)
        prev_home = os.environ.get("NOTEBOOKLM_HOME")
        os.environ["NOTEBOOKLM_HOME"] = str(self._config.home)
        try:
            yield
        finally:
            for k, v in saved.items():
                os.environ[k] = v
            if prev_home is None:
                os.environ.pop("NOTEBOOKLM_HOME", None)
            else:
                os.environ["NOTEBOOKLM_HOME"] = prev_home

    @asynccontextmanager
    async def _open_client(self) -> AsyncIterator[Any]:
        with self._controlled_env():
            factory = self._client_factory_override or _default_client_factory
            async with factory() as client:
                yield client

    # --- workflow async --------------------------------------------------------------------

    async def _auth_check_async(self) -> str | None:
        async with self._open_client() as client:
            email = await client.get_account_email(live_fallback=True)
            return str(email) if email else None

    async def _archive_async(self, video: Video, summary_markdown: str) -> ArchiveOutcome:
        last_err: ArchiveError | None = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                async with self._open_client() as client:
                    notebook_id = await self._ensure_notebook(client)
                    await self._ensure_source(client, notebook_id, video)
                    note_id = await self._ensure_note(client, notebook_id, video, summary_markdown)
                    return ArchiveOutcome(notebook_id=notebook_id, note_id=note_id)
            except ArchiveError as exc:
                last_err = exc
                if not exc.retryable or attempt == _RETRY_MAX_ATTEMPTS:
                    raise
                log.warning(
                    "archive.retry",
                    extra={"attempt": attempt, "error": str(exc), "sleep_s": _RETRY_SLEEP_S},
                )
                await self._sleep(_RETRY_SLEEP_S)
            except Exception as exc:
                # Toute exception non ArchiveError venant de la bibliothèque est
                # redactée avant journalisation et propagée comme ArchiveError.
                message = redact(f"{type(exc).__name__}: {exc}")
                raise ArchiveError(message, retryable=_is_retryable(exc)) from exc
        assert last_err is not None
        raise last_err

    # --- étapes du workflow ----------------------------------------------------------------

    async def _ensure_notebook(self, client: Any) -> str:
        stored = self._store.get_meta(_META_NOTEBOOK_ID)
        index = int(self._store.get_meta(_META_NOTEBOOK_INDEX) or "1")
        if stored:
            # Le notebook courant peut déborder : on vérifie la taille et on
            # bascule vers « (n+1) » si besoin. list() n'est pas facturé.
            sources = await self._safe_call(client.sources.list, stored)
            if len(sources) < self._config.max_sources_per_notebook:
                return stored
            log.info(
                "archive.notebook_rotate",
                extra={
                    "previous": stored,
                    "sources": len(sources),
                    "max": self._config.max_sources_per_notebook,
                },
            )
            index += 1
        title = self._notebook_title(index)
        notebook = await self._safe_call(client.notebooks.create, title)
        notebook_id = str(notebook.id)
        self._store.set_meta(_META_NOTEBOOK_ID, notebook_id)
        self._store.set_meta(_META_NOTEBOOK_INDEX, str(index))
        log.info("archive.notebook_created", extra={"title": title, "notebook_id": notebook_id})
        return notebook_id

    def _notebook_title(self, index: int) -> str:
        base = self._config.notebook_name
        return base if index == 1 else f"{base} ({index})"

    async def _ensure_source(self, client: Any, notebook_id: str, video: Video) -> str:
        existing = await self._safe_call(client.sources.list, notebook_id)
        for src in existing:
            if _same_url(getattr(src, "url", None), video.url):
                log.info(
                    "archive.source_exists",
                    extra={"notebook_id": notebook_id, "video_id": video.video_id},
                )
                return str(src.id)
        source = await self._safe_call(
            client.sources.add_url, notebook_id, video.url, wait=True, wait_timeout=240.0
        )
        log.info(
            "archive.source_added",
            extra={"notebook_id": notebook_id, "video_id": video.video_id, "source_id": source.id},
        )
        return str(source.id)

    async def _ensure_note(
        self, client: Any, notebook_id: str, video: Video, summary_markdown: str
    ) -> str:
        title = self._note_title(video)
        existing = await self._safe_call(client.notes.list, notebook_id)
        for note in existing:
            if getattr(note, "title", None) == title:
                log.info(
                    "archive.note_exists",
                    extra={"notebook_id": notebook_id, "video_id": video.video_id},
                )
                return str(note.id)
        body = f"{summary_markdown}\n\n{video.url}"
        note = await self._safe_call(client.notes.create, notebook_id, title=title, content=body)
        log.info(
            "archive.note_created",
            extra={"notebook_id": notebook_id, "video_id": video.video_id, "note_id": note.id},
        )
        return str(note.id)

    def _note_title(self, video: Video) -> str:
        date = (video.published or self._now()).astimezone(UTC).strftime("%Y-%m-%d")
        return f"{date} - {video.title}"

    async def _safe_call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except ArchiveError:
            raise
        except Exception as exc:
            raise ArchiveError(
                redact(f"{type(exc).__name__}: {exc}"), retryable=_is_retryable(exc)
            ) from exc


# --- helpers ----------------------------------------------------------------------------


def _same_url(a: str | None, b: str | None) -> bool:
    """Comparaison souple : suffixes de query et fragment ignorés."""
    if not a or not b:
        return False
    return _normalize_url(a) == _normalize_url(b)


def _normalize_url(url: str) -> str:
    stripped = url.strip().rstrip("/")
    for sep in ("#", "?"):
        if sep in stripped:
            stripped = stripped.split(sep, 1)[0]
    return stripped


_RETRYABLE_TYPES = {"RateLimitError", "ServerError", "NetworkError"}


def _is_retryable(exc: BaseException) -> bool:
    return type(exc).__name__ in _RETRYABLE_TYPES


def _default_client_factory() -> AbstractAsyncContextManager[Any]:
    """Import paresseux : notebooklm n'est chargé qu'à la première archive réelle.

    Message explicite si l'extra optionnel n'a pas été installé."""
    try:
        from notebooklm import NotebookLMClient
    except ImportError as exc:
        raise ArchiveError(
            "notebooklm-py absent : installez l'extra avec "
            "`uv sync --frozen --no-dev --extra notebooklm` (0.8.3 verrouillé dans uv.lock).",
            retryable=False,
        ) from exc
    client: AbstractAsyncContextManager[Any] = NotebookLMClient.from_storage()
    return client
