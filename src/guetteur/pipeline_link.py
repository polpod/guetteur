"""Orchestration des items LIEN (Lot 8) : fetch → summarize → notify → obsidian.

Pas de nouveau thread, pas de scheduler : la pipeline est appelée directement
depuis le bot Telegram (quand une URL arrive) ou depuis la CLI `guetteur
digest`. La récupération se fait en synchrone côté bot, dans un thread du pool
`handle_update` existant — un lien tweet prend 1 à 3 s réseau, acceptable.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from guetteur.archive.base import ArchiveError, Archiver, NoOpArchiver
from guetteur.config import Config, NotifyChannel
from guetteur.items import LinkItem
from guetteur.models import Summary
from guetteur.notify.base import Message, Notifier, NotifyError
from guetteur.sources.liens.base import LinkFetchError
from guetteur.sources.liens.dispatch import ReaderBundle
from guetteur.store import Store
from guetteur.summarize.base import SummarizerUnavailableError, summary_from_json, summary_to_json
from guetteur.summarize.link import LinkSummaryMeta, SupportsRawCall, summarize_link

log = logging.getLogger(__name__)


class LinkPipelineError(RuntimeError):
    pass


class LinkPipelineUnavailableError(LinkPipelineError):
    """Le backend de résumé est indisponible : ne pas consommer d'essai."""


# Alias de rétrocompat historique du Lot 8 (nommage sans suffixe Error).
LinkPipelineUnavailable = LinkPipelineUnavailableError


NotifierFactory = Callable[[NotifyChannel], Notifier]
# Un exporter est une fonction qui prend (item, summary) et écrit la note.
# `pipeline.build_link_pipeline` passe `functools.partial(write_link_note, exporter)`.
LinkExporter = Callable[[LinkItem, Summary], None]


@dataclass
class LinkCycleStats:
    processed: int = 0
    sent: int = 0
    failed: int = 0


