"""Pipeline des liens : fetch → summarize → notify, mocké de bout en bout."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

from guetteur.config import load_config
from guetteur.items import LinkContent
from guetteur.notify.base import Message, Notifier
from guetteur.pipeline_link import LinkPipeline
from guetteur.sources.liens.dispatch import ReaderBundle
from guetteur.store import Store


class FakeBackend:
    """`raw_call` renvoie un JSON résumé prédéfini."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def raw_call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        timeout_s: float,
    ) -> str:
        self.calls.append((system_prompt[:40], user_prompt[:40]))
        return json.dumps(
            {
                "title": "Résumé du lien",
                "tldr": "Un lien intéressant qui parle de choses.",
                "key_points": ["Point A", "Point B", "Point C"],
                "why_it_matters": "Parce que.",
            }
        )


class FakeReader:
    def __init__(self, kind: str = "article") -> None:
        self._kind = kind

    def read(self, url: str) -> LinkContent:
        return LinkContent(
            url=url,
            kind=self._kind,  # type: ignore[arg-type]
            title="Titre",
            author="Auteur",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            text="Corps du lien",
            extras={},
        )


class FakeNotifier(Notifier):
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> str | None:
        self.sent.append(message)
        return None


def _config(tmp_path: Any) -> Any:
    conf = tmp_path / "config.toml"
    conf.write_text(
        """
[general]
source = "rss"
data_dir = "data"
[[playlists]]
id = "P"
label = "P"
""",
        encoding="utf-8",
    )
    cfg = load_config(conf)
    return _frozen_replace(cfg, data_dir=tmp_path)


def _frozen_replace(cfg: Any, **changes: Any) -> Any:
    from dataclasses import replace

    return replace(cfg, **changes)


def test_pipeline_processes_link_end_to_end(tmp_path: Any) -> None:
    cfg = _config(tmp_path)
    store = Store(":memory:")
    try:
        iid, _ = store.add_link("https://ex.com/a", "article", "telegram")
        backend = FakeBackend()
        notifier = FakeNotifier()
        bundle = ReaderBundle(
            readers={
                "article": FakeReader(),
                "tweet": FakeReader("tweet"),
                "github": FakeReader("github"),
                "youtube_oneshot": FakeReader("youtube_oneshot"),
            }
        )
        pipeline = LinkPipeline(
            config=cfg,
            store=store,
            backend=backend,
            notifier_factory=lambda ch: notifier,
            readers=bundle,
            claude_lock=threading.Lock(),
            timeout_s=10,
        )
        outcome = pipeline.process(iid)
        assert outcome == "sent"
        assert len(notifier.sent) == 1
        assert "Résumé du lien" in notifier.sent[0].plain
    finally:
        store.close()


def test_pipeline_retries_on_fetch_failure(tmp_path: Any) -> None:
    from guetteur.sources.liens.net import LinkFetchError

    cfg = _config(tmp_path)
    store = Store(":memory:")
    try:
        iid, _ = store.add_link("https://ex.com/b", "article", "telegram")

        class BoomReader:
            def read(self, url: str) -> LinkContent:
                raise LinkFetchError("boom")

        bundle = ReaderBundle(
            readers={
                "article": BoomReader(),
                "tweet": BoomReader(),
                "github": BoomReader(),
                "youtube_oneshot": BoomReader(),
            }
        )
        pipeline = LinkPipeline(
            config=cfg,
            store=store,
            backend=FakeBackend(),
            notifier_factory=lambda ch: FakeNotifier(),
            readers=bundle,
            claude_lock=threading.Lock(),
        )
        outcome = pipeline.process(iid)
        assert outcome in ("retry", "failed")
        item = store.get_item(iid)
        assert item is not None and (item.last_error or "").startswith("fetch")
    finally:
        store.close()


def test_pipeline_delegates_youtube(tmp_path: Any) -> None:
    cfg = _config(tmp_path)
    store = Store(":memory:")
    try:
        iid, _ = store.add_link("https://youtu.be/abc12345", "youtube_oneshot", "telegram")
        called: list[str] = []

        def delegate(item: Any) -> None:
            called.append(item.item_id)

        pipeline = LinkPipeline(
            config=cfg,
            store=store,
            backend=FakeBackend(),
            notifier_factory=lambda ch: FakeNotifier(),
            claude_lock=threading.Lock(),
            kind_delegate=delegate,
        )
        outcome = pipeline.process(iid)
        assert outcome == "delegated"
        assert called == [iid]
    finally:
        store.close()
