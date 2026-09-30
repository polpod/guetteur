"""Point d'entrée CLI : guetteur run | once | backfill | status | retry | reset | health |
archive | compare | test-notify | doctor | auth."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any

import schedule
from dotenv import load_dotenv

from guetteur.archive.base import Archiver, NoOpArchiver
from guetteur.config import Config, ConfigError, NotifyChannel, PlaylistConfig, load_config
from guetteur.logs import setup_logging
from guetteur.models import DETAIL_LEVELS, DetailLevel, KeyPoint, Summary, Video
from guetteur.notify import NotifyError, build_notifier
from guetteur.notify.base import Message
from guetteur.pipeline import Pipeline, build_message
from guetteur.sources.base import SourceError, VideoSource
from guetteur.store import Status, Store

log = logging.getLogger("guetteur")


def build_archiver(config: Config, store: Store) -> Archiver:
    """Renvoie un archiveur NotebookLM si `archive.enabled = true`, sinon un no-op.
    L'import de notebooklm-py reste paresseux : rien n'est chargé si l'archivage est off."""
    if not config.archive.enabled:
        return NoOpArchiver()
    from guetteur.archive.notebooklm import NotebookLMArchiver

    return NotebookLMArchiver(config.archive, store)


def build_pipeline(
    config: Config,
    store: Store,
    detail_override: DetailLevel | None = None,
    claude_lock: Any = None,
) -> Pipeline:
    from guetteur.sources.rss import RssSource
    from guetteur.summarize import build_summarizer
    from guetteur.transcript import Transcriber
    from guetteur.transcript.whisper import WhisperTranscriber

    summarizer = build_summarizer(config)  # ConfigError explicite si clé API manquante
    rss = RssSource()
    oauth_source: VideoSource | None = None
    public_source = _build_public_source(config, store, rss)

    def source_for(playlist: PlaylistConfig) -> VideoSource:
        nonlocal oauth_source
        if not playlist.private:
            return public_source
        if oauth_source is None:
            from guetteur.sources.api import YouTubeApiSource

            oauth_source = YouTubeApiSource(config.token_path)
        return oauth_source

    whisper = (
        WhisperTranscriber(config.transcript.whisper_model)
        if config.transcript.whisper_enabled
        else None
    )
    return Pipeline(
        config=config,
        store=store,
        source_factory=source_for,
        transcriber=Transcriber(config.transcript.languages, whisper=whisper),
        summarizer=summarizer,
        notifier_factory=lambda channel: build_notifier(channel, config),
        archiver=build_archiver(config, store),
        detail_override=detail_override,
        claude_lock=claude_lock,
    )


def _build_public_source(config: Config, store: Store, rss: Any) -> VideoSource:
    """Choisit la source pour les playlists NON privées, selon config.source :

    - `rss` : force RSS (comportement Lot 1/2).
    - `api` : force la clé API (échoue clairement si YOUTUBE_API_KEY manque).
    - `auto` (défaut) : API par clé si YOUTUBE_API_KEY présente, RSS sinon.

    En mode API on encapsule dans `AdaptiveApiKeySource` qui bascule sur RSS
    au-delà du plafond de quota et alerte Telegram aux paliers 8000/9500."""
    from guetteur.sources.adaptive import AdaptiveApiKeySource
    from guetteur.sources.api import YouTubeApiKeySource

    api_key = config.secrets.youtube_api_key
    mode = config.source
    if mode == "rss" or (mode == "auto" and not api_key):
        return rss  # type: ignore[no-any-return]
    if mode == "api" and not api_key:
        raise ConfigError(
            "general.source = 'api' exige YOUTUBE_API_KEY dans .env "
            "(ou revenez à source = 'auto' ou 'rss')."
        )

    def bump() -> None:
        store.youtube_quota_bump(1)

    api_source = YouTubeApiKeySource(api_key, on_call=bump)
    alert: Any = None
    if config.secrets.telegram_bot_token and config.secrets.telegram_chat_id:
        from guetteur.notify.telegram import TelegramNotifier

        notifier = TelegramNotifier(
            config.secrets.telegram_bot_token, config.secrets.telegram_chat_id
        )

        def send_alert(text: str) -> None:
            try:
                from guetteur.notify.base import Message

                notifier.send(Message(markdown_v2=text, plain=text, short=text))
            except Exception:
                log.exception("youtube_quota.alert_failed")

        alert = send_alert
    return AdaptiveApiKeySource(api_source, rss, store, alert=alert)


def cmd_once(config: Config, detail: DetailLevel | None = None) -> int:
    store = Store(config.db_path)
    try:
        stats = build_pipeline(config, store, detail_override=detail).run_cycle()
    finally:
        store.close()
    return 1 if stats.failed or stats.aborted else 0


