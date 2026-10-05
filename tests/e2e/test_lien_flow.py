"""E2E Lot 8 : un tweet partagé via le faux Telegram crée un item LIEN,
produit un résumé, envoie un message sur Telegram, écrit une note Obsidian."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from unittest.mock import patch

from guetteur.config import Config, ObsidianConfig, PlaylistConfig
from guetteur.export.obsidian import ObsidianExporter
from guetteur.items import LinkContent
from guetteur.notify.base import Message, Notifier
from guetteur.pipeline_link import LinkPipeline
from guetteur.sources.liens.dispatch import ReaderBundle
from guetteur.store import Store


class _Notifier(Notifier):
    name = "test"

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> str | None:
        self.sent.append(message)
        return None


class _TweetReader:
    def read(self, url: str) -> LinkContent:
        return LinkContent(
            url=url,
            kind="tweet",
            title="Tweet de @foo",
            author="@foo",
            published_at=None,
            text="Hello world via fxtwitter",
            extras={},
        )


class _Summarizer:
    def raw_call(self, system_prompt: str, user_prompt: str, json_schema, timeout_s):  # type: ignore[no-untyped-def]
        return json.dumps(
            {
                "title": "Hello world",
                "tldr": "Un tweet qui dit bonjour.",
                "key_points": ["Bonjour", "Au monde"],
                "why_it_matters": "Parce que.",
            }
        )


def _make_config(tmp_path: Path) -> Config:
    obsidian = ObsidianConfig(
        enabled=True,
        path=tmp_path / "vault",
        git_sync=False,
        git_remote="",
    )
    return Config(
        playlists=(PlaylistConfig(id="P", label="P"),),
        data_dir=tmp_path,
        obsidian=obsidian,
    )


def test_tweet_url_flows_to_telegram_and_obsidian(tmp_path: Path) -> None:
    def gai(*args, **kwargs):  # type: ignore[no-untyped-def]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

    cfg = _make_config(tmp_path)
    store = Store(":memory:")
    try:
        notifier = _Notifier()
        exporter = ObsidianExporter(cfg, store)

        def _write(item, summary):  # type: ignore[no-untyped-def]
            from guetteur.export.liens import write_link_note

            write_link_note(exporter, item, summary)

        bundle = ReaderBundle(
            readers={
                "article": _TweetReader(),
                "tweet": _TweetReader(),
                "github": _TweetReader(),
                "youtube_oneshot": _TweetReader(),
            }
        )
        pipeline = LinkPipeline(
            config=cfg,
            store=store,
            backend=_Summarizer(),
            notifier_factory=lambda ch: notifier,
            readers=bundle,
            exporter=_write,
            claude_lock=threading.Lock(),
            timeout_s=10,
        )
        url = "https://x.com/foo/status/1234567890"
        item_id, inserted = store.add_link(url, "tweet", "telegram")
        assert inserted

        with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=gai):
            outcome = pipeline.process(item_id)

        assert outcome == "sent"
        assert len(notifier.sent) == 1
        assert "Hello world" in notifier.sent[0].plain
        # Obsidian : la note est dans Veille/Inbox et porte le frontmatter du lien.
        inbox = tmp_path / "vault" / "Veille" / "Inbox"
        notes = list(inbox.glob("*.md"))
        assert notes, f"aucune note créée dans {inbox}"
        content = notes[0].read_text(encoding="utf-8")
        assert "kind: tweet" in content
        assert "https://x.com/foo/status/1234567890" in content
        assert "source: telegram" in content
        assert "Hello world" in content

        item = store.get_item(item_id)
        assert item is not None and item.really_sent
    finally:
        store.close()
