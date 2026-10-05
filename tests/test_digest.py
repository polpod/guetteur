"""Digest hebdomadaire : construction, rendu Markdown, parse_since."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from guetteur.jobs.digest import (
    build_digest,
    parse_since,
    render_digest_markdown,
    render_digest_telegram,
)
from guetteur.store import Store


def test_parse_since_relatives() -> None:
    ref = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    assert (ref - parse_since("7d", now=ref)) == timedelta(days=7)
    assert (ref - parse_since("24h", now=ref)) == timedelta(hours=24)
    assert (ref - parse_since("2w", now=ref)) == timedelta(weeks=2)


def test_parse_since_iso() -> None:
    d = parse_since("2026-01-01")
    assert d.year == 2026 and d.month == 1


def test_parse_since_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_since("yolo")


def test_digest_empty_renders_markdown(store: Store) -> None:
    since = datetime.now(UTC) - timedelta(days=7)
    digest = build_digest(store, since)
    md = render_digest_markdown(digest)
    assert "Digest" in md
    assert "Rien gardé" in md


def test_digest_with_sent_link(store: Store) -> None:
    iid, _ = store.add_link("https://ex.com/a", "article", "telegram")
    store.item_set_fetched(iid, title="Un article", author="J", published_at=None, content="x")
    store.item_set_summary(iid, '{"title": "Un article", "tldr": "..."}')
    store.item_set_theme(iid, "IA")
    store.item_claim_for_sending(iid)
    store.item_mark_sent(iid)

    digest = build_digest(store, since=datetime.now(UTC) - timedelta(hours=1))
    assert any(e.title == "Un article" for e in digest.entries)
    md = render_digest_markdown(digest)
    assert "IA" in md
    assert "Un article" in md

    message = render_digest_telegram(digest)
    assert "Un article" in message.plain
    assert "Un article" in message.markdown_v2
