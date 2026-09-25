"""Point d'entrée CLI : guetteur run | once | backfill | status | retry | reset | health |
test-notify | doctor | auth."""

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

import schedule
from dotenv import load_dotenv

from guetteur.config import Config, ConfigError, NotifyChannel, PlaylistConfig, load_config
from guetteur.logs import setup_logging
from guetteur.models import KeyPoint, Summary, Video
from guetteur.notify import NotifyError, build_notifier
from guetteur.pipeline import Pipeline, build_message
from guetteur.sources.base import SourceError, VideoSource
from guetteur.store import Status, Store

log = logging.getLogger("guetteur")


def build_pipeline(config: Config, store: Store) -> Pipeline:
    from guetteur.sources.rss import RssSource
    from guetteur.summarize import build_summarizer
    from guetteur.transcript import Transcriber
    from guetteur.transcript.whisper import WhisperTranscriber

    summarizer = build_summarizer(config)  # ConfigError explicite si clé API manquante
    rss = RssSource()
    api_source: VideoSource | None = None

    def source_for(playlist: PlaylistConfig) -> VideoSource:
        nonlocal api_source
        if not playlist.private:
            return rss
        if api_source is None:
            from guetteur.sources.api import YouTubeApiSource

            api_source = YouTubeApiSource(config.token_path)
        return api_source

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
    )


def cmd_once(config: Config) -> int:
    store = Store(config.db_path)
    try:
        stats = build_pipeline(config, store).run_cycle()
    finally:
        store.close()
    return 1 if stats.failed or stats.aborted else 0


def cmd_run(config: Config) -> int:
    store = Store(config.db_path)
    pipeline = build_pipeline(config, store)
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
        store.close()
    return 0


def cmd_backfill(config: Config, playlist_id: str, limit: int, force: bool) -> int:
    store = Store(config.db_path)
    try:
        stats = build_pipeline(config, store).backfill(playlist_id, limit, force=force)
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
    rows = [
        [
            r.video_id,
            truncate(r.title, 40),
            truncate(labels.get(r.playlist_id, r.playlist_id), 20),
            r.status.value if r.status is not Status.SENT or r.sent_at else "sent (ignorée)",
            str(r.retries),
            truncate(r.last_error, 70),
        ]
        for r in records
    ]
    if rows:
        print(
            render_rows(["id", "titre", "playlist", "statut", "retries", "dernière erreur"], rows)
        )
    else:
        print("Aucune vidéo.")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "base vide"
    print(f"\nTotal : {summary}")
    print(f"Dernier cycle : {beat.isoformat(timespec='seconds') if beat else 'jamais'}")
    return 0


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


def cmd_reset(config: Config, video_id: str) -> int:
    store = Store(config.db_path)
    try:
        before = store.reset(video_id)
    finally:
        store.close()
    if before is None:
        print(f"❌ Vidéo inconnue : {video_id}", file=sys.stderr)
        return 1
    log.info("video.reset", extra={"video_id": video_id, "previous_status": before.status.value})
    print(f"✅ {video_id} remise en « new » (était : {before.status.value}).")
    print("   Transcription, résumé et envoi seront refaits au prochain cycle.")
    if before.really_sent:
        print("⚠️  Cette vidéo avait déjà été envoyée : elle le sera une seconde fois.")
    return 0


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
    sub.add_parser("run", help="service : un cycle toutes les N secondes")
    sub.add_parser("once", help="un seul cycle puis sortie")
    bf = sub.add_parser("backfill", help="traite les dernières vidéos d'une playlist")
    bf.add_argument("--playlist", required=True, help="identifiant de la playlist")
    bf.add_argument("--limit", type=int, default=5, help="nombre de vidéos (défaut 5)")
    bf.add_argument(
        "--force", action="store_true", help="reprend aussi les vidéos « failed » de la playlist"
    )
    st = sub.add_parser("status", help="tableau des vidéos et de leur état")
    st.add_argument("--limit", type=int, default=30, help="nombre de lignes (défaut 30)")
    st.add_argument("--status", choices=[s.value for s in Status], default=None)
    rt = sub.add_parser("retry", help="remet en file les vidéos « failed »")
    target = rt.add_mutually_exclusive_group(required=True)
    target.add_argument("--video-id", help="une vidéo précise")
    target.add_argument("--all", action="store_true", help="toutes les vidéos « failed »")
    rs = sub.add_parser("reset", help="retraitement complet d'une vidéo (repasse en « new »)")
    rs.add_argument("--video-id", required=True)
    he = sub.add_parser("health", help="base OK et dernier cycle récent (code retour 1 si KO)")
    he.add_argument(
        "--alert", action="store_true", help="alerte Telegram si KO (au plus une par heure)"
    )
    tn = sub.add_parser("test-notify", help="envoie un message de test")
    tn.add_argument("--channel", choices=["telegram", "whatsapp"], default=None)
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
                return cmd_once(config)
            case "backfill":
                return cmd_backfill(config, args.playlist, args.limit, args.force)
            case "status":
                return cmd_status(config, args.limit, args.status)
            case "retry":
                return cmd_retry(config, None if args.all else args.video_id)
            case "reset":
                return cmd_reset(config, args.video_id)
            case "health":
                return cmd_health(config, args.alert)
            case "test-notify":
                return cmd_test_notify(config, args.channel)
            case "doctor":
                return cmd_doctor(config)
            case "auth":
                return cmd_auth(config, args.port, args.bind)
    except (ConfigError, SourceError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(cli())
