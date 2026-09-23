"""Persistance SQLite (data/guetteur.db).

Idempotence : l'envoi passe par `claim_for_sending`, une transition atomique
summarized -> sent effectuée AVANT l'appel au canal. Une vidéo ne peut donc être réclamée
qu'une seule fois ; en cas d'échec d'envoi elle est explicitement relâchée
(`release_claim`). Un arrêt brutal pendant l'envoi laisse la vidéo « sent » : on préfère
perdre un message plutôt que l'envoyer deux fois."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from guetteur.models import Video


class Status(StrEnum):
    NEW = "new"
    TRANSCRIBED = "transcribed"
    SUMMARIZED = "summarized"
    SENT = "sent"
    FAILED = "failed"
    RETRY = "retry"


PENDING_STATUSES = (Status.NEW, Status.RETRY, Status.TRANSCRIBED, Status.SUMMARIZED)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS videos (
    video_id     TEXT PRIMARY KEY,
    playlist_id  TEXT NOT NULL,
    title        TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    status       TEXT NOT NULL CHECK (status IN ({", ".join(f"'{s.value}'" for s in Status)})),
    retries      INTEGER NOT NULL DEFAULT 0,
    transcript   TEXT,
    summary      TEXT,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    sent_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status, created_at);
CREATE TABLE IF NOT EXISTS playlists (
    playlist_id    TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
    sent_at: str | None

    def to_video(self) -> Video:
        published = datetime.fromisoformat(self.published_at) if self.published_at else None
        return Video(
            video_id=self.video_id,
            title=self.title,
            channel=self.channel,
            published=published,
            url=self.url,
        )


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

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

    def requeue_for_backfill(self, video: Video, playlist_id: str) -> bool:
        """Remet en file une vidéo pour un backfill explicite. Une vidéo réellement envoyée
        (sent_at non NULL) n'est jamais remise en file."""
        if self.add_new(video, playlist_id):
            return True
        cur = self._conn.execute(
            "UPDATE videos SET status = ?, retries = 0, last_error = NULL, updated_at = ? "
            "WHERE video_id = ? AND sent_at IS NULL AND status IN (?, ?)",
            (Status.NEW.value, _now(), video.video_id, Status.SENT.value, Status.FAILED.value),
        )
        return cur.rowcount == 1

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

    def set_transcript(self, video_id: str, transcript: str) -> None:
        self._update(video_id, status=Status.TRANSCRIBED, transcript=transcript, last_error=None)

    def set_summary(self, video_id: str, summary: str) -> None:
        self._update(video_id, status=Status.SUMMARIZED, summary=summary, last_error=None)

    def mark_retry(self, video_id: str, error: str) -> int:
        """Incrémente le compteur d'échecs et passe en « retry ». Retourne le nouveau compteur."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, retries = retries + 1, last_error = ?, "
                "updated_at = ? WHERE video_id = ? AND status != ?",
                (Status.RETRY.value, error, _now(), video_id, Status.SENT.value),
            )
            row = conn.execute(
                "SELECT retries FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return int(row["retries"]) if row else 0

    def mark_failed(self, video_id: str, error: str) -> bool:
        cur = self._conn.execute(
            "UPDATE videos SET status = ?, last_error = ?, updated_at = ? "
            "WHERE video_id = ? AND status NOT IN (?, ?)",
            (Status.FAILED.value, error, _now(), video_id, Status.SENT.value, Status.FAILED.value),
        )
        return cur.rowcount == 1

    def claim_for_sending(self, video_id: str) -> bool:
        """Transition atomique (résumé prêt) -> sent. False si déjà envoyée (ou pas prête)."""
        now = _now()
        cur = self._conn.execute(
            "UPDATE videos SET status = ?, sent_at = ?, updated_at = ? "
            "WHERE video_id = ? AND status IN (?, ?) AND sent_at IS NULL "
            "AND summary IS NOT NULL",
            (Status.SENT.value, now, now, video_id, Status.SUMMARIZED.value, Status.RETRY.value),
        )
        return cur.rowcount == 1

    def release_claim(self, video_id: str, error: str) -> int:
        """L'envoi a échoué : la vidéo repasse « summarized » avec un échec compté."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, sent_at = NULL, retries = retries + 1, "
                "last_error = ?, updated_at = ? WHERE video_id = ? AND status = ?",
                (Status.SUMMARIZED.value, error, _now(), video_id, Status.SENT.value),
            )
            row = conn.execute(
                "SELECT retries FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return int(row["retries"]) if row else 0

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")
        return {str(r["status"]): int(r["n"]) for r in rows}

    def _update(
        self,
        video_id: str,
        status: Status,
        transcript: str | None = None,
        summary: str | None = None,
        last_error: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE videos SET status = ?, transcript = COALESCE(?, transcript), "
            "summary = COALESCE(?, summary), last_error = ?, updated_at = ? "
            "WHERE video_id = ? AND status != ?",
            (status.value, transcript, summary, last_error, _now(), video_id, Status.SENT.value),
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
            sent_at=row["sent_at"],
        )