class LinkPipeline:
    """Oriente un item depuis NEW/RETRY/FETCHED/SUMMARIZED jusqu'à SENT.

    - fetch via `ReaderBundle` ; youtube_oneshot est délégué (voir §kind_delegate).
    - summarize via `raw_call` du backend.
    - notify : canal Telegram par défaut (les liens arrivent par Telegram).
    - obsidian : si configuré, note dans Veille/Inbox avec frontmatter kind/url/auteur/source.
    - archive : si configuré, source texte dans NotebookLM.
    """

    def __init__(
        self,
        config: Config,
        store: Store,
        backend: SupportsRawCall,
        notifier_factory: NotifierFactory,
        readers: ReaderBundle | None = None,
        exporter: LinkExporter | None = None,
        archiver: Archiver | None = None,
        claude_lock: threading.Lock | None = None,
        timeout_s: float = 180.0,
        kind_delegate: Callable[[LinkItem], None] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._backend = backend
        self._notifier_factory = notifier_factory
        self._readers = readers or ReaderBundle()
        self._exporter = exporter
        self._archiver = archiver or NoOpArchiver()
        self._claude_lock = claude_lock or threading.Lock()
        self._timeout_s = timeout_s
        self._notifiers: dict[NotifyChannel, Notifier] = {}
        self._kind_delegate = kind_delegate

    def process(self, item_id: str) -> str:
        """Fait avancer un item jusqu'à l'envoi. Retourne 'sent' | 'failed' |
        'retry' | 'delegated' | 'skipped'."""
        item = self._store.get_item(item_id)
        if item is None:
            raise LinkPipelineError(f"Item inconnu : {item_id}")
        ctx = {"item_id": item.item_id, "kind": item.kind, "url": item.url}

        # YouTube hors playlist : on ne résume PAS ici. Le dispatcher (le bot
        # ou le CLI) a la responsabilité de passer le relais au pipeline vidéo
        # via `kind_delegate` qui connaît le store des vidéos et retournera
        # un video_id — on marque l'item comme délégué et on sort.
        if item.kind == "youtube_oneshot":
            if self._kind_delegate is None:
                return self._mark_failed(item, "youtube_oneshot sans délégué")
            try:
                self._kind_delegate(item)
            except Exception as exc:
                log.exception("link.delegate_failed", extra=ctx)
                return self._retry_or_fail(item, f"{type(exc).__name__}: {exc}")
            self._store.item_mark_sent(item.item_id)
            return "delegated"

        # Phase 1 : fetch (si pas déjà fetché).
        if item.content is None:
            try:
                content = self._readers.read(item.url, item.kind)
            except LinkFetchError as exc:
                log.warning("link.fetch_failed", extra={**ctx, "error": str(exc)})
                return self._retry_or_fail(item, f"fetch : {exc}")
            self._store.item_set_fetched(
                item.item_id,
                title=content.title,
                author=content.author,
                published_at=content.published_at,
                content=content.text or _marker(content),
            )
            item = self._store.get_item(item.item_id) or item
            log.info("link.fetched", extra=ctx)

        # Phase 2 : summarize (si pas déjà résumé).
        if item.summary is None:
            try:
                summary = self._summarize(item)
            except SummarizerUnavailableError:
                raise LinkPipelineUnavailable(
                    "backend Claude indisponible"
                ) from None  # pas d'essai consommé
            except Exception as exc:
                log.warning("link.summarize_failed", extra={**ctx, "error": str(exc)})
                return self._retry_or_fail(item, f"summarize : {exc}")
            self._store.item_set_summary(item.item_id, summary_to_json(summary))
            item = self._store.get_item(item.item_id) or item
        else:
            summary = summary_from_json(item.summary)

        # Phase 3 : envoi.
        if not self._store.item_claim_for_sending(item.item_id):
            log.warning("link.not_claimable", extra={**ctx, "status": item.status.value})
            return "skipped"
        try:
            self._deliver(item, summary)
            self._maybe_export(item, summary)
            self._maybe_archive(item, summary)
            self._store.item_mark_sent(item.item_id)
            log.info("link.sent", extra=ctx)
            return "sent"
        except NotifyError as exc:
            self._store.item_release_claim(item.item_id, f"notify : {exc}")
            return "retry"
        except Exception as exc:
            log.exception("link.unexpected_error", extra=ctx)
            self._store.item_release_claim(item.item_id, f"{type(exc).__name__}: {exc}")
            return "retry"

    def _summarize(self, item: LinkItem) -> Summary:
        from guetteur.items import LinkContent

        link = LinkContent(
            url=item.url,
            kind=item.kind,
            title=item.title,
            author=item.author,
            published_at=item.published_at,
            text=item.content or "",
            extras={},
        )
        meta = LinkSummaryMeta(link=link, language="fr", detail="standard")
        with self._claude_lock:
            return summarize_link(self._backend, meta, timeout_s=self._timeout_s)

    def _deliver(self, item: LinkItem, summary: Summary) -> None:
        channel: NotifyChannel = "telegram"
        notifier = self._notifiers.get(channel) or self._notifier_factory(channel)
        self._notifiers[channel] = notifier
        message = build_link_message(summary, item)
        notifier.send(message)

    def _maybe_export(self, item: LinkItem, summary: Summary) -> None:
        if self._exporter is None:
            return
        try:
            self._exporter(item, summary)
        except Exception:  # un échec d'export ne bloque jamais l'envoi
            log.exception("link.export_failed", extra={"item_id": item.item_id})

    def _maybe_archive(self, item: LinkItem, summary: Summary) -> None:
        if isinstance(self._archiver, NoOpArchiver):
            return
        try:
            archive_link_source(self._archiver, item, summary)
            self._store.item_mark_archived(item.item_id)
        except ArchiveError:
            log.warning("link.archive_failed", extra={"item_id": item.item_id})

    def _retry_or_fail(self, item: LinkItem, error: str) -> str:
        n = self._store.item_mark_retry(item.item_id, error)
        if n >= self._config.transcript.max_retries:
            self._store.item_mark_failed(item.item_id, error)
            return "failed"
        return "retry"

    def _mark_failed(self, item: LinkItem, error: str) -> str:
        self._store.item_mark_failed(item.item_id, error)
        return "failed"

    def run_pending(self, limit: int = 20) -> LinkCycleStats:
        stats = LinkCycleStats()
        for item in self._store.pending_items(limit):
            stats.processed += 1
            outcome = self.process(item.item_id)
            if outcome == "sent" or outcome == "delegated":
                stats.sent += 1
            elif outcome == "failed":
                stats.failed += 1
        return stats


def _marker(content: Any) -> str:
    return json.dumps(dict(content.extras.items())) if content.extras else ""


def build_link_message(summary: Summary, item: LinkItem) -> Message:
    """Rend un message Telegram court pour un lien. Buttons reply_markup laissé
    au bot (voir telegram_bot.build_summary_keyboard_for_item)."""
    from guetteur.summarize.format import escape_md_v2

    title = escape_md_v2(summary.title or item.title or item.url)
    url = escape_md_v2(item.url)
    tldr = escape_md_v2(summary.tldr or "")
    why = escape_md_v2(summary.why_it_matters or "")
    kind_label = {
        "tweet": "Tweet",
        "article": "Article",
        "github": "Repo GitHub",
        "youtube_oneshot": "Vidéo YouTube",
    }.get(item.kind, "Lien")

    kps = "\n".join(f"• {escape_md_v2(k.text)}" for k in summary.key_points)
    actions = (
        "\n\n*Actions :*\n" + "\n".join(f"• {escape_md_v2(a)}" for a in summary.actions)
        if summary.actions
        else ""
    )
    md = (
        f"*{escape_md_v2(kind_label)}* \\— [{title}]({url})\n\n"
        f"_{tldr}_\n\n{kps}\n\n*Pourquoi :* {why}{actions}"
    )

    plain_lines = [f"{kind_label} — {summary.title}", f"{item.url}", "", summary.tldr, ""]
    plain_lines += [f"• {k.text}" for k in summary.key_points]
    if summary.actions:
        plain_lines += ["", "Actions :", *(f"• {a}" for a in summary.actions)]
    plain_lines += ["", f"Pourquoi : {summary.why_it_matters}"]
    plain = "\n".join(plain_lines)

    short = f"{summary.title} — {item.url}"
    return Message(markdown_v2=md, plain=plain, short=short)


def archive_link_source(archiver: Archiver, item: LinkItem, summary: Summary) -> None:
    """Dépose une source texte dans NotebookLM pour un lien. On réutilise la
    même interface Archiver que les vidéos (`archive_video`) via l'adaptateur
    `TextSourceAdapter` quand disponible, sinon on tombe silencieusement."""
    adapter = getattr(archiver, "archive_text", None)
    if adapter is None:
        return
    title = summary.title or item.title or item.url
    body = _link_source_text(item, summary)
    adapter(title=title, body=body, url=item.url, kind=item.kind)


def _link_source_text(item: LinkItem, summary: Summary) -> str:
    lines = [
        f"# {summary.title or item.title or item.url}",
        "",
        f"URL : {item.url}",
        f"Kind : {item.kind}",
        f"Source : {item.source}",
        f"Auteur : {item.author or '(inconnu)'}",
        "",
        summary.tldr,
        "",
        "## Points clés",
    ]
    lines += [f"- {k.text}" for k in summary.key_points]
    if summary.why_it_matters:
        lines += ["", "## Pourquoi c'est intéressant", summary.why_it_matters]
    return "\n".join(lines)