def cmd_run(config: Config) -> int:
    import threading

    store = Store(config.db_path)
    # Verrou Claude partagé pipeline ↔ bot (Lot 5) : jamais deux appels concurrents.
    claude_lock = threading.Lock()
    pipeline = build_pipeline(config, store, claude_lock=claude_lock)
    bot = _maybe_start_bot(config, store, pipeline, claude_lock)
    stopping = False

    def _stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        log.info("service.stopping", extra={"signal": signum})
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    def job() -> None:
        try:
            pipeline.run_cycle()
        except Exception:
            log.exception("cycle.crashed")

    log.info(
        "service.started",
        extra={
            "interval_s": config.poll_interval_seconds,
            "playlists": [p.id for p in config.playlists],
            "model": config.claude_model,
            "provider": config.summarize.provider,
            "bot_interactive": bot is not None,
        },
    )
    schedule.every(config.poll_interval_seconds).seconds.do(job)
    job()
    try:
        while not stopping:
            schedule.run_pending()
            time.sleep(1)
    finally:
        schedule.clear()
        if bot is not None:
            bot.stop()
        store.close()
    return 0


def _maybe_start_bot(config: Config, store: Store, pipeline: Pipeline, claude_lock: Any) -> Any:
    """Instancie et démarre le bot Telegram si `[telegram] interactive = true` ET si
    les jetons Telegram sont présents. En dev/tests sans jetons, retourne None sans
    lever : `guetteur run` continue à tourner en mode « envoi automatique seul »."""
    if not config.telegram.interactive:
        log.info("telegram_bot.disabled_by_config")
        return None
    if not config.secrets.telegram_bot_token or not config.secrets.telegram_chat_id:
        log.info("telegram_bot.disabled_missing_tokens")
        return None
    try:
        from guetteur.notify import build_notifier
        from guetteur.notify.telegram import TelegramNotifier
        from guetteur.notify.telegram_bot import TelegramBot
        from guetteur.summarize import build_summarizer
        from guetteur.summarize.qa import build_answerer_from_summarizer

        raw_notifier = build_notifier("telegram", config)
        if not isinstance(raw_notifier, TelegramNotifier):  # pragma: no cover - défensif
            log.info("telegram_bot.disabled_wrong_notifier")
            return None
        notifier = raw_notifier
        summarizer = build_summarizer(config)
        answerer = build_answerer_from_summarizer(summarizer)

        def playlist_for(playlist_id: str) -> PlaylistConfig:
            try:
                return config.playlist(playlist_id)
            except ConfigError:
                return PlaylistConfig(id=playlist_id, label=playlist_id)

        bot = TelegramBot(
            config=config,
            store=store,
            summarizer=summarizer,
            question_answerer=answerer,
            claude_lock=claude_lock,
            notifier=notifier,
            get_playlist=playlist_for,
        )
        bot.start()
        return bot
    except Exception:
        log.exception("telegram_bot.start_failed")
        return None


def cmd_backfill(
    config: Config,
    playlist_id: str,
    limit: int,
    force: bool,
    detail: DetailLevel | None = None,
) -> int:
    store = Store(config.db_path)
    try:
        pipeline = build_pipeline(config, store, detail_override=detail)
        stats = pipeline.backfill(playlist_id, limit, force=force)
    finally:
        store.close()
    return 1 if stats.failed or stats.aborted else 0


def cmd_status(config: Config, limit: int, status: str | None) -> int:
    from guetteur.tables import render_rows, truncate

    store = Store(config.db_path)
    try:
        records = store.list_videos(limit=limit, status=Status(status) if status else None)
        counts = store.counts()
        beat = store.heartbeat()
    finally:
        store.close()
    labels = {p.id: p.label for p in config.playlists}
    archive_enabled = config.archive.enabled
    rows = [
        [
            r.video_id,
            truncate(r.title, 40),
            truncate(labels.get(r.playlist_id, r.playlist_id), 20),
            r.status.value if r.status is not Status.SENT or r.sent_at else "sent (ignorée)",
            str(r.retries),
            _archive_label(r.is_archived, r.really_sent, archive_enabled),
            truncate(r.last_error, 70),
        ]
        for r in records
    ]
    if rows:
        print(
            render_rows(
                ["id", "titre", "playlist", "statut", "retries", "archive", "dernière erreur"],
                rows,
            )
        )
    else:
        print("Aucune vidéo.")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "base vide"
    print(f"\nTotal : {summary}")
    print(f"Dernier cycle : {beat.isoformat(timespec='seconds') if beat else 'jamais'}")
    return 0


def _archive_label(is_archived: bool, really_sent: bool, enabled: bool) -> str:
    if is_archived:
        return "oui"
    if not enabled:
        return "—"
    return "non" if really_sent else "—"


