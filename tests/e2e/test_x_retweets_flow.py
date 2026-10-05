"""E2E Lot 8b : collecte retweets → item LIEN kind=tweet → pipeline lien
(mocké pour les lectures tweet) → message Telegram + note Obsidian."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from guetteur.config import (
    Config,
    ObsidianConfig,
    PlaylistConfig,
    XSourceConfig,
)
from guetteur.export.liens import write_link_note
from guetteur.export.obsidian import ObsidianExporter
from guetteur.items import LinkContent
from guetteur.notify.base import Message, Notifier
from guetteur.pipeline_link import LinkPipeline
from guetteur.sources.liens.dispatch import ReaderBundle
from guetteur.sources.x_retweets import collect
from guetteur.sources.x_subprocess import XSourceEnv
from guetteur.store import Store


class _Notifier(Notifier):
    name = "test"

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> str | None:
        self.sent.append(message)
        return None


class _FakeTweetReader:
    def read(self, url: str) -> LinkContent:
        return LinkContent(
            url=url,
            kind="tweet",
            title="Un tweet marquant",
            author="@foo",
            published_at=None,
            text="Un tweet qui mérite résumé.",
            extras={},
        )


class _FakeBackend:
    def raw_call(self, system_prompt, user_prompt, json_schema, timeout_s):  # type: ignore[no-untyped-def]
        return json.dumps(
            {
                "title": "Un tweet marquant",
                "tldr": "Résumé du tweet.",
                "key_points": ["Point 1", "Point 2"],
                "why_it_matters": "Parce que.",
            }
        )


def _config(tmp_path: Path) -> Config:
    return Config(
        playlists=(PlaylistConfig(id="P", label="P"),),
        data_dir=tmp_path,
        obsidian=ObsidianConfig(
            enabled=True,
            path=tmp_path / "vault",
            git_sync=False,
            git_remote="",
        ),
        x_source=XSourceConfig(
            enabled=True,
            account="me",
            watch_handle="mon_compte",
            poll_minutes=30,
            twitter_cli_bin="/bin/true",
            home=tmp_path / "x",
        ),
    )


def test_retweet_detected_summarized_notified(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    store = Store(":memory:")
    try:
        # 1er lancement : marque les items existants comme vus, pas de notif.
        payload_first = [
            {"id": "100", "retweetedStatus": {"id": "999", "screenName": "existant"}}
        ]
        xenv = XSourceEnv(
            binary="/bin/true", home=tmp_path / "x", venv_bin=None, auth_token="a", ct0="b"
        )

        def runner_first(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload_first), stderr="")

        collect(store, cfg.x_source, xenv, source="x_retweets", runner=runner_first)
        # L'item existant est en « sent » (sans sent_at) — pas de traitement.
        assert store.item_by_url("https://x.com/existant/status/999") is not None

        # 2e lancement : un nouveau retweet apparaît et est traité par le pipeline.
        payload_second = [
            {"id": "101", "retweetedStatus": {"id": "1234", "screenName": "jack"}},
        ]

        def runner_second(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(payload_second), stderr=""
            )

        stats = collect(store, cfg.x_source, xenv, source="x_retweets", runner=runner_second)
        assert stats.new_items == 1
        new_item = store.item_by_url("https://x.com/jack/status/1234")
        assert new_item is not None

        # Pipeline LIEN : fetch (mocké), résumé (mocké), notif.
        notifier = _Notifier()
        exporter = ObsidianExporter(cfg, store)

        def _write(item, summary):  # type: ignore[no-untyped-def]
            write_link_note(exporter, item, summary)

        bundle = ReaderBundle(
            readers={
                "article": _FakeTweetReader(),
                "tweet": _FakeTweetReader(),
                "github": _FakeTweetReader(),
                "youtube_oneshot": _FakeTweetReader(),
            }
        )
        pipeline = LinkPipeline(
            config=cfg,
            store=store,
            backend=_FakeBackend(),
            notifier_factory=lambda ch: notifier,
            readers=bundle,
            exporter=_write,
            claude_lock=threading.Lock(),
            timeout_s=10,
        )

        def gai(*args, **kwargs):  # type: ignore[no-untyped-def]
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

        with patch("guetteur.sources.liens.net.socket.getaddrinfo", side_effect=gai):
            outcome = pipeline.process(new_item.item_id)
        assert outcome == "sent"
        assert len(notifier.sent) == 1
        assert "Un tweet marquant" in notifier.sent[0].plain

        # Obsidian : note créée dans Inbox avec frontmatter source=x_retweets.
        inbox = tmp_path / "vault" / "Veille" / "Inbox"
        notes = list(inbox.glob("*.md"))
        assert notes
        content = notes[0].read_text(encoding="utf-8")
        assert "source: x_retweets" in content
        assert "kind: tweet" in content
    finally:
        store.close()


def test_main_cli_refuses_forbidden_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2 : si TWITTER_BROWSER est posé, `_build_xenv` refuse."""
    from guetteur.config import Config, XSourceConfig
    from guetteur.main import _build_xenv

    monkeypatch.setenv("TWITTER_BROWSER", "chrome")
    monkeypatch.setenv("TWITTER_AUTH_TOKEN", "a")
    monkeypatch.setenv("TWITTER_CT0", "b")
    cfg = Config(
        playlists=(PlaylistConfig(id="P", label="P"),),
        x_source=XSourceConfig(enabled=True, account="me", watch_handle="me"),
    )
    from guetteur.config import ConfigError

    with pytest.raises(ConfigError, match="interdites"):
        _build_xenv(cfg)


def test_main_cli_refuses_without_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    from guetteur.config import Config, ConfigError, XSourceConfig
    from guetteur.main import _build_xenv

    monkeypatch.delenv("TWITTER_BROWSER", raising=False)
    monkeypatch.delenv("TWITTER_CHROME_PROFILE", raising=False)
    monkeypatch.delenv("TWITTER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_CT0", raising=False)
    cfg = Config(
        playlists=(PlaylistConfig(id="P", label="P"),),
        x_source=XSourceConfig(enabled=True, account="me", watch_handle="me"),
    )
    with pytest.raises(ConfigError, match="TWITTER_AUTH_TOKEN"):
        _build_xenv(cfg)
