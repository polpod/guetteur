"""Table items : add_link idempotent, machine à états, dédup par URL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from guetteur.items import ItemStatus
from guetteur.store import Store


def test_add_link_is_idempotent(store: Store) -> None:
    iid, inserted = store.add_link("https://x.com/foo/status/1", "tweet", "telegram")
    assert inserted
    iid2, inserted2 = store.add_link("https://x.com/foo/status/1", "tweet", "telegram")
    assert iid == iid2
    assert not inserted2
    assert store.get_item(iid) is not None


def test_mark_seen_skips_processing(store: Store) -> None:
    iid, _ = store.add_link("https://ex.com/a", "article", "x_retweets", mark_seen=True)
    item = store.get_item(iid)
    assert item is not None
    assert item.status is ItemStatus.SENT
    assert item.sent_at is None  # marqué vu, pas réellement envoyé
    assert not item.really_sent


def test_state_machine(store: Store) -> None:
    iid, _ = store.add_link("https://ex.com/x", "article", "telegram")
    store.item_set_fetched(
        iid,
        title="Hello",
        author="Jane",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content="body",
    )
    item = store.get_item(iid)
    assert item is not None and item.status is ItemStatus.FETCHED
    assert item.title == "Hello"

    store.item_set_summary(iid, '{"title": "Hello", "tldr": "..."}')
    item = store.get_item(iid)
    assert item is not None and item.status is ItemStatus.SUMMARIZED

    assert store.item_claim_for_sending(iid)
    # Deuxième claim refusé (déjà en SENDING)
    assert not store.item_claim_for_sending(iid)

    assert store.item_mark_sent(iid)
    item = store.get_item(iid)
    assert item is not None and item.really_sent


def test_retry_counts_and_fails(store: Store) -> None:
    iid, _ = store.add_link("https://ex.com/y", "article", "telegram")
    n1 = store.item_mark_retry(iid, "boom")
    n2 = store.item_mark_retry(iid, "boom again")
    assert n1 == 1
    assert n2 == 2
    assert store.item_mark_failed(iid, "give up")
    assert (store.get_item(iid) or ...).status is ItemStatus.FAILED  # type: ignore[union-attr]


def test_recover_stale_item_sending(store: Store) -> None:
    iid, _ = store.add_link("https://ex.com/z", "article", "telegram")
    store.item_set_fetched(
        iid, title="t", author="", published_at=None, content="body"
    )
    store.item_set_summary(iid, "{}")
    assert store.item_claim_for_sending(iid)
    # Simule un envoi bloqué très ancien.
    now = datetime.now(UTC) + timedelta(minutes=20)
    recovered = store.recover_stale_item_sending(timedelta(minutes=5), now=now)
    assert iid in recovered
    assert (store.get_item(iid) or ...).status is ItemStatus.SUMMARIZED  # type: ignore[union-attr]


def test_items_sent_since_window(store: Store) -> None:
    iid, _ = store.add_link("https://ex.com/win", "article", "telegram")
    store.item_set_fetched(iid, title="t", author="", published_at=None, content="b")
    store.item_set_summary(iid, "{}")
    store.item_claim_for_sending(iid)
    store.item_mark_sent(iid)
    recent = store.items_sent_since(datetime.now(UTC) - timedelta(hours=1))
    assert any(i.item_id == iid for i in recent)
    old = store.items_sent_since(datetime.now(UTC) + timedelta(hours=1))
    assert not old