def cmd_retry(config: Config, video_id: str | None) -> int:
    store = Store(config.db_path)
    try:
        if video_id is not None:
            record = store.get(video_id)
            if record is None:
                print(f"❌ Vidéo inconnue : {video_id}", file=sys.stderr)
                return 1
            if record.status is not Status.FAILED:
                print(f"→ {video_id} n'est pas « failed » (statut : {record.status.value}).")
                return 0
        ids = store.retry_failed(video_id)
    finally:
        store.close()
    for vid in ids:
        log.info("video.retry_requested", extra={"video_id": vid})
    print(f"✅ {len(ids)} vidéo(s) remise(s) en file : {', '.join(ids) or '—'}")
    if ids:
        print("   Elles partiront au prochain cycle (service) ou avec : guetteur once")
    return 0


def cmd_reset(config: Config, video_id: str, detail: DetailLevel | None = None) -> int:
    store = Store(config.db_path)
    before = store.reset(video_id)
    if before is None:
        store.close()
        print(f"❌ Vidéo inconnue : {video_id}", file=sys.stderr)
        return 1
    log.info("video.reset", extra={"video_id": video_id, "previous_status": before.status.value})
    print(f"✅ {video_id} remise en « new » (était : {before.status.value}).")
    if before.really_sent:
        print("⚠️  Cette vidéo avait déjà été envoyée : elle le sera une seconde fois.")
    if detail is None:
        store.close()
        print("   Transcription, résumé et envoi seront refaits au prochain cycle.")
        return 0
    # --detail : on lance un cycle immédiat avec l'override pour retraiter la vidéo avec
    # le niveau choisi. La surcharge n'est pas persistée : les cycles suivants
    # retomberont sur le niveau de la playlist.
    print(f"   Retraitement immédiat en niveau « {detail} »…")
    try:
        stats = build_pipeline(config, store, detail_override=detail).run_cycle()
    finally:
        store.close()
    return 1 if stats.failed or stats.aborted else 0


def cmd_archive(config: Config, video_id: str | None, pending: bool) -> int:
    """`guetteur archive` : archive une vidéo dans NotebookLM ou rattrape le retard.

    - `--video-id X` : archive une vidéo précise, doit être « sent » et non déjà archivée.
    - `--pending`    : archive toutes les vidéos `sent` sans archived_at (par sent_at asc).

    Retour 0 si tout est archivé, 1 si au moins une échoue. L'échec d'une vidéo ne bloque
    pas les suivantes ; la raison est loggée avec redaction et archived_at reste NULL."""
    from guetteur.archive.base import ArchiveError
    from guetteur.summarize.base import summary_from_json
    from guetteur.summarize.format import render_markdown

    if not config.archive.enabled:
        print("❌ archive.enabled = false dans config.toml", file=sys.stderr)
        return 1

    labels = {p.id: p.label for p in config.playlists}

    store = Store(config.db_path)
    try:
        if video_id is not None:
            record = store.get(video_id)
            if record is None:
                print(f"❌ Vidéo inconnue : {video_id}", file=sys.stderr)
                return 1
            if not record.really_sent:
                print(f"→ {video_id} n'est pas « sent » (statut : {record.status.value}).")
                return 1
            if record.is_archived:
                print(f"→ {video_id} est déjà archivée ({record.archived_at}).")
                return 0
            records = [record]
        elif pending:
            records = store.pending_archive(limit=1000)
            if not records:
                print("→ Aucune vidéo « sent » sans archive en attente.")
                return 0
        else:
            print("❌ précisez --video-id ou --pending", file=sys.stderr)
            return 2

        archiver = build_archiver(config, store)
        successes = 0
        failures = 0
        for rec in records:
            video = rec.to_video()
            summary_json = rec.summary
            if summary_json is None:
                print(f"⚠️  {rec.video_id} : résumé absent, ignorée", file=sys.stderr)
                failures += 1
                continue
            try:
                summary = summary_from_json(summary_json)
                body = render_markdown(summary, video, labels.get(rec.playlist_id, ""))
                outcome = archiver.archive(video, body)
            except ArchiveError as exc:
                print(f"❌ {rec.video_id} : {exc}", file=sys.stderr)
                log.error(
                    "archive.cli_failed",
                    extra={
                        "video_id": rec.video_id,
                        "retryable": exc.retryable,
                        "error": str(exc),
                    },
                )
                failures += 1
                continue
            store.mark_archived(rec.video_id)
            print(f"✅ {rec.video_id} → note {outcome.note_id}")
            log.info(
                "archive.cli_ok",
                extra={
                    "video_id": rec.video_id,
                    "notebook_id": outcome.notebook_id,
                    "note_id": outcome.note_id,
                },
            )
            successes += 1
        print(f"\nTotal : {successes} archivée(s), {failures} en échec")
        return 0 if failures == 0 else 1
    finally:
        store.close()


def cmd_health(config: Config, alert: bool) -> int:
    from guetteur.health import check_health, maybe_alert
    from guetteur.tables import render_rows

    store = Store(config.db_path)
    try:
        checks = check_health(config, store)
        print(
            render_rows(
                ["vérification", "état", "détail"],
                [[c.name, "OK" if c.ok else "KO", c.detail] for c in checks],
            )
        )
        if alert:
            outcome = maybe_alert(store, checks, lambda: build_notifier("telegram", config))
            print(f"Alerte : {outcome}")
    finally:
        store.close()
    return 0 if all(c.ok for c in checks) else 1


