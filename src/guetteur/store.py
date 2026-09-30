"""Persistance SQLite (data/guetteur.db).

Machine à états d'une vidéo :

    new → transcribed → summarized → sending → sent
                 ↘ retry ↗        ↘ failed (erreur définitive ou essais épuisés)

- `claim_for_sending` fait passer atomiquement une vidéo résumée en « sending »
  (horodatée par send_attempt_at). Deux processus ne peuvent donc pas l'envoyer en même temps.
- Succès : `mark_sent`. Échec : `release_claim` (retour en « summarized », essai compté) ou
  `mark_failed` (raison lisible dans last_error).
- Une vidéo restée en « sending » plus de 10 min (processus tué pendant l'envoi) repasse en
  « summarized » au cycle suivant (`recover_stale_sending`) ; le résumé est conservé.
- Une vidéo n'est « réellement envoyée » que si status = sent ET sent_at non NULL. Les vidéos
  ignorées au premier lancement sont « sent » avec sent_at NULL : un backfill peut les traiter.
- Chaque tentative d'envoi est tracée dans la table deliveries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from guetteur.models import Video

SCHEMA_VERSION = 4


class Status(StrEnum):
    NEW = "new"
    TRANSCRIBED = "transcribed"
    SUMMARIZED = "summarized"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    RETRY = "retry"


PENDING_STATUSES = (Status.NEW, Status.RETRY, Status.TRANSCRIBED, Status.SUMMARIZED)
_STATUS_CHECK = ", ".join(f"'{s.value}'" for s in Status)

_VIDEOS_DDL = f"""
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    playlist_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    published_at    TEXT,
    status          TEXT NOT NULL CHECK (status IN ({_STATUS_CHECK})),
    retries         INTEGER NOT NULL DEFAULT 0,
    transcript      TEXT,
    summary         TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    sent_at         TEXT,
    send_attempt_at TEXT,
    archived_at     TEXT
);
"""

_SCHEMA = (
    _VIDEOS_DDL
    + """
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status, created_at);
CREATE TABLE IF NOT EXISTS playlists (
    playlist_id    TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            TEXT NOT NULL,
    channel             TEXT NOT NULL,
    attempt             INTEGER NOT NULL,
    at                  TEXT NOT NULL,
    ok                  INTEGER NOT NULL,
    provider_message_id TEXT,
    error               TEXT,
    is_fallback         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deliveries_video ON deliveries(video_id, id);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Cache des résumés par niveau de détail (Lot 5). Chaque niveau est généré une
-- fois puis servi depuis ce cache (les boutons Bref/Standard/Détaillé du bot).
CREATE TABLE IF NOT EXISTS summaries (
    video_id     TEXT NOT NULL,
    detail       TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (video_id, detail)
);
-- Association message Telegram ↔ vidéo (Lot 5). Permet de retrouver la vidéo depuis
-- un reply ou un callback sur n'importe quel message envoyé par le bot.
CREATE TABLE IF NOT EXISTS telegram_messages (
    message_id INTEGER PRIMARY KEY,
    video_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_msg_video ON telegram_messages(video_id);
-- Historique des Q&A par vidéo (Lot 5). Les 6 derniers échanges sont réinjectés
-- pour permettre les relances.
CREATE TABLE IF NOT EXISTS qa (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_video ON qa(video_id, id);
-- Export Obsidian (Lot 6) : chemin de la note par vidéo (retrouvée même après
-- déplacement Inbox → Veille/<thème> ou Veille/_ecartes), statut et thème pour les
-- boutons Garder/Écarter, taxonomie corrigée par /theme.
CREATE TABLE IF NOT EXISTS obsidian_notes (
    video_id     TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'inbox',
    theme        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);
-- Applicabilité aux projets (Lot 6) : score et méga-prompt par (video_id, projet).
-- Cache pour éviter de rejouer la seconde passe Claude à chaque `guetteur applicability`.
CREATE TABLE IF NOT EXISTS applicability (
    video_id     TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    score        INTEGER NOT NULL,
    idea         TEXT NOT NULL DEFAULT '',
    integration  TEXT NOT NULL DEFAULT '',
    effort       TEXT NOT NULL DEFAULT '',
    risks        TEXT NOT NULL DEFAULT '',
    prompt       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (video_id, project_slug)
);
-- Idempotence des append dans Projets/<slug>/IDEES.md (Lot 6) : une seule ligne
-- écrite par couple (video_id, project_slug).
CREATE TABLE IF NOT EXISTS ideas_written (
    video_id     TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    at           TEXT NOT NULL,
    PRIMARY KEY (video_id, project_slug)
);
-- Lot 7 : jobs de compilation d'une chaîne YouTube en ebook. L'état d'un job
-- (pending/running/paused/done/failed/cancelled) survit à un crash — voir
-- `guetteur livre resume`. Le plan JSON et le livre assemblé sont mis en cache
-- pour permettre de relancer la sortie sans rejouer tous les appels Claude.
CREATE TABLE IF NOT EXISTS livres (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_url   TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    channel_name  TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT 'standard',
    filters_json  TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',
    plan_json     TEXT,
    book_md       TEXT,
    output_dir    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    resume_after  TEXT,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_livres_status ON livres(status, created_at);
-- Vidéos d'un livre : rang stable dans la liste initiale, statut individuel de
-- traitement (queued/summarized/failed/skipped). `livre_id + video_id` unique.
CREATE TABLE IF NOT EXISTS livre_videos (
    livre_id    INTEGER NOT NULL,
    video_id    TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    duration_s  INTEGER,
    published_at TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (livre_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_livre_videos_status
    ON livre_videos(livre_id, status, rank);
"""
)

# Colonnes de la table videos du Lot 1 (avant send_attempt_at et le statut « sending »).
_V1_COLUMNS = (
    "video_id, playlist_id, title, channel, url, published_at, status, retries, transcript, "
    "summary, last_error, created_at, updated_at, sent_at"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convertit une `sqlite3.Row` en dict — l'itération sur `Row` renvoie ses
    VALEURS (pas ses clefs), donc `{k: row[k] for k in row}` échoue avec
    IndexError. `row.keys()` est la seule voie propre."""
    return {k: row[k] for k in row.keys()}  # noqa: SIM118 — `.keys()` obligatoire ici


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now() -> str:
    return _iso(utcnow())


@dataclass(frozen=True)
class VideoRecord:
    video_id: str
    playlist_id: str
    title: str
    channel: str
    url: str
    published_at: str | None
    status: Status
    retries: int
    transcript: str | None
    summary: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    sent_at: str | None
    send_attempt_at: str | None
    archived_at: str | None = None

    @property
    def really_sent(self) -> bool:
        return self.status is Status.SENT and self.sent_at is not None

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def to_video(self) -> Video:
        published = datetime.fromisoformat(self.published_at) if self.published_at else None
        return Video(
            video_id=self.video_id,
            title=self.title,
            channel=self.channel,
            published=published,
            url=self.url,
        )


@dataclass(frozen=True)
class Delivery:
    id: int
    video_id: str
    channel: str
    attempt: int
    at: str
    ok: bool
    provider_message_id: str | None
    error: str | None
    is_fallback: bool


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` : le Lot 5 partage le store entre le thread du
        # pipeline principal et le thread du bot Telegram. La cohérence est assurée
        # par SQLite (WAL + busy_timeout) et par le verrou Claude côté applicatif.
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        """Lot 1 → Lot 2 : la contrainte CHECK du statut ne connaît pas « sending » et la
        colonne send_attempt_at manque. SQLite ne sait pas modifier une contrainte : on
        reconstruit la table en conservant toutes les lignes.
        Lot 2 → Lot 3 : ajoute la colonne archived_at (nullable, ALTER TABLE suffit)."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
        ).fetchone()
        if row is None:
            return  # base neuve : _SCHEMA crée tout
        sql = str(row["sql"])
        if "send_attempt_at" not in sql:
            with self._tx() as conn:
                conn.execute("ALTER TABLE videos RENAME TO videos_v1")
                conn.execute("DROP INDEX IF EXISTS idx_videos_status")
                conn.execute(_VIDEOS_DDL)
                conn.execute(
                    f"INSERT INTO videos ({_V1_COLUMNS}) SELECT {_V1_COLUMNS} FROM videos_v1"
                )
                conn.execute("DROP TABLE videos_v1")
            sql = str(
                self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
                ).fetchone()["sql"]
            )
        if "archived_at" not in sql:
            self._conn.execute("ALTER TABLE videos ADD COLUMN archived_at TEXT")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # --- playlists -------------------------------------------------------------------------

    def is_playlist_initialized(self, playlist_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM playlists WHERE playlist_id = ?", (playlist_id,)
        ).fetchone()
        return row is not None

    def initialize_playlist(self, playlist_id: str, existing: Sequence[Video]) -> int:
        """Premier passage : les vidéos déjà présentes sont marquées « sent » sans traitement
        (sent_at reste NULL : elles n'ont jamais été réellement envoyées)."""
        now = _now()
        inserted = 0
        with self._tx() as conn:
            for v in existing:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO videos (video_id, playlist_id, title, channel, url, "
                    "published_at, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (*self._video_cols(v, playlist_id), Status.SENT.value, now, now),
                )
                inserted += cur.rowcount
            conn.execute(
                "INSERT OR IGNORE INTO playlists (playlist_id, initialized_at) VALUES (?, ?)",
                (playlist_id, now),
            )
        return inserted

    # --- vidéos ----------------------------------------------------------------------------

    @staticmethod
    def _video_cols(v: Video, playlist_id: str) -> tuple[str, str, str, str, str, str | None]:
        published = v.published.isoformat() if v.published else None
        return (v.video_id, playlist_id, v.title, v.channel, v.url, published)

    def add_new(self, video: Video, playlist_id: str) -> bool:
        """Ajoute une vidéo « new » ; sans effet si elle est déjà connue (quel que soit son
        état)."""
        now = _now()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO videos (video_id, playlist_id, title, channel, url, "
            "published_at, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (*self._video_cols(video, playlist_id), Status.NEW.value, now, now),
        )
        return cur.rowcount == 1

    @staticmethod
    def _resume_status_sql() -> str:
        """Statut de reprise d'après ce qui est déjà en base : on ne refait jamais un travail
        acquis (résumé → summarized, transcription → transcribed, sinon new)."""
        return (
            "CASE WHEN summary IS NOT NULL THEN 'summarized' "
            "WHEN transcript IS NOT NULL THEN 'transcribed' ELSE 'new' END"
        )

    def requeue_for_backfill(self, video: Video, playlist_id: str, force: bool = False) -> bool:
        """Remet en file une vidéo pour un backfill explicite : les vidéos ignorées au premier
        lancement, et les « failed » si force=True. Une vidéo réellement envoyée ne l'est
        jamais une seconde fois."""
        if self.add_new(video, playlist_id):
            return True
        statuses = [Status.SENT.value] + ([Status.FAILED.value] if force else [])
        placeholders = ",".join("?" for _ in statuses)
        cur = self._conn.execute(
            f"UPDATE videos SET status = {self._resume_status_sql()}, retries = 0, "
            "last_error = NULL, send_attempt_at = NULL, updated_at = ? "
            f"WHERE video_id = ? AND sent_at IS NULL AND status IN ({placeholders})",
            (_now(), video.video_id, *statuses),
        )
        return cur.rowcount == 1

    def retry_failed(self, video_id: str | None = None) -> list[str]:
        """`guetteur retry` : remet les vidéos « failed » en file (summarized si le résumé
        existe) avec retries = 0. video_id=None : toutes. Retourne les identifiants repris."""
        sql = "SELECT video_id FROM videos WHERE status = ?"
        params: list[str] = [Status.FAILED.value]
        if video_id is not None:
            sql += " AND video_id = ?"
            params.append(video_id)
        with self._tx() as conn:
            ids = [str(r["video_id"]) for r in conn.execute(sql, params).fetchall()]
            for vid in ids:
                conn.execute(
                    f"UPDATE videos SET status = {self._resume_status_sql()}, retries = 0, "
                    "last_error = NULL, send_attempt_at = NULL, updated_at = ? "
                    "WHERE video_id = ?",
                    (_now(), vid),
                )
        return ids

    def reset(self, video_id: str) -> VideoRecord | None:
        """`guetteur reset` : retraitement complet (transcription, résumé, envoi). Retourne
        l'état AVANT remise à zéro, ou None si la vidéo est inconnue."""
        before = self.get(video_id)
        if before is None:
            return None
        self._conn.execute(
            "UPDATE videos SET status = ?, retries = 0, transcript = NULL, summary = NULL, "
            "last_error = NULL, sent_at = NULL, send_attempt_at = NULL, updated_at = ? "
            "WHERE video_id = ?",
            (Status.NEW.value, _now(), video_id),
        )
        return before

    def get(self, video_id: str) -> VideoRecord | None:
        row = self._conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        return self._record(row) if row else None

    def pending(self, limit: int, playlist_ids: Sequence[str] | None = None) -> list[VideoRecord]:
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        sql = f"SELECT * FROM videos WHERE status IN ({placeholders})"
        params: list[str | int] = [s.value for s in PENDING_STATUSES]
        if playlist_ids is not None:
            sql += f" AND playlist_id IN ({','.join('?' for _ in playlist_ids)})"
            params.extend(playlist_ids)
        sql += " ORDER BY created_at, COALESCE(published_at, '') LIMIT ?"
        params.append(limit)
        return [self._record(r) for r in self._conn.execute(sql, params).fetchall()]

    def list_videos(self, limit: int = 50, status: Status | None = None) -> list[VideoRecord]:
        sql = "SELECT * FROM videos"
        params: list[str | int] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.value)
        sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        return [self._record(r) for r in self._conn.execute(sql, params).fetchall()]

    def set_transcript(self, video_id: str, transcript: str) -> None:
        self._update(video_id, status=Status.TRANSCRIBED, transcript=transcript)

    def set_summary(self, video_id: str, summary: str) -> None:
        self._update(video_id, status=Status.SUMMARIZED, summary=summary)

    def mark_retry(self, video_id: str, error: str) -> int:
        """Incrémente le compteur d'échecs et passe en « retry ». Retourne le nouveau compteur."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, retries = retries + 1, last_error = ?, "
                "updated_at = ? WHERE video_id = ? AND status NOT IN (?, ?)",
                (
                    Status.RETRY.value,
                    error,
                    _now(),
                    video_id,
                    Status.SENT.value,
                    Status.SENDING.value,
                ),
            )
            row = conn.execute(
                "SELECT retries FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return int(row["retries"]) if row else 0

    def mark_failed(self, video_id: str, error: str) -> bool:
        cur = self._conn.execute(
            "UPDATE videos SET status = ?, last_error = ?, send_attempt_at = NULL, "
            "updated_at = ? WHERE video_id = ? AND status NOT IN (?, ?)",
            (Status.FAILED.value, error, _now(), video_id, Status.SENT.value, Status.FAILED.value),
        )
        return cur.rowcount == 1

    # --- envoi -----------------------------------------------------------------------------

    def claim_for_sending(self, video_id: str) -> bool:
        """Transition atomique (résumé prêt) → sending. False si la vidéo est déjà en cours
        d'envoi, envoyée, abandonnée, ou sans résumé."""
        now = _now()
        cur = self._conn.execute(
            "UPDATE videos SET status = ?, send_attempt_at = ?, updated_at = ? "
            "WHERE video_id = ? AND summary IS NOT NULL AND sent_at IS NULL "
            "AND status NOT IN (?, ?, ?)",
            (
                Status.SENDING.value,
                now,
                now,
                video_id,
                Status.SENDING.value,
                Status.SENT.value,
                Status.FAILED.value,
            ),
        )
        return cur.rowcount == 1

    def mark_sent(self, video_id: str) -> bool:
        now = _now()
        cur = self._conn.execute(
            "UPDATE videos SET status = ?, sent_at = ?, last_error = NULL, updated_at = ? "
            "WHERE video_id = ? AND status = ?",
            (Status.SENT.value, now, now, video_id, Status.SENDING.value),
        )
        return cur.rowcount == 1

    def release_claim(self, video_id: str, error: str) -> int:
        """Envoi en échec transitoire : la vidéo repasse « summarized » avec un échec compté."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, send_attempt_at = NULL, retries = retries + 1, "
                "last_error = ?, updated_at = ? WHERE video_id = ? AND status = ?",
                (Status.SUMMARIZED.value, error, _now(), video_id, Status.SENDING.value),
            )
            row = conn.execute(
                "SELECT retries FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return int(row["retries"]) if row else 0

    def recover_stale_sending(
        self, older_than: timedelta, now: datetime | None = None
    ) -> list[str]:
        """Vidéos bloquées en « sending » (processus tué pendant l'envoi) : retour en
        « summarized », résumé conservé, aucun rappel Claude."""
        cutoff = _iso((now or utcnow()) - older_than)
        with self._tx() as conn:
            ids = [
                str(r["video_id"])
                for r in conn.execute(
                    "SELECT video_id FROM videos WHERE status = ? "
                    "AND (send_attempt_at IS NULL OR send_attempt_at < ?)",
                    (Status.SENDING.value, cutoff),
                ).fetchall()
            ]
            for vid in ids:
                conn.execute(
                    "UPDATE videos SET status = ?, send_attempt_at = NULL, "
                    "last_error = 'envoi interrompu (sending périmé), repris', updated_at = ? "
                    "WHERE video_id = ?",
                    (Status.SUMMARIZED.value, _now(), vid),
                )
        return ids

    def add_delivery(
        self,
        video_id: str,
        channel: str,
        attempt: int,
        ok: bool,
        provider_message_id: str | None = None,
        error: str | None = None,
        is_fallback: bool = False,
    ) -> None:
        self._conn.execute(
            "INSERT INTO deliveries (video_id, channel, attempt, at, ok, provider_message_id, "
            "error, is_fallback) VALUES (?,?,?,?,?,?,?,?)",
            (
                video_id,
                channel,
                attempt,
                _now(),
                int(ok),
                provider_message_id,
                error,
                int(is_fallback),
            ),
        )

    def deliveries(self, video_id: str | None = None) -> list[Delivery]:
        sql = "SELECT * FROM deliveries"
        params: list[str] = []
        if video_id is not None:
            sql += " WHERE video_id = ?"
            params.append(video_id)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [
            Delivery(
                id=int(r["id"]),
                video_id=str(r["video_id"]),
                channel=str(r["channel"]),
                attempt=int(r["attempt"]),
                at=str(r["at"]),
                ok=bool(r["ok"]),
                provider_message_id=r["provider_message_id"],
                error=r["error"],
                is_fallback=bool(r["is_fallback"]),
            )
            for r in rows
        ]

    # --- archivage (Lot 3) -----------------------------------------------------------------

    def mark_archived(self, video_id: str, now: datetime | None = None) -> bool:
        """Marque une vidéo comme archivée dans NotebookLM (archived_at horodaté).
        Ne modifie pas le statut de l'envoi ; seule la vidéo `sent` a un sens ici."""
        stamp = _iso(now or utcnow())
        cur = self._conn.execute(
            "UPDATE videos SET archived_at = ?, updated_at = ? "
            "WHERE video_id = ? AND status = ? AND archived_at IS NULL",
            (stamp, stamp, video_id, Status.SENT.value),
        )
        return cur.rowcount == 1

    def pending_archive(self, limit: int = 50) -> list[VideoRecord]:
        """Vidéos réellement envoyées (sent + sent_at non NULL) mais pas encore archivées."""
        rows = self._conn.execute(
            "SELECT * FROM videos WHERE status = ? AND sent_at IS NOT NULL "
            "AND archived_at IS NULL ORDER BY sent_at ASC LIMIT ?",
            (Status.SENT.value, limit),
        ).fetchall()
        return [self._record(r) for r in rows]

    # --- meta (heartbeat, alertes, archive) ------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def beat(self, now: datetime | None = None) -> None:
        self.set_meta("heartbeat", _iso(now or utcnow()))

    def heartbeat(self) -> datetime | None:
        raw = self.get_meta("heartbeat")
        return datetime.fromisoformat(raw) if raw else None

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")
        return {str(r["status"]): int(r["n"]) for r in rows}

    # --- quota YouTube Data API (par jour PT) ----------------------------------------------
    #
    # Google reset le quota YouTube Data API à minuit heure du Pacifique (UTC-8/-7),
    # pas UTC. On stocke la date PT courante et le compteur ; toute lecture après un
    # changement de jour PT réinitialise le compteur et les drapeaux d'alerte.

    def _youtube_pt_day(self, now: datetime) -> str:
        # Approximation : Pacific = UTC-8 (heure standard). L'API est tolérante à
        # quelques heures de décalage — le compteur peut s'écouler jusqu'à une heure
        # avant/après minuit local. Un jour de plus ou de moins n'ouvre pas la porte
        # à un dépassement (le hard-limit reste à 9500/10000).
        pt = now.astimezone(UTC) - timedelta(hours=8)
        return pt.date().isoformat()

    def _youtube_quota_reset_if_new_day(self, now: datetime) -> str:
        day = self._youtube_pt_day(now)
        stored = self.get_meta("youtube_quota_day")
        if stored != day:
            self.set_meta("youtube_quota_day", day)
            self.set_meta("youtube_quota_used", "0")
            # Les alertes sont réarmées : nouvelle journée, nouveau plafond.
            self._conn.execute(
                "DELETE FROM meta WHERE key IN ('youtube_alert_8k', 'youtube_alert_9k5')"
            )
        return day

    def youtube_quota_used(self, now: datetime | None = None) -> int:
        """Nombre d'appels API consommés aujourd'hui (heure PT)."""
        self._youtube_quota_reset_if_new_day(now or utcnow())
        raw = self.get_meta("youtube_quota_used")
        return int(raw) if raw else 0

    def youtube_quota_bump(self, n: int = 1, now: datetime | None = None) -> int:
        """Incrémente le compteur de `n` et renvoie la nouvelle valeur."""
        current = self.youtube_quota_used(now)
        new_value = current + n
        self.set_meta("youtube_quota_used", str(new_value))
        return new_value

    def youtube_quota_mark_exhausted(self, hard_limit: int, now: datetime | None = None) -> None:
        """Force le compteur au plafond dur (utilisé quand l'API renvoie 403
        quotaExceeded : Google confirme qu'on n'a plus rien, quel que soit notre
        compteur local, on passe en mode RSS jusqu'au prochain jour PT)."""
        self._youtube_quota_reset_if_new_day(now or utcnow())
        self.set_meta("youtube_quota_used", str(hard_limit))

    def youtube_quota_alert_needed(self, level: str, now: datetime | None = None) -> bool:
        """Renvoie True au premier appel de la journée pour ce niveau (« 8k » ou
        « 9k5 »), False les suivants — permet de dé-doublonner les alertes Telegram."""
        self._youtube_quota_reset_if_new_day(now or utcnow())
        key = f"youtube_alert_{level}"
        if self.get_meta(key):
            return False
        self.set_meta(key, _iso(now or utcnow()))
        return True

    # --- bot Telegram (Lot 5) --------------------------------------------------------------

    def cache_summary(self, video_id: str, detail: str, summary_json: str) -> None:
        """Enregistre le résumé d'un niveau donné dans le cache multi-niveaux.
        Les résumés déjà présents pour ce (video_id, detail) sont écrasés."""
        self._conn.execute(
            "INSERT INTO summaries (video_id, detail, summary_json, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(video_id, detail) DO UPDATE SET "
            "summary_json = excluded.summary_json, created_at = excluded.created_at",
            (video_id, detail, summary_json, _now()),
        )

    def cached_summary(self, video_id: str, detail: str) -> str | None:
        row = self._conn.execute(
            "SELECT summary_json FROM summaries WHERE video_id = ? AND detail = ?",
            (video_id, detail),
        ).fetchone()
        return str(row["summary_json"]) if row else None

    def link_message(self, message_id: int, video_id: str, kind: str) -> None:
        """Associe un message Telegram à une vidéo. `kind` distingue le type de message
        (« summary », « question », « answer »…) pour aider au diagnostic."""
        self._conn.execute(
            "INSERT INTO telegram_messages (message_id, video_id, kind, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET video_id = excluded.video_id, "
            "kind = excluded.kind, created_at = excluded.created_at",
            (message_id, video_id, kind, _now()),
        )

    def video_for_message(self, message_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT video_id FROM telegram_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return str(row["video_id"]) if row else None

    def record_qa(self, video_id: str, question: str, answer: str) -> None:
        self._conn.execute(
            "INSERT INTO qa (video_id, question, answer, created_at) VALUES (?, ?, ?, ?)",
            (video_id, question, answer, _now()),
        )

    def recent_qa(self, video_id: str, limit: int = 6) -> list[tuple[str, str]]:
        """Retourne les `limit` derniers échanges Q&A pour cette vidéo, dans l'ordre
        chronologique (le plus ancien d'abord) — prêts à être réinjectés en contexte."""
        rows = self._conn.execute(
            "SELECT question, answer FROM qa WHERE video_id = ? ORDER BY id DESC LIMIT ?",
            (video_id, limit),
        ).fetchall()
        return [(str(r["question"]), str(r["answer"])) for r in reversed(rows)]

    def all_qa(self, video_id: str) -> list[tuple[int, str, str, str]]:
        """Tous les Q&A d'une vidéo dans l'ordre chronologique. Retourne (id, q, a, at)."""
        rows = self._conn.execute(
            "SELECT id, question, answer, created_at FROM qa WHERE video_id = ? ORDER BY id",
            (video_id,),
        ).fetchall()
        return [
            (int(r["id"]), str(r["question"]), str(r["answer"]), str(r["created_at"])) for r in rows
        ]

    # --- export Obsidian (Lot 6) -----------------------------------------------------------

    def upsert_obsidian_note(
        self, video_id: str, path: str, status: str = "inbox", theme: str = ""
    ) -> None:
        self._conn.execute(
            "INSERT INTO obsidian_notes (video_id, path, status, theme, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET path = excluded.path, "
            "status = excluded.status, theme = excluded.theme, "
            "updated_at = excluded.updated_at",
            (video_id, path, status, theme, _now()),
        )

    def obsidian_note(self, video_id: str) -> tuple[str, str, str] | None:
        row = self._conn.execute(
            "SELECT path, status, theme FROM obsidian_notes WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["path"]), str(row["status"]), str(row["theme"])

    def pending_exports(self, limit: int = 1000) -> list[VideoRecord]:
        """Vidéos réellement envoyées mais sans note Obsidian associée."""
        rows = self._conn.execute(
            "SELECT v.* FROM videos v LEFT JOIN obsidian_notes n ON v.video_id = n.video_id "
            "WHERE v.status = ? AND v.sent_at IS NOT NULL AND n.video_id IS NULL "
            "ORDER BY v.sent_at ASC LIMIT ?",
            (Status.SENT.value, limit),
        ).fetchall()
        return [self._record(r) for r in rows]

    # --- applicabilité (Lot 6) -------------------------------------------------------------

    def upsert_applicability(
        self,
        video_id: str,
        project_slug: str,
        score: int,
        idea: str,
        integration: str,
        effort: str,
        risks: str,
        prompt: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO applicability (video_id, project_slug, score, idea, integration, "
            "effort, risks, prompt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id, project_slug) DO UPDATE SET score = excluded.score, "
            "idea = excluded.idea, integration = excluded.integration, "
            "effort = excluded.effort, risks = excluded.risks, prompt = excluded.prompt, "
            "created_at = excluded.created_at",
            (video_id, project_slug, score, idea, integration, effort, risks, prompt, _now()),
        )

    def applicability_for(self, video_id: str) -> list[tuple[str, int, str, str, str, str, str]]:
        rows = self._conn.execute(
            "SELECT project_slug, score, idea, integration, effort, risks, prompt "
            "FROM applicability WHERE video_id = ? ORDER BY score DESC, project_slug",
            (video_id,),
        ).fetchall()
        return [
            (
                str(r["project_slug"]),
                int(r["score"]),
                str(r["idea"]),
                str(r["integration"]),
                str(r["effort"]),
                str(r["risks"]),
                str(r["prompt"]),
            )
            for r in rows
        ]

    def clear_applicability(self, video_id: str) -> None:
        self._conn.execute("DELETE FROM applicability WHERE video_id = ?", (video_id,))
        self._conn.execute("DELETE FROM ideas_written WHERE video_id = ?", (video_id,))

    def mark_idea_written(self, video_id: str, project_slug: str) -> bool:
        """Retourne True si c'est la première fois qu'on écrit cette idée dans IDEES.md."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO ideas_written (video_id, project_slug, at) VALUES (?, ?, ?)",
            (video_id, project_slug, _now()),
        )
        return cur.rowcount == 1

    def pending_applicability(self, limit: int = 1000) -> list[VideoRecord]:
        """Vidéos réellement envoyées avec résumé mais sans score d'applicabilité."""
        rows = self._conn.execute(
            "SELECT v.* FROM videos v "
            "LEFT JOIN (SELECT DISTINCT video_id FROM applicability) a "
            "ON v.video_id = a.video_id "
            "WHERE v.status = ? AND v.sent_at IS NOT NULL AND v.summary IS NOT NULL "
            "AND a.video_id IS NULL ORDER BY v.sent_at ASC LIMIT ?",
            (Status.SENT.value, limit),
        ).fetchall()
        return [self._record(r) for r in rows]

    # --- livres (Lot 7) --------------------------------------------------------------------

    LIVRE_STATUSES = ("pending", "running", "paused", "done", "failed", "cancelled")

    def create_livre(
        self,
        channel_url: str,
        channel_id: str,
        channel_name: str,
        title: str,
        detail: str,
        filters_json: str,
        videos: Sequence[tuple[str, str, int | None, str | None]],
    ) -> int:
        """Crée un livre et pré-remplit ses vidéos. `videos` : (video_id, title,
        duration_s, published_at). Retourne l'id du livre."""
        now = _now()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO livres (channel_url, channel_id, channel_name, title, detail, "
                "filters_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (channel_url, channel_id, channel_name, title, detail, filters_json, now, now),
            )
            livre_id = int(cur.lastrowid or 0)
            for rank, (video_id, vtitle, duration, published) in enumerate(videos):
                conn.execute(
                    "INSERT OR IGNORE INTO livre_videos (livre_id, video_id, rank, title, "
                    "duration_s, published_at, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                    (livre_id, video_id, rank, vtitle, duration, published, now),
                )
        return livre_id

    def get_livre(self, livre_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM livres WHERE id = ?", (livre_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def list_livres(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM livres ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def has_running_livre(self, exclude_id: int | None = None) -> bool:
        """Un livre est en cours si status ∈ (running, paused) : `paused` compte
        aussi — on veut interdire d'en lancer un second pendant qu'un autre attend
        sa reprise (usage_limit backoff)."""
        sql = "SELECT 1 FROM livres WHERE status IN ('running', 'paused')"
        params: list[Any] = []
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        return self._conn.execute(sql, params).fetchone() is not None

    def set_livre_status(
        self,
        livre_id: int,
        status: str,
        *,
        last_error: str | None = None,
        resume_after: datetime | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        if status not in self.LIVRE_STATUSES:
            raise ValueError(f"statut de livre inconnu : {status!r}")
        now = _now()
        sets = ["status = ?", "updated_at = ?", "last_error = ?"]
        params: list[Any] = [status, now, last_error]
        if resume_after is not None:
            sets.append("resume_after = ?")
            params.append(_iso(resume_after))
        else:
            sets.append("resume_after = NULL")
        if started:
            sets.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if finished:
            sets.append("finished_at = ?")
            params.append(now)
        params.append(livre_id)
        self._conn.execute(f"UPDATE livres SET {', '.join(sets)} WHERE id = ?", params)

    def set_livre_plan(self, livre_id: int, plan_json: str) -> None:
        self._conn.execute(
            "UPDATE livres SET plan_json = ?, updated_at = ? WHERE id = ?",
            (plan_json, _now(), livre_id),
        )

    def set_livre_output(self, livre_id: int, book_md: str, output_dir: str) -> None:
        self._conn.execute(
            "UPDATE livres SET book_md = ?, output_dir = ?, updated_at = ? WHERE id = ?",
            (book_md, output_dir, _now(), livre_id),
        )

    def livre_videos(self, livre_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM livre_videos WHERE livre_id = ? ORDER BY rank",
            (livre_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def livre_progress(self, livre_id: int) -> dict[str, int]:
        """Retourne {statut: nb_de_vidéos} pour l'affichage /status."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM livre_videos WHERE livre_id = ? GROUP BY status",
            (livre_id,),
        ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def next_queued_livre_video(self, livre_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM livre_videos WHERE livre_id = ? AND status = 'queued' "
            "ORDER BY rank LIMIT 1",
            (livre_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def set_livre_video_status(
        self,
        livre_id: int,
        video_id: str,
        status: str,
        last_error: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE livre_videos SET status = ?, last_error = ?, updated_at = ? "
            "WHERE livre_id = ? AND video_id = ?",
            (status, last_error, _now(), livre_id, video_id),
        )

    def _update(
        self,
        video_id: str,
        status: Status,
        transcript: str | None = None,
        summary: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE videos SET status = ?, transcript = COALESCE(?, transcript), "
            "summary = COALESCE(?, summary), last_error = NULL, updated_at = ? "
            "WHERE video_id = ? AND status NOT IN (?, ?)",
            (
                status.value,
                transcript,
                summary,
                _now(),
                video_id,
                Status.SENT.value,
                Status.SENDING.value,
            ),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> VideoRecord:
        return VideoRecord(
            video_id=row["video_id"],
            playlist_id=row["playlist_id"],
            title=row["title"],
            channel=row["channel"],
            url=row["url"],
            published_at=row["published_at"],
            status=Status(row["status"]),
            retries=int(row["retries"]),
            transcript=row["transcript"],
            summary=row["summary"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sent_at=row["sent_at"],
            send_attempt_at=row["send_attempt_at"],
            archived_at=row["archived_at"],
        )
