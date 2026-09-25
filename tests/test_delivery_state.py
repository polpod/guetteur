"""Machine à états des envois : reprise des « sending » périmés, retry, reset, migration Lot 1,
table deliveries, classification retryable / non retryable des erreurs des canaux."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from guetteur.config import ConfigError, Secrets, parse_config
from guetteur.models import Video
from guetteur.notify.base import Message, NotifyError, is_retryable_status
from guetteur.notify.telegram import TelegramNotifier
from guetteur.notify.whatsapp_cloud import WhatsAppCloudNotifier
from guetteur.store import SCHEMA_VERSION, Status, Store


def _video(vid: str) -> Video:
    return Video(vid, f"Titre {vid}", "Chaîne", datetime(2024, 1, 1, tzinfo=UTC), f"u/{vid}")


def _summarized(store: Store, vid: str) -> None:
    store.add_new(_video(vid), "PL")
    store.set_transcript(vid, '{"language": "fr", "source": "youtube", "segments": []}')
    store.set_summary(vid, '{"title": "T"}')


# --- reprise des « sending » périmés -----------------------------------------------------


def test_stale_sending_is_recovered_with_summary_kept(store: Store) -> None:
    _summarized(store, "v1")
    assert store.claim_for_sending("v1")
    later = datetime.now(UTC) + timedelta(minutes=11)

    assert store.recover_stale_sending(timedelta(minutes=10), now=later) == ["v1"]
    rec = store.get("v1")
    assert rec is not None
    assert rec.status is Status.SUMMARIZED
    assert rec.summary == '{"title": "T"}'  # aucun rappel Claude nécessaire
    assert rec.send_attempt_at is None and rec.sent_at is None
    assert store.claim_for_sending("v1")  # de nouveau envoyable


def test_recent_sending_is_left_alone(store: Store) -> None:
    _summarized(store, "v1")
    store.claim_for_sending("v1")
    soon = datetime.now(UTC) + timedelta(minutes=9)
    assert store.recover_stale_sending(timedelta(minutes=10), now=soon) == []
    rec = store.get("v1")
    assert rec is not None and rec.status is Status.SENDING


def test_sent_and_other_statuses_are_not_recovered(store: Store) -> None:
    _summarized(store, "sent")
    store.claim_for_sending("sent")
    store.mark_sent("sent")
    _summarized(store, "waiting")
    later = datetime.now(UTC) + timedelta(hours=2)
    assert store.recover_stale_sending(timedelta(minutes=10), now=later) == []


# --- retry / reset / backfill --force ---------------------------------------------------


def test_retry_failed_resumes_from_summary_with_zero_retries(store: Store) -> None:
    _summarized(store, "v1")
    store.claim_for_sending("v1")
    store.release_claim("v1", "boom")
    store.mark_failed("v1", "Telegram HTTP 403 : Forbidden")
    store.add_new(_video("v2"), "PL")
    store.mark_failed("v2", "pas de transcription")

    assert store.retry_failed("v1") == ["v1"]
    rec = store.get("v1")
    assert rec is not None
    assert (rec.status, rec.retries, rec.last_error) == (Status.SUMMARIZED, 0, None)

    assert store.retry_failed() == ["v2"]  # --all : les « failed » restantes
    rec2 = store.get("v2")
    assert rec2 is not None and rec2.status is Status.NEW  # rien d'acquis : tout refaire


def test_retry_ignores_non_failed(store: Store) -> None:
    _summarized(store, "v1")
    assert store.retry_failed("v1") == []
    assert store.retry_failed() == []


def test_reset_clears_everything(store: Store) -> None:
    _summarized(store, "v1")
    store.claim_for_sending("v1")
    store.mark_sent("v1")

    before = store.reset("v1")
    assert before is not None and before.really_sent
    rec = store.get("v1")
    assert rec is not None
    assert rec.status is Status.NEW
    assert (rec.transcript, rec.summary, rec.sent_at, rec.retries) == (None, None, None, 0)
    assert store.reset("inconnue") is None


def test_backfill_requeue_failed_only_with_force(store: Store) -> None:
    _summarized(store, "v1")
    store.mark_failed("v1", "HTTP 400 : chat not found")
    assert store.requeue_for_backfill(_video("v1"), "PL") is False
    assert store.requeue_for_backfill(_video("v1"), "PL", force=True) is True
    rec = store.get("v1")
    assert rec is not None and rec.status is Status.SUMMARIZED  # le résumé est réutilisé


def test_deliveries_are_recorded(store: Store) -> None:
    _summarized(store, "v1")
    store.add_delivery("v1", "whatsapp", 1, ok=False, error="HTTP 500")
    store.add_delivery("v1", "telegram", 1, ok=True, provider_message_id="42", is_fallback=True)
    rows = store.deliveries("v1")
    assert [(d.channel, d.attempt, d.ok, d.is_fallback) for d in rows] == [
        ("whatsapp", 1, False, False),
        ("telegram", 1, True, True),
    ]
    assert rows[1].provider_message_id == "42" and rows[0].error == "HTTP 500"


# --- migration d'une base du Lot 1 ------------------------------------------------------

LOT1_DDL = """
CREATE TABLE videos (
    video_id TEXT PRIMARY KEY, playlist_id TEXT NOT NULL, title TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', published_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('new','transcribed','summarized','sent','failed',
    'retry')), retries INTEGER NOT NULL DEFAULT 0, transcript TEXT, summary TEXT,
    last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT
);
CREATE INDEX idx_videos_status ON videos(status, created_at);
CREATE TABLE playlists (playlist_id TEXT PRIMARY KEY, initialized_at TEXT NOT NULL);
INSERT INTO videos VALUES ('a','PL','A','c','u',NULL,'sent',0,NULL,'{}',NULL,'t','t','t');
INSERT INTO videos VALUES ('b','PL','B','c','u',NULL,'failed',4,NULL,'{}','HTTP 400','t','t',NULL);
INSERT INTO playlists VALUES ('PL', 't');
"""


def test_lot1_database_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "guetteur.db"
    conn = sqlite3.connect(db)
    conn.executescript(LOT1_DDL)
    conn.close()

    store = Store(db)
    a, b = store.get("a"), store.get("b")
    assert a is not None and a.really_sent
    assert b is not None and b.status is Status.FAILED and b.last_error == "HTTP 400"
    assert store.is_playlist_initialized("PL")
    # Le nouveau statut est accepté par la contrainte CHECK reconstruite.
    assert store.retry_failed("b") == ["b"] and store.claim_for_sending("b")
    assert store.deliveries() == []
    store.close()

    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    raw.close()
    Store(db).close()  # rouvrir une base déjà migrée ne fait rien


# --- classification des erreurs ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (500, True),
        (502, True),
        (503, True),
        (429, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
    ],
)
def test_is_retryable_status(status: int, retryable: bool) -> None:
    assert is_retryable_status(status) is retryable


def _telegram(handler: httpx.MockTransport) -> TelegramNotifier:
    return TelegramNotifier("T", "42", httpx.Client(transport=handler))


@pytest.mark.parametrize(
    ("status", "description", "retryable"),
    [
        (400, "Bad Request: chat not found", False),
        (401, "Unauthorized", False),
        (403, "Forbidden: bot was blocked by the user", False),
        (404, "Not Found", False),
        (429, "Too Many Requests: retry after 5", True),
        (500, "Internal Server Error", True),
        (502, "Bad Gateway", True),
    ],
)
def test_telegram_error_classification(status: int, description: str, retryable: bool) -> None:
    body: dict[str, object] = {"ok": False, "description": description}
    if status == 429:
        body["parameters"] = {"retry_after": 5}
    transport = httpx.MockTransport(lambda _r: httpx.Response(status, json=body))
    with pytest.raises(NotifyError) as exc_info:
        _telegram(transport).send(Message("x", "x"))
    err = exc_info.value
    assert err.retryable is retryable
    assert err.status_code == status
    assert description in str(err)  # raison lisible
    if status == 429:
        assert err.retry_after == 5.0


def test_telegram_timeout_and_network_errors_are_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lent", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refusé", request=request)

    for handler in (timeout, refused):
        with pytest.raises(NotifyError) as exc_info:
            _telegram(httpx.MockTransport(handler)).send(Message("x", "x"))
        assert exc_info.value.retryable is True


def test_telegram_returns_provider_message_id() -> None:
    ok = httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})
    assert _telegram(httpx.MockTransport(lambda _r: ok)).send(Message("x", "x")) == "777"


@pytest.mark.parametrize(("status", "retryable"), [(500, True), (401, False), (400, False)])
def test_whatsapp_error_classification(status: int, retryable: bool) -> None:
    body = {"error": {"code": 190 if status == 401 else 1, "message": "erreur"}}
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(status, json=body))
    )
    with pytest.raises(NotifyError) as exc_info:
        WhatsAppCloudNotifier("T", "P", "336", client=client).send(Message("x", "x"))
    assert exc_info.value.retryable is retryable


def test_whatsapp_returns_provider_message_id() -> None:
    ok = httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: ok))
    assert WhatsAppCloudNotifier("T", "P", "336", client=client).send(Message("x", "x")) == (
        "wamid.ABC"
    )


def test_missing_tokens_error_is_not_retryable() -> None:
    with pytest.raises(NotifyError) as exc_info:
        TelegramNotifier("", "")
    assert exc_info.value.retryable is False


# --- configuration [notify] -------------------------------------------------------------


def test_notify_section_defaults_and_values() -> None:
    default = parse_config({}, Secrets()).notify
    assert default.fallback is None and default.max_attempts == 3
    assert default.retry_delays_s == (2.0, 8.0, 30.0) and default.sending_timeout_min == 10
    cfg = parse_config(
        {"notify": {"fallback": "telegram", "max_attempts": 4, "retry_delays_s": [1, 2]}},
        Secrets(),
    ).notify
    assert (cfg.fallback, cfg.max_attempts, cfg.retry_delays_s) == ("telegram", 4, (1.0, 2.0))
    assert parse_config({"notify": {"fallback": ""}}, Secrets()).notify.fallback is None


@pytest.mark.parametrize(
    "section",
    [{"fallback": "sms"}, {"max_attempts": 0}, {"retry_delays_s": []}, {"retry_delays_s": [-1]}],
)
def test_invalid_notify_section(section: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_config({"notify": section}, Secrets())
