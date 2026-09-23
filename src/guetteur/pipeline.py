"""Orchestration : découverte des vidéos, transcription, résumé, envoi — séquentiellement."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from guetteur.config import Config, ConfigError, NotifyChannel, PlaylistConfig
from guetteur.models import Segment, Summary, Transcript, Video
from guetteur.notify.base import Message, Notifier, NotifyError
from guetteur.sources.base import SourceError, VideoSource
from guetteur.store import Status, Store, VideoRecord
from guetteur.summarize.base import (
    SummarizeError,
    Summarizer,
    SummarizerUnavailableError,
    SummaryMeta,
    summary_from_json,
    summary_to_json,
)
from guetteur.summarize.format import render_markdown_v2, render_no_transcript, render_plain
from guetteur.transcript.base import NoTranscriptError, TranscriptError, TranscriptProvider

log = logging.getLogger(__name__)


SourceFactory = Callable[[PlaylistConfig], VideoSource]
NotifierFactory = Callable[[NotifyChannel], Notifier]


@dataclass
class CycleStats:
    discovered: int = 0
    processed: int = 0
    sent: int = 0
    failed: int = 0
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


def build_message(summary: Summary, video: Video, label: str) -> Message:
    return Message(
        markdown_v2=render_markdown_v2(summary, video, label),
        plain=render_plain(summary, video, label),
        short=f"{summary.title} — {video.url}",
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
    ) -> None:
        self._config = config
        self._store = store
        self._source_factory = source_factory
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._notifier_factory = notifier_factory
        self._notifiers: dict[NotifyChannel, Notifier] = {}

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

    def run_cycle(self) -> CycleStats:
        stats = CycleStats(discovered=self.poll())
        ids = [p.id for p in self._config.playlists]
        try:
            for record in self._store.pending(self._config.max_videos_per_cycle, ids):
                self._process_safely(record, stats)
        except SummarizerUnavailableError as exc:
            stats.aborted = str(exc)
            log.error("cycle.summarizer_unavailable", extra={"error": str(exc)})
        log.info("cycle.done", extra={**vars(stats), "db": self._store.counts()})
        return stats

    def backfill(self, playlist_id: str, limit: int) -> CycleStats:
        """Traite explicitement les `limit` vidéos les plus récentes d'une playlist, y compris
        celles ignorées au premier lancement. Les vidéos déjà envoyées ne sont jamais renvoyées."""
        playlist = self._config.playlist(playlist_id)
        videos = self._source_factory(playlist).fetch(playlist.id)
        if not self._store.is_playlist_initialized(playlist.id):
            self._store.initialize_playlist(playlist.id, videos)
        stats = CycleStats()
        for video in videos[:limit]:
            if self._store.requeue_for_backfill(video, playlist.id):
                stats.discovered += 1
            record = self._store.get(video.video_id)
            if record is not None and record.sent_at is None and record.status != Status.SENT:
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
        elif outcome == "failed":
            stats.failed += 1

    def _playlist_for(self, record: VideoRecord) -> PlaylistConfig:
        try:
            return self._config.playlist(record.playlist_id)
        except ConfigError:
            return PlaylistConfig(id=record.playlist_id, label=record.playlist_id)

    def _notifier(self, channel: NotifyChannel) -> Notifier:
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
                summary = self._summarizer.summarize(
                    transcript, SummaryMeta(video=video, language=playlist.language)
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
            log.warning("video.already_sent", extra=ctx)
            return "skipped"
        try:
            self._notifier(playlist.notify).send(build_message(summary, video, playlist.label))
        except NotifyError as exc:
            retries = self._store.release_claim(vid, str(exc))
            log.error("video.notify_failed", extra={**ctx, "error": str(exc), "retries": retries})
            if retries > self._config.transcript.max_retries:
                self._store.mark_failed(vid, str(exc))
                return "failed"
            return "retry"
        log.info("video.sent", extra={**ctx, "channel": playlist.notify})
        return "sent"

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
