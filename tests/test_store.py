from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from guetteur.models import Video
from guetteur.store import Status, Store


def _video(vid: str) -> Video:
    return Video(vid, f"Titre {vid}", "Chaîne", datetime(2024, 1, 1, tzinfo=UTC), f"u/{vid}")


def _ready(store: Store, vid: str) -> None:
    store.add_new(_video(vid), "PL")
    store.set_transcript(vid, "{}")
    store.set_summary(vid, "{}")


def test_add_new_is_idempotent(store: Store) -> None:
    assert store.add_new(_video("v1"), "PL") is True
    assert store.add_new(_video("v1"), "PL") is False
    assert store.counts() == {"new": 1}


def test_initialize_marks_existing_as_sent_without_sent_at(store: Store) -> None:
    n = store.initialize_playlist("PL", [_video("v1"), _video("v2")])
    assert n == 2
    assert store.is_playlist_initialized("PL")
    rec = store.get("v1")
    assert rec is not None
    assert rec.status is Status.SENT
    assert rec.sent_at is None
    assert store.pending(10) == []


def test_claim_for_sending_only_once(store: Store) -> None:
    _ready(store, "v1")
    assert store.claim_for_sending("v1") is True
    assert store.claim_for_sending("v1") is False
    rec = store.get("v1")
    assert rec is not None and rec.status is Status.SENDING and rec.send_attempt_at is not None
    assert rec.sent_at is None
    assert store.mark_sent("v1") is True
    assert store.claim_for_sending("v1") is False
    rec = store.get("v1")
    assert rec is not None and rec.status is Status.SENT and rec.sent_at is not None


def test_claim_requires_summary(store: Store) -> None:
    store.add_new(_video("v1"), "PL")
    assert store.claim_for_sending("v1") is False


def test_release_claim_allows_single_resend(store: Store) -> None:
    _ready(store, "v1")
    assert store.claim_for_sending("v1")
    assert store.release_claim("v1", "boom") == 1
    rec = store.get("v1")
    assert rec is not None and rec.status is Status.SUMMARIZED and rec.sent_at is None
    assert store.claim_for_sending("v1") is True
    assert store.claim_for_sending("v1") is False


def test_sent_video_cannot_be_downgraded(store: Store) -> None:
    _ready(store, "v1")
    store.claim_for_sending("v1")
    store.mark_sent("v1")
    store.set_summary("v1", "autre")
    store.mark_retry("v1", "err")
    assert store.mark_failed("v1", "err") is False
    rec = store.get("v1")
    assert rec is not None and rec.status is Status.SENT and rec.summary == "{}"


def test_add_new_does_not_resurrect_sent_video(store: Store) -> None:
    _ready(store, "v1")
    store.claim_for_sending("v1")
    store.mark_sent("v1")
    assert store.add_new(_video("v1"), "PL") is False
    assert store.pending(10) == []


def test_backfill_requeue_never_touches_really_sent(store: Store) -> None:
    store.initialize_playlist("PL", [_video("skipped")])
    _ready(store, "real")
    store.claim_for_sending("real")
    store.mark_sent("real")

    assert store.requeue_for_backfill(_video("skipped"), "PL") is True
    assert store.requeue_for_backfill(_video("real"), "PL") is False
    assert [r.video_id for r in store.pending(10)] == ["skipped"]


def test_mark_retry_counts_and_mark_failed_once(store: Store) -> None:
    store.add_new(_video("v1"), "PL")
    assert store.mark_retry("v1", "e1") == 1
    assert store.mark_retry("v1", "e2") == 2
    assert store.mark_failed("v1", "fin") is True
    assert store.mark_failed("v1", "fin") is False


def test_pending_respects_limit_and_playlists(store: Store) -> None:
    for i in range(5):
        store.add_new(_video(f"a{i}"), "A")
    store.add_new(_video("b0"), "B")
    assert len(store.pending(3)) == 3
    assert [r.video_id for r in store.pending(10, ["B"])] == ["b0"]


def test_persistence_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "guetteur.db"
    s1 = Store(db)
    _ready(s1, "v1")
    assert s1.claim_for_sending("v1")
    s1.close()
    s2 = Store(db)
    assert s2.claim_for_sending("v1") is False
    s2.close()