def _sample_message() -> tuple[Summary, Video]:
    video = Video(
        video_id="dQw4w9WgXcQ",
        title="Vidéo de test",
        channel="GUETTEUR",
        published=None,
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    summary = Summary(
        title="Test de notification GUETTEUR (caractères spéciaux : _*[]()~`>#+-=|{}.!)",
        tldr="Ceci est un message de test. Si vous le lisez, le canal fonctionne.",
        key_points=(
            KeyPoint(0, "Premier point, horodaté au début."),
            KeyPoint(43, "Deuxième point, lien cliquable vers 0:43."),
            KeyPoint(3725, "Troisième point, au-delà d'une heure (1:02:05)."),
        ),
        why_it_matters="Vérifier l'échappement MarkdownV2 et les liens avant la mise en service.",
        reading_time_minutes=1,
    )
    return summary, video


def _parse_detail_filter(raw: str | None) -> tuple[DetailLevel, ...]:
    """Parse la valeur de `guetteur compare --detail`.

    - Non fourni (None ou vide) → les trois niveaux.
    - Un ou plusieurs niveaux séparés par des virgules (`bref` ou `bref,detaille`).
    - Ordre du CLI conservé et doublons ignorés.
    - Une valeur inconnue lève `ConfigError` (message clair pour l'utilisateur)."""
    if not raw:
        return DETAIL_LEVELS
    seen: list[DetailLevel] = []
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        if item not in DETAIL_LEVELS:
            raise ConfigError(
                f"--detail : « {item} » n'est pas un niveau valide "
                f"(attendu : {', '.join(DETAIL_LEVELS)})"
            )
        if item not in seen:
            # Le contrôle `item in DETAIL_LEVELS` ci-dessus a déjà rétréci le type.
            seen.append(item)
    return tuple(seen) or DETAIL_LEVELS


def cmd_export(config: Config, video_id: str | None, pending: bool) -> int:
    """`guetteur export` : écrit la note Obsidian pour une vidéo (ou pour toutes les
    vidéos `sent` sans note). Idempotent. Ne modifie ni le statut ni l'archivage."""
    from guetteur.export.obsidian import ObsidianExporter
    from guetteur.summarize.base import summary_from_json

    if not config.obsidian.enabled:
        print("❌ obsidian.enabled = false dans config.toml", file=sys.stderr)
        return 1

    store = Store(config.db_path)
    try:
        if video_id is not None:
            record = store.get(video_id)
            if record is None:
                print(f"❌ Vidéo inconnue : {video_id}", file=sys.stderr)
                return 1
            records = [record]
        elif pending:
            records = store.pending_exports(limit=1000)
            if not records:
                print("→ Aucune vidéo sent sans note Obsidian.")
                return 0
        else:
            print("❌ précisez --video-id ou --pending", file=sys.stderr)
            return 2

        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        successes = 0
        failures = 0
        for rec in records:
            if rec.summary is None:
                print(
                    f"⚠️  {rec.video_id} : résumé absent, ignorée",
                    file=sys.stderr,
                )
                failures += 1
                continue
            video = rec.to_video()
            summary = summary_from_json(rec.summary)
            scores = store.applicability_for(rec.video_id)
            projets_scores = [(slug, score, idea) for slug, score, idea, *_ in scores]
            try:
                result = exporter.export_note(
                    video=video,
                    summary=summary,
                    detail=summary.detail,
                    theme="",
                    tags=[],
                    tags_proposes=[],
                    projets_scores=projets_scores,
                )
            except Exception as exc:
                print(f"❌ {rec.video_id} : {exc}", file=sys.stderr)
                failures += 1
                continue
            print(f"✅ {rec.video_id} → {result.path}")
            successes += 1
            # Applicabilité idempotente si présente en base.
            for slug, score, idea, integration, effort, _risks, prompt in scores:
                if score >= config.applicability.idea_threshold and prompt:
                    exporter.append_idea(video, slug, score, idea, integration, effort, prompt)
        print(f"\nTotal : {successes} exportée(s), {failures} en échec")
        return 0 if failures == 0 else 1
    finally:
        store.close()


def cmd_applicability(config: Config, video_id: str | None, pending: bool) -> int:
    """`guetteur applicability` : relance la seconde passe Claude pour une vidéo (ou
    pour toutes les `sent` sans score)."""
    from guetteur.export.obsidian import ObsidianExporter
    from guetteur.summarize import build_summarizer
    from guetteur.summarize.applicability import build_evaluator_from_summarizer
    from guetteur.summarize.base import summary_from_json

    if not config.applicability.enabled:
        print("❌ applicability.enabled = false dans config.toml", file=sys.stderr)
        return 1

    store = Store(config.db_path)
    try:
        if video_id is not None:
            record = store.get(video_id)
            if record is None:
                print(f"❌ Vidéo inconnue : {video_id}", file=sys.stderr)
                return 1
            records = [record]
        elif pending:
            records = store.pending_applicability(limit=1000)
            if not records:
                print("→ Aucune vidéo sent sans applicabilité en attente.")
                return 0
        else:
            print("❌ précisez --video-id ou --pending", file=sys.stderr)
            return 2

        exporter = ObsidianExporter(config, store)
        exporter.ensure_vault_layout()
        sheets = exporter.load_project_sheets()
        if not sheets:
            print("❌ Aucune fiche projet trouvée dans Projets/", file=sys.stderr)
            return 1
        summarizer = build_summarizer(config)
        evaluator = build_evaluator_from_summarizer(summarizer)
        successes = 0
        for rec in records:
            if rec.summary is None:
                continue
            video = rec.to_video()
            summary = summary_from_json(rec.summary)
            try:
                pertinences = evaluator.evaluate(video, summary, sheets)
            except Exception as exc:
                print(f"❌ {rec.video_id} : {exc}", file=sys.stderr)
                continue
            store.clear_applicability(rec.video_id)
            for p in pertinences:
                store.upsert_applicability(
                    rec.video_id,
                    p.projet,
                    p.score,
                    p.idee,
                    p.integration,
                    p.effort,
                    p.risques,
                    p.prompt_claude_code,
                )
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
            best = [(p.projet, p.score) for p in pertinences if p.score >= 1]
            hint = ", ".join(f"{s}({sc})" for s, sc in best) or "aucune pertinence"
            print(f"✅ {rec.video_id} → {hint}")
            successes += 1
        print(f"\nTotal : {successes} évaluée(s)")
        return 0
    finally:
        store.close()


def cmd_compare(
    config: Config,
    video_id: str,
    channel: NotifyChannel | None = None,
    detail: str | None = None,
) -> int:
    """Génère les niveaux demandés pour la même vidéo et envoie chacun sur le canal
    choisi (défaut : le canal de la playlist de la vidéo), avec un en-tête « [BREF] »,
    « [STANDARD] », « [DETAILLE] » en tête du texte. Ne touche PAS au statut de la vidéo
    en base : le résumé de comparaison n'est jamais persisté. `detail` accepte une chaîne
    séparée par virgules (par ex. « bref,detaille ») ; défaut = les trois niveaux."""
    from guetteur.pipeline import transcript_from_json
    from guetteur.summarize.base import SummarizeError, SummaryMeta

    try:
        levels = _parse_detail_filter(detail)
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    store = Store(config.db_path)
    try:
        record = store.get(video_id)
        if record is None:
            print(f"❌ Vidéo inconnue : {video_id}", file=sys.stderr)
            return 1
        if record.transcript is None:
            print(
                f"❌ {video_id} n'a pas encore de transcription en base — lancez d'abord "
                "`guetteur once` (ou `backfill --playlist … --limit 1`).",
                file=sys.stderr,
            )
            return 1

        try:
            playlist = config.playlist(record.playlist_id)
        except ConfigError:
            playlist = PlaylistConfig(id=record.playlist_id, label=record.playlist_id)
        target_channel: NotifyChannel = channel or playlist.notify
        transcript = transcript_from_json(video_id, record.transcript)
        video = record.to_video()
    finally:
        store.close()

    from guetteur.summarize import build_summarizer

    summarizer = build_summarizer(config)
    notifier = build_notifier(target_channel, config)
    code = 0
    for level in levels:
        header = f"[{level.upper()}]"
        print(f"→ génération {header}…")
        try:
            summary = summarizer.summarize(
                transcript,
                SummaryMeta(video=video, language=playlist.language, detail=level),
            )
        except SummarizeError as exc:
            print(f"❌ {header} : {exc}", file=sys.stderr)
            code = 1
            continue
        message = _prepend_header(build_message(summary, video, playlist.label), header)
        try:
            notifier.send(message)
            log.info(
                "compare.sent",
                extra={"video_id": video_id, "detail": level, "channel": target_channel},
            )
            print(f"✅ {header} envoyé sur {target_channel}")
        except NotifyError as exc:
            print(f"❌ {header} : {exc}", file=sys.stderr)
            log.error(
                "compare.failed",
                extra={"video_id": video_id, "detail": level, "error": str(exc)},
            )
            code = 1
    return code


def _prepend_header(message: Message, header: str) -> Message:
    """Ajoute un en-tête « [BREF] » (ou similaire) en tête de chaque part.
    Les caractères MarkdownV2 sensibles du header sont échappés."""
    from guetteur.summarize.format import escape_markdown_v2

    md_header = escape_markdown_v2(header)
    md_parts = message.markdown_v2_parts or (message.markdown_v2,)
    plain_parts = message.plain_parts or (message.plain,)
    new_md = tuple(f"{md_header}\n{p}" for p in md_parts)
    new_plain = tuple(f"{header}\n{p}" for p in plain_parts)
    return Message(
        markdown_v2=new_md[0] if len(new_md) == 1 else "\n\n".join(new_md),
        plain=new_plain[0] if len(new_plain) == 1 else "\n\n".join(new_plain),
        short=f"{header} {message.short}".strip(),
        markdown_v2_parts=new_md if len(new_md) > 1 else (),
        plain_parts=new_plain if len(new_plain) > 1 else (),
    )


def cmd_test_notify(config: Config, channel: NotifyChannel | None) -> int:
    channels: list[NotifyChannel]
    if channel:
        channels = [channel]
    else:
        channels = sorted({p.notify for p in config.playlists}) or ["telegram"]
    summary, video = _sample_message()
    message = build_message(summary, video, "test-notify")
    code = 0
    for ch in channels:
        try:
            build_notifier(ch, config).send(message)
            log.info("test_notify.ok", extra={"channel": ch})
            print(f"✅ {ch} : message de test envoyé")
        except NotifyError as exc:
            log.error("test_notify.failed", extra={"channel": ch, "error": str(exc)})
            print(f"❌ {ch} : {exc}", file=sys.stderr)
            code = 1
    return code


def cmd_doctor(config: Config) -> int:
    from guetteur.doctor import render_table, run_checks

    checks = run_checks(config)
    print(render_table(checks))
    return 0 if all(c.ok for c in checks) else 1


def _parse_since_until(raw: str | None, name: str) -> Any:
    """Parse `YYYY-MM-DD` en datetime UTC ; renvoie None si `raw` est vide."""
    if not raw:
        return None
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    try:
        return _dt.fromisoformat(raw).replace(tzinfo=_UTC)
    except ValueError as exc:
        raise ConfigError(f"--{name} : date invalide (YYYY-MM-DD attendu) : {raw!r}") from exc


def cmd_livre_create(config: Config, args: argparse.Namespace) -> int:
    """Étape 1 (résolution + estimation) + étape 2 (persistance) sous confirmation.
    Sans --yes, affiche le plan et quitte (l'utilisateur relance avec --yes)."""
    from guetteur.jobs.livre import (
        JobAlreadyRunningError,
        persist_new_livre,
        plan_book,
    )
    from guetteur.sources.channel import ChannelFilters

    filters = ChannelFilters(
        min_duration_s=args.min_duration,
        max_duration_s=args.max_duration,
        since=_parse_since_until(args.since, "since"),
        until=_parse_since_until(args.until, "until"),
        include_shorts=args.include_shorts,
        max_videos=args.max_videos or config.livre.max_videos_default,
    )
    try:
        plan = plan_book(
            config,
            args.channel,
            title=args.title,
            filters=filters,
            detail=args.detail,
        )
    except Exception as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    print(plan.render())
    if not args.yes:
        print(
            "\nRelancez avec --yes pour créer le job "
            "(puis `guetteur livre run <id>` pour démarrer)."
        )
        return 0
    store = Store(config.db_path)
    try:
        try:
            livre_id = persist_new_livre(store, plan, args.channel)
        except JobAlreadyRunningError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
    finally:
        store.close()
    print(f"\n✅ Livre {livre_id} créé. `guetteur livre run {livre_id}` pour démarrer.")
    return 0


def cmd_livre_run(config: Config, livre_id: int) -> int:
    """Résume les vidéos puis assemble et convertit le livre. Sur usage_limit,
    le job est mis en `paused` avec `resume_after` — l'utilisateur relance
    `guetteur livre resume <id>` puis `run <id>`."""
    import subprocess as _sp
    import threading

    from guetteur.jobs.livre import (
        BookAssembler,
        LivreRunner,
        finalize,
        livre_status_text,
    )
    from guetteur.summarize import build_summarizer
    from guetteur.transcript import Transcriber
    from guetteur.transcript.whisper import WhisperTranscriber

    store = Store(config.db_path)
    try:
        row = store.get_livre(livre_id)
        if row is None:
            print(f"❌ Livre {livre_id} inconnu", file=sys.stderr)
            return 2
        whisper = (
            WhisperTranscriber(config.transcript.whisper_model)
            if config.transcript.whisper_enabled
            else None
        )
        transcriber = Transcriber(config.transcript.languages, whisper=whisper)
        summarizer = build_summarizer(config)
        claude_lock = threading.Lock()
        runner = LivreRunner(
            config=config,
            store=store,
            transcriber=transcriber,
            summarizer=summarizer,
            claude_lock=claude_lock,
            progress=lambda msg: print(msg),
        )
        outcome = runner.run(livre_id)
        if outcome != "summarized":
            print(livre_status_text(store, livre_id))
            return 0 if outcome == "done" else 1
        # Assembler ne dépend PAS des méthodes internes du summarizer par défaut :
        # en prod on branche un adaptateur `raw_call` sur le client Claude.
        assembler = BookAssembler(config, store, summarizer, claude_lock=claude_lock)
        try:
            output = assembler.build(livre_id)
        except Exception as exc:
            store.set_livre_status(
                livre_id, "failed", last_error=f"{type(exc).__name__}: {exc}"
            )
            print(f"❌ Assemblage : {exc}", file=sys.stderr)
            return 2

        def pandoc_call(cmd: list[str]) -> _sp.CompletedProcess[str]:
            return _sp.run(cmd, capture_output=True, text=True, check=False)

        final = finalize(output, title=str(row["title"]), pandoc_runner=pandoc_call)
        store.set_livre_status(livre_id, "done", finished=True)
        print(f"✅ Livre {livre_id} prêt : {final.livre_md}")
        if final.epub:
            print(f"   EPUB : {final.epub}")
        if final.pdf:
            print(f"   PDF  : {final.pdf}")
    finally:
        store.close()
    return 0


def cmd_livre_status(config: Config, livre_id: int) -> int:
    from guetteur.jobs.livre import livre_status_text

    store = Store(config.db_path)
    try:
        print(livre_status_text(store, livre_id))
    finally:
        store.close()
    return 0


def cmd_livre_list(config: Config) -> int:
    from guetteur.jobs.livre import list_livres_text

    store = Store(config.db_path)
    try:
        print(list_livres_text(store))
    finally:
        store.close()
    return 0


def cmd_livre_control(config: Config, verb: str, livre_id: int) -> int:
    from guetteur.jobs.livre import cancel_livre, pause_livre, resume_livre

    dispatch = {"pause": pause_livre, "resume": resume_livre, "cancel": cancel_livre}
    if verb not in dispatch:
        print(f"❌ verbe inconnu : {verb}", file=sys.stderr)
        return 2
    store = Store(config.db_path)
    try:
        print(dispatch[verb](store, livre_id))
    finally:
        store.close()
    return 0


def cmd_auth(config: Config, port: int, bind: str) -> int:
    from guetteur.sources.api import run_oauth_flow

    try:
        run_oauth_flow(config.client_secret_path, config.token_path, port=port, bind_addr=bind)
    except SourceError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(f"✅ Jeton enregistré dans {config.token_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guetteur", description="Veille YouTube résumée par Claude."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("GUETTEUR_CONFIG", "config.toml")),
        help="chemin de config.toml (défaut : $GUETTEUR_CONFIG ou ./config.toml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    detail_help = f"niveau de détail du résumé ({', '.join(DETAIL_LEVELS)}) ; surcharge la playlist"
    sub.add_parser("run", help="service : un cycle toutes les N secondes")
    on = sub.add_parser("once", help="un seul cycle puis sortie")
    on.add_argument("--detail", choices=list(DETAIL_LEVELS), default=None, help=detail_help)
    bf = sub.add_parser("backfill", help="traite les dernières vidéos d'une playlist")
    bf.add_argument("--playlist", required=True, help="identifiant de la playlist")
    bf.add_argument("--limit", type=int, default=5, help="nombre de vidéos (défaut 5)")
    bf.add_argument(
        "--force", action="store_true", help="reprend aussi les vidéos « failed » de la playlist"
    )
    bf.add_argument("--detail", choices=list(DETAIL_LEVELS), default=None, help=detail_help)
    st = sub.add_parser("status", help="tableau des vidéos et de leur état")
    st.add_argument("--limit", type=int, default=30, help="nombre de lignes (défaut 30)")
    st.add_argument("--status", choices=[s.value for s in Status], default=None)
    rt = sub.add_parser("retry", help="remet en file les vidéos « failed »")
    target = rt.add_mutually_exclusive_group(required=True)
    target.add_argument("--video-id", help="une vidéo précise")
    target.add_argument("--all", action="store_true", help="toutes les vidéos « failed »")
    rs = sub.add_parser("reset", help="retraitement complet d'une vidéo (repasse en « new »)")
    rs.add_argument("--video-id", required=True)
    rs.add_argument(
        "--detail",
        choices=list(DETAIL_LEVELS),
        default=None,
        help="retraite immédiatement avec ce niveau (sinon au prochain cycle)",
    )
    cp = sub.add_parser(
        "compare",
        help="génère les 3 niveaux (bref/standard/detaille) pour une vidéo et envoie chacun",
    )
    cp.add_argument("--video-id", required=True)
    cp.add_argument("--channel", choices=["telegram", "whatsapp"], default=None)
    cp.add_argument(
        "--detail",
        default=None,
        help=(
            "restreint la comparaison à un ou plusieurs niveaux, séparés par virgules "
            f"({', '.join(DETAIL_LEVELS)}). Défaut : les trois."
        ),
    )
    he = sub.add_parser("health", help="base OK et dernier cycle récent (code retour 1 si KO)")
    he.add_argument(
        "--alert", action="store_true", help="alerte Telegram si KO (au plus une par heure)"
    )
    ar = sub.add_parser(
        "archive", help="archive une vidéo (ou toutes les « pending ») dans NotebookLM"
    )
    ar_target = ar.add_mutually_exclusive_group(required=True)
    ar_target.add_argument("--video-id", help="une vidéo précise (doit être « sent »)")
    ar_target.add_argument(
        "--pending", action="store_true", help="toutes les vidéos « sent » sans archived_at"
    )
    ex = sub.add_parser(
        "export", help="écrit la note Obsidian d'une vidéo (ou de toutes les sent sans note)"
    )
    ex_target = ex.add_mutually_exclusive_group(required=True)
    ex_target.add_argument("--video-id")
    ex_target.add_argument("--pending", action="store_true")
    ap = sub.add_parser(
        "applicability",
        help="score chaque projet chargé face au résumé (une vidéo ou toutes les sent)",
    )
    ap_target = ap.add_mutually_exclusive_group(required=True)
    ap_target.add_argument("--video-id")
    ap_target.add_argument("--pending", action="store_true")
    tn = sub.add_parser("test-notify", help="envoie un message de test")
    tn.add_argument("--channel", choices=["telegram", "whatsapp"], default=None)
    lv = sub.add_parser("livre", help="Lot 7 : compile une chaîne YouTube en ebook")
    lv_sub = lv.add_subparsers(dest="livre_verb", required=True)
    lv_create = lv_sub.add_parser("create", help="crée un job livre à partir d'une URL de chaîne")
    lv_create.add_argument("--channel", required=True, help="URL @handle, /channel/UC…, /c/…")
    lv_create.add_argument("--title", default=None)
    lv_create.add_argument(
        "--detail", choices=list(DETAIL_LEVELS), default=None, help=detail_help
    )
    lv_create.add_argument("--min-duration", type=int, default=None, help="secondes minimum")
    lv_create.add_argument("--max-duration", type=int, default=None, help="secondes maximum")
    lv_create.add_argument("--since", default=None, help="YYYY-MM-DD (inclus)")
    lv_create.add_argument("--until", default=None, help="YYYY-MM-DD (inclus)")
    lv_create.add_argument("--max-videos", type=int, default=None)
    lv_create.add_argument("--include-shorts", action="store_true")
    lv_create.add_argument("--yes", action="store_true", help="créer sans confirmation")
    lv_run = lv_sub.add_parser("run", help="lance ou reprend le job")
    lv_run.add_argument("livre_id", type=int)
    lv_status = lv_sub.add_parser("status", help="état d'un job")
    lv_status.add_argument("livre_id", type=int)
    lv_sub.add_parser("list", help="liste des livres en base")
    for verb in ("pause", "resume", "cancel"):
        p = lv_sub.add_parser(verb, help=f"{verb} un job")
        p.add_argument("livre_id", type=int)
    sub.add_parser("doctor", help="vérifie claude, ffmpeg, la base et les jetons")
    au = sub.add_parser("auth", help="autorisation OAuth pour les playlists privées")
    au.add_argument("--port", type=int, default=8765)
    au.add_argument("--bind", default="127.0.0.1", help="adresse d'écoute (0.0.0.0 dans Docker)")
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level)

    try:
        match args.command:
            case "run":
                return cmd_run(config)
            case "once":
                return cmd_once(config, args.detail)
            case "backfill":
                return cmd_backfill(config, args.playlist, args.limit, args.force, args.detail)
            case "status":
                return cmd_status(config, args.limit, args.status)
            case "retry":
                return cmd_retry(config, None if args.all else args.video_id)
            case "reset":
                return cmd_reset(config, args.video_id, args.detail)
            case "health":
                return cmd_health(config, args.alert)
            case "archive":
                return cmd_archive(config, args.video_id, args.pending)
            case "export":
                return cmd_export(config, args.video_id, args.pending)
            case "applicability":
                return cmd_applicability(config, args.video_id, args.pending)
            case "compare":
                return cmd_compare(config, args.video_id, args.channel, args.detail)
            case "test-notify":
                return cmd_test_notify(config, args.channel)
            case "doctor":
                return cmd_doctor(config)
            case "livre":
                match args.livre_verb:
                    case "create":
                        return cmd_livre_create(config, args)
                    case "run":
                        return cmd_livre_run(config, args.livre_id)
                    case "status":
                        return cmd_livre_status(config, args.livre_id)
                    case "list":
                        return cmd_livre_list(config)
                    case "pause" | "resume" | "cancel":
                        return cmd_livre_control(config, args.livre_verb, args.livre_id)
                    case _:
                        return 2
            case "auth":
                return cmd_auth(config, args.port, args.bind)
    except (ConfigError, SourceError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(cli())
