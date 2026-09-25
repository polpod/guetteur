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

from guetteur.models import Video

SCHEMA_VERSION = 2


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
    send_attempt_at TEXT
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
"""
)

# Colonnes de la table videos du Lot 1 (avant send_attempt_at et le statut « sending »).
_V1_COLUMNS = (
    "video_id, playlist_id, title, channel, url, published_at, status, retries, transcript, "
    "summary, last_error, created_at, updated_at, sent_at"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


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

    @property
    def really_sent(self) -> bool:
        return self.status is Status.SENT and self.sent_at is not None

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
        self._conn = sqlite3.connect(str(path), isolation_level=None)
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
        reconstruit la table en conservant toutes les lignes."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
        ).fetchone()
        if row is None or "send_attempt_at" in str(row["sql"]):
            return
        with self._tx() as conn:
            conn.execute("ALTER TABLE videos RENAME TO videos_v1")
            conn.execute("DROP INDEX IF EXISTS idx_videos_status")
            conn.execute(_VIDEOS_DDL)
            conn.execute(f"INSERT INTO videos ({_V1_COLUMNS}) SELECT {_V1_COLUMNS} FROM videos_v1")
            conn.execute("DROP TABLE videos_v1")

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

    # --- meta (heartbeat, alertes) ---------------------------------------------------------

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
        )
