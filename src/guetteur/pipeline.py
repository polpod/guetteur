"""Orchestration : découverte des vidéos, transcription, résumé, envoi — séquentiellement."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from guetteur.archive.base import ArchiveError, Archiver, NoOpArchiver, redact
from guetteur.config import Config, ConfigError, NotifyChannel, PlaylistConfig
from guetteur.models import DetailLevel, Segment, Summary, Transcript, Video
from guetteur.notify.base import Message, Notifier, NotifyError
from guetteur.sources.base import SourceError, VideoSource
from guetteur.store import PENDING_STATUSES, Status, Store, VideoRecord
from guetteur.summarize.base import (
    SummarizeError,
    Summarizer,
    SummarizerUnavailableError,
    SummaryMeta,
    summary_from_json,
    summary_to_json,
)
from guetteur.summarize.format import (
    TELEGRAM_LIMIT,
    numbered,
    render_markdown,
    render_markdown_v2,
    render_no_transcript,
    render_plain,
    split_markdown_v2_parts,
    split_plain_parts,
)
from guetteur.transcript.base import NoTranscriptError, TranscriptError, TranscriptProvider

WHATSAPP_LIMIT = 4096

log = logging.getLogger(__name__)


MAX_RETRY_AFTER_S = 60.0

SourceFactory = Callable[[PlaylistConfig], VideoSource]
NotifierFactory = Callable[[NotifyChannel], Notifier]


@dataclass
class CycleStats:
    discovered: int = 0
    processed: int = 0
    sent: int = 0
    failed: int = 0
    recovered: int = 0  # envois « sending » périmés repris en début de cycle
    skipped: int = 0  # backfill : vidéos déjà envoyées ou « failed » sans --force
    archived: int = 0  # vidéos archivées avec succès dans NotebookLM
    aborted: str = ""  # raison si le cycle a été interrompu (backend de résumé indisponible)


def transcript_to_json(t: Transcript) -> str:
    return json.dumps(
        {
            "language": t.language,
            "source": t.source,
            "segments": [[s.start, s.text] for s in t.segments],
        },
        ensure_ascii=False,
    )


def transcript_from_json(video_id: str, raw: str) -> Transcript:
    data = json.loads(raw)
    return Transcript(
        video_id=video_id,
        language=str(data["language"]),
        source=str(data["source"]),
        segments=tuple(Segment(start=float(s), text=str(t)) for s, t in data["segments"]),
    )


def build_message(
    summary: Summary,
    video: Video,
    label: str,
    reply_markup: dict[str, Any] | None = None,
) -> Message:
    md_raw = render_markdown_v2(summary, video, label)
    plain_raw = render_plain(summary, video, label)
    md_parts = numbered(split_markdown_v2_parts(md_raw, TELEGRAM_LIMIT), escape=True)
    plain_parts = numbered(split_plain_parts(plain_raw, WHATSAPP_LIMIT), escape=False)
    # Pour la rétrocompatibilité on remplit toujours markdown_v2/plain avec la string
    # complète (utilisée par les tests et le rendu archive). Les notifiers préfèrent les
    # `*_parts` quand ils sont non vides ; on ne les remplit que si un découpage réel a
    # eu lieu, sinon le comportement reste identique aux Lots 1/2/3.
    return Message(
        markdown_v2=md_parts[0] if len(md_parts) == 1 else md_raw,
        plain=plain_parts[0] if len(plain_parts) == 1 else plain_raw,
        short=f"{summary.title} — {video.url}",
        markdown_v2_parts=tuple(md_parts) if len(md_parts) > 1 else (),
        plain_parts=tuple(plain_parts) if len(plain_parts) > 1 else (),
        reply_markup=reply_markup,
    )


class Pipeline:
    def __init__(
        self,
        config: Config,
        store: Store,
        source_factory: SourceFactory,
        transcriber: TranscriptProvider,
        summarizer: Summarizer,
        notifier_factory: NotifierFactory,
        sleep: Callable[[float], None] = time.sleep,
        archiver: Archiver | None = None,
        detail_override: DetailLevel | None = None,
        claude_lock: threading.Lock | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._source_factory = source_factory
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._notifier_factory = notifier_factory
        self._notifiers: dict[NotifyChannel, Notifier] = {}
        self._sleep = sleep
        self._archiver: Archiver = archiver or NoOpArchiver()
        # Surcharge du niveau de détail choisi côté playlist : appliqué en session (CLI
        # `--detail` sur once/backfill/reset), jamais persistée. None = respecter la
        # playlist. Le prochain cycle sans override retombe sur playlist.detail.
        self._detail_override: DetailLevel | None = detail_override
        # Verrou Claude partagé avec le bot Telegram (Lot 5) : jamais deux appels
        # concurrents à Claude. Par défaut un lock local (comportement historique).
        self._claude_lock: threading.Lock = claude_lock or threading.Lock()

    # --- découverte ------------------------------------------------------------------------

    def poll(self) -> int:
        discovered = 0
        for playlist in self._config.playlists:
            try:
                videos = self._source_factory(playlist).fetch(playlist.id)
            except SourceError as exc:
                log.error(
                    "poll.source_error", extra={"playlist_id": playlist.id, "error": str(exc)}
                )
                continue
            if not self._store.is_playlist_initialized(playlist.id):
                n = self._store.initialize_playlist(playlist.id, videos)
                log.info(
                    "poll.playlist_initialized",
                    extra={"playlist_id": playlist.id, "skipped_existing": n},
                )
                continue
            new = sum(1 for v in videos if self._store.add_new(v, playlist.id))
            if new:
                log.info("poll.new_videos", extra={"playlist_id": playlist.id, "count": new})
            discovered += new
        return discovered

    # --- traitement ------------------------------------------------------------------------

    def recover_stale_sending(self) -> int:
        timeout = timedelta(minutes=self._config.notify.sending_timeout_min)
        recovered = self._store.recover_stale_sending(timeout)
        for vid in recovered:
            log.warning("video.sending_recovered", extra={"video_id": vid})
        return len(recovered)

    def run_cycle(self) -> CycleStats:
        self._store.beat()
        recovered = self.recover_stale_sending()
        stats = CycleStats(discovered=self.poll(), recovered=recovered)
        ids = [p.id for p in self._config.playlists]
        try:
            for record in self._store.pending(self._config.max_videos_per_cycle, ids):
                self._process_safely(record, stats)
        except SummarizerUnavailableError as exc:
            stats.aborted = str(exc)
            log.error("cycle.summarizer_unavailable", extra={"error": str(exc)})
        self._store.beat()
        log.info("cycle.done", extra={**vars(stats), "db": self._store.counts()})
        return stats

    def backfill(self, playlist_id: str, limit: int, force: bool = False) -> CycleStats:
        """Traite explicitement les `limit` vidéos les plus récentes d'une playlist, y compris
        celles ignorées au premier lancement. Seul le statut « sent » (réellement envoyé) vaut
        « déjà envoyée » ; les « failed » ne sont reprises qu'avec force=True."""
        playlist = self._config.playlist(playlist_id)
        videos = self._source_factory(playlist).fetch(playlist.id)
        if not self._store.is_playlist_initialized(playlist.id):
            self._store.initialize_playlist(playlist.id, videos)
        stats = CycleStats(recovered=self.recover_stale_sending())
        for video in videos[:limit]:
            if self._store.requeue_for_backfill(video, playlist.id, force=force):
                stats.discovered += 1
            record = self._store.get(video.video_id)
            if record is None:
                continue
            ctx = {"video_id": record.video_id, "status": record.status.value}
            if record.really_sent:
                log.info("backfill.already_sent", extra=ctx)
                stats.skipped += 1
                continue
            if record.status is Status.FAILED:
                log.warning(
                    "backfill.skipped_failed",
                    extra={
                        **ctx,
                        "error": record.last_error,
                        "hint": "guetteur retry --video-id ou backfill --force",
                    },
                )
                stats.skipped += 1
                continue
            if record.status is Status.SENDING:
                log.info("backfill.sending_in_progress", extra=ctx)
                stats.skipped += 1
                continue
            if record.status in PENDING_STATUSES:
                try:
                    self._process_safely(record, stats)
                except SummarizerUnavailableError as exc:
                    stats.aborted = str(exc)
                    log.error("backfill.summarizer_unavailable", extra={"error": str(exc)})
                    break
        log.info("backfill.done", extra={"playlist_id": playlist_id, **vars(stats)})
        return stats

    def _process_safely(self, record: VideoRecord, stats: CycleStats) -> None:
        stats.processed += 1
        try:
            outcome = self.process(record)
        except SummarizerUnavailableError:
            raise
        except Exception as exc:  # une vidéo ne doit jamais arrêter le service
            log.exception("video.unexpected_error", extra={"video_id": record.video_id})
            outcome = self._retry_or_fail(record, f"{type(exc).__name__}: {exc}", notify=False)
        if outcome == "sent":
            stats.sent += 1
            latest = self._store.get(record.video_id)
            if latest is not None and latest.is_archived:
                stats.archived += 1
        elif outcome == "failed":
            stats.failed += 1

    def _playlist_for(self, record: VideoRecord) -> PlaylistConfig:
        try:
            return self._config.playlist(record.playlist_id)
        except ConfigError:
            return PlaylistConfig(id=record.playlist_id, label=record.playlist_id)

    def _notifier(self, channel: NotifyChannel) -> Notifier:
        """Notifier du canal (mis en cache). Jetons manquants → NotifyError non retryable."""
        if channel not in self._notifiers:
            self._notifiers[channel] = self._notifier_factory(channel)
        return self._notifiers[channel]

    def process(self, record: VideoRecord) -> str:
        """Fait avancer une vidéo jusqu'à l'envoi. Reprend là où elle s'était arrêtée."""
        vid = record.video_id
        playlist = self._playlist_for(record)
        video = record.to_video()
        ctx = {"video_id": vid, "playlist_id": playlist.id}

        if record.summary is not None:
            summary = summary_from_json(record.summary)
        else:
            try:
                if record.transcript is not None:
                    transcript = transcript_from_json(vid, record.transcript)
                else:
                    transcript = self._transcriber.get(vid)
                    self._store.set_transcript(vid, transcript_to_json(transcript))
                    log.info("video.transcribed", extra={**ctx, "source": transcript.source})
                detail = self._detail_override or playlist.detail
                with self._claude_lock:
                    summary = self._summarizer.summarize(
                        transcript,
                        SummaryMeta(video=video, language=playlist.language, detail=detail),
                    )
            except SummarizerUnavailableError:
                raise  # problème de backend, pas de la vidéo : ne consomme pas d'essai
            except NoTranscriptError as exc:
                return self._retry_or_fail(record, str(exc), notify=True)
            except (TranscriptError, SummarizeError) as exc:
                return self._retry_or_fail(record, str(exc), notify=False)
            self._store.set_summary(vid, summary_to_json(summary))
            log.info("video.summarized", extra=ctx)

        if not self._store.claim_for_sending(vid):
            current = self._store.get(vid)
            state = current.status.value if current else "inconnue"
            log.warning("video.not_claimable", extra={**ctx, "status": state})
            return "skipped"
        # Cache multi-niveaux du Lot 5 : le résumé fraîchement généré est enregistré
        # sous son niveau. Les boutons du bot peuvent le servir sans rappeler Claude.
        self._store.cache_summary(vid, summary.detail, summary_to_json(summary))
        reply_markup = self._reply_markup_for(playlist, vid, summary.detail)
        return self._deliver(
            vid,
            playlist,
            video,
            summary,
            build_message(summary, video, playlist.label, reply_markup=reply_markup),
        )

    def _reply_markup_for(
        self, playlist: PlaylistConfig, video_id: str, detail: DetailLevel
    ) -> dict[str, Any] | None:
        """Boutons inline sous le résumé quand le bot Telegram est actif et que la
        playlist envoie sur Telegram. Rien sur WhatsApp (l'API ne gère pas ce clavier)."""
        if not self._config.telegram.interactive or playlist.notify != "telegram":
            return None
        # Import local pour éviter la dépendance à `notify/telegram_bot.py` quand le bot
        # n'est pas utilisé (tests, provider WhatsApp uniquement…).
        from guetteur.notify.telegram_bot import build_summary_keyboard

        return build_summary_keyboard(video_id, detail)

    # --- envoi -----------------------------------------------------------------------------

    def _delay(self, attempt: int, exc: NotifyError) -> float:
        delays = self._config.notify.retry_delays_s
        delay = delays[min(attempt - 1, len(delays) - 1)]
        if exc.retry_after is not None:  # 429 : le fournisseur indique combien attendre
            delay = max(delay, min(exc.retry_after, MAX_RETRY_AFTER_S))
        return delay

    def _channels(self, playlist: PlaylistConfig) -> list[tuple[NotifyChannel, bool]]:
        channels: list[tuple[NotifyChannel, bool]] = [(playlist.notify, False)]
        fallback = self._config.notify.fallback
        if fallback is not None and fallback != playlist.notify:
            channels.append((fallback, True))
        return channels

    def _deliver(
        self,
        vid: str,
        playlist: PlaylistConfig,
        video: Video,
        summary: Summary,
        message: Message,
    ) -> str:
        """Envoie une vidéo déjà réclamée (« sending ») : tentatives avec attente croissante
        sur erreur passagère, puis canal de secours après un échec définitif du principal.
        Chaque tentative est tracée dans deliveries."""
        max_attempts = self._config.notify.max_attempts
        reasons: list[str] = []
        transient = False
        for channel, is_fallback in self._channels(playlist):
            ctx = {"video_id": vid, "channel": channel, "fallback": is_fallback}
            last: NotifyError | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    provider_id = self._notifier(channel).send(message)
                except NotifyError as exc:
                    last = exc
                    self._store.add_delivery(
                        vid, channel, attempt, ok=False, error=str(exc), is_fallback=is_fallback
                    )
                    log.warning(
                        "notify.attempt_failed",
                        extra={
                            **ctx,
                            "attempt": attempt,
                            "retryable": exc.retryable,
                            "error": str(exc),
                        },
                    )
                    if not exc.retryable or attempt == max_attempts:
                        break
                    self._sleep(self._delay(attempt, exc))
                    continue
                self._store.add_delivery(
                    vid,
                    channel,
                    attempt,
                    ok=True,
                    provider_message_id=provider_id,
                    is_fallback=is_fallback,
                )
                self._store.mark_sent(vid)
                log.info("video.sent", extra={**ctx, "attempt": attempt})
                # Lot 5 : chaque message Telegram envoyé est lié à la vidéo pour que
                # l'utilisateur puisse répondre à N'IMPORTE quelle partie et poser une
                # question. WhatsApp n'utilise pas ce mécanisme.
                if channel == "telegram" and provider_id:
                    for raw in provider_id.split(","):
                        raw = raw.strip()
                        if raw.isdigit():
                            self._store.link_message(int(raw), vid, kind="summary:auto")
                self._maybe_archive(vid, video, summary, playlist.label)
                self.maybe_export(video, summary)
                return "sent"
            if last is not None:
                kind = "passagère" if last.retryable else "définitive"
                reasons.append(f"{channel} ({kind}) : {last}")
                transient = transient or last.retryable

        reason = " | ".join(reasons) or "aucun canal disponible"
        if transient:
            # Au moins un canal a échoué de façon passagère : on retentera au prochain cycle.
            retries = self._store.release_claim(vid, reason)
            log.error(
                "video.notify_failed", extra={"video_id": vid, "retries": retries, "error": reason}
            )
            if retries > self._config.transcript.max_retries:
                self._store.mark_failed(vid, reason)
                log.error("video.failed", extra={"video_id": vid, "error": reason})
                return "failed"
            return "retry"
        # Erreur de configuration partout : inutile d'insister, raison lisible en base.
        self._store.mark_failed(vid, reason)
        log.error("video.failed", extra={"video_id": vid, "error": reason, "retryable": False})
        return "failed"

    def _retry_or_fail(self, record: VideoRecord, error: str, notify: bool) -> str:
        vid = record.video_id
        retries = self._store.mark_retry(vid, error)
        max_retries = self._config.transcript.max_retries
        if retries <= max_retries:
            log.warning("video.retry", extra={"video_id": vid, "retries": retries, "error": error})
            return "retry"
        if self._store.mark_failed(vid, error):
            log.error("video.failed", extra={"video_id": vid, "error": error})
            if notify:
                self._notify_no_transcript(record)
        return "failed"

    # --- export Obsidian + applicabilité (Lot 6) ------------------------------------------

    def maybe_export(self, video: Video, summary: Summary) -> None:
        """Appelé après un envoi réussi si `[obsidian] enabled` OU `[applicability] enabled`.
        Non bloquant : toute erreur reste en warning."""
        if not self._config.obsidian.enabled and not self._config.applicability.enabled:
            return
        try:
            from guetteur.export.obsidian import ObsidianExporter
        except ImportError:
            return
        exporter: Any | None = None
        pertinences: list[Any] = []
        # 1. Applicabilité (si activée) — on scorer d'abord pour pouvoir écrire les
        #    scores dans le frontmatter de la note.
        if self._config.applicability.enabled:
            try:
                pertinences = self._evaluate_applicability(video, summary)
            except Exception as exc:
                log.warning(
                    "applicability.failed",
                    extra={"video_id": video.video_id, "error": f"{type(exc).__name__}: {exc}"},
                )
        # 2. Export Obsidian (si activé).
        if self._config.obsidian.enabled:
            try:
                exporter = ObsidianExporter(self._config, self._store)
                projets_scores = [(p.projet, p.score, p.idee) for p in pertinences]
                exporter.export_note(
                    video=video,
                    summary=summary,
                    detail=summary.detail,
                    theme="",
                    tags=[],
                    tags_proposes=[],
                    projets_scores=projets_scores,
                )
                for p in pertinences:
                    if p.is_actionable:
                        exporter.append_idea(
                            video,
                            p.projet,
                            p.score,
                            p.idee,
                            p.integration,
                            p.effort,
                            p.prompt_claude_code,
                        )
            except Exception as exc:
                log.warning(
                    "obsidian.export_failed",
                    extra={"video_id": video.video_id, "error": f"{type(exc).__name__}: {exc}"},
                )

    def _evaluate_applicability(self, video: Video, summary: Summary) -> list[Any]:
        from guetteur.export.obsidian import ObsidianExporter
        from guetteur.summarize.applicability import build_evaluator_from_summarizer

        exporter = ObsidianExporter(self._config, self._store)
        exporter.ensure_vault_layout()  # crée les fiches par défaut si absentes
        sheets = exporter.load_project_sheets()
        if not sheets:
            return []
        evaluator = build_evaluator_from_summarizer(self._summarizer)
        with self._claude_lock:
            pertinences = evaluator.evaluate(video, summary, sheets)
        for p in pertinences:
            self._store.upsert_applicability(
                video.video_id,
                p.projet,
                p.score,
                p.idee,
                p.integration,
                p.effort,
                p.risques,
                p.prompt_claude_code,
            )
        return pertinences

    # --- archivage (Lot 3) -----------------------------------------------------------------

    def _maybe_archive(self, vid: str, video: Video, summary: Summary, label: str) -> None:
        """Après un envoi réussi : ajoute la vidéo au notebook NotebookLM. Non bloquant :
        toute erreur reste en warning, archived_at reste NULL, la vidéo est déjà « sent »."""
        if not self._archiver.enabled:
            return
        try:
            body = render_markdown(summary, video, label)
            outcome = self._archiver.archive(video, body)
        except ArchiveError as exc:
            log.warning(
                "video.archive_failed",
                extra={"video_id": vid, "retryable": exc.retryable, "error": redact(str(exc))},
            )
            return
        except Exception as exc:  # défense en profondeur : jamais bloquant
            log.warning(
                "video.archive_failed",
                extra={"video_id": vid, "error": redact(f"{type(exc).__name__}: {exc}")},
            )
            return
        self._store.mark_archived(vid)
        log.info(
            "video.archived",
            extra={
                "video_id": vid,
                "notebook_id": outcome.notebook_id,
                "note_id": outcome.note_id,
            },
        )

    def _notify_no_transcript(self, record: VideoRecord) -> None:
        playlist = self._playlist_for(record)
        video = record.to_video()
        message = Message(
            markdown_v2=render_no_transcript(video, markdown_v2=True),
            plain=render_no_transcript(video, markdown_v2=False),
            short=f"Pas de transcription — {video.title}",
        )
        try:
            self._notifier(playlist.notify).send(message)
        except NotifyError as exc:
            log.error(
                "video.failure_notify_failed",
                extra={"video_id": record.video_id, "error": str(exc)},
            )
