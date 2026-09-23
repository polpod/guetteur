"""Monde simulé partagé par les tests e2e : flux RSS mocké (HTTP), transcription mockée,
backend de résumé mocké (SDK anthropic ou binaire claude), Telegram mocké (HTTP)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx

from guetteur.config import Config
from guetteur.notify.telegram import TelegramNotifier
from guetteur.pipeline import Pipeline
from guetteur.sources.rss import RssSource
from guetteur.store import Store
from guetteur.summarize.base import Summarizer
from guetteur.summarize.claude_api import ClaudeApiSummarizer
from guetteur.summarize.claude_code import ClaudeCodeSummarizer
from tests.helpers import FakeSpawn, FakeTranscriber, claude_code_ok, fake_anthropic, feed_xml

Backend = Literal["claude_api", "claude_code"]

OLD = [
    ("OLD00000001", "Ancienne 1", "2024-01-01T10:00:00+00:00"),
    ("OLD00000002", "Ancienne 2", "2024-01-02T10:00:00+00:00"),
]
NEW = ("NEW00000001", "Nouvelle vidéo : 100% utile !", "2024-02-01T10:00:00+00:00")
NOSUB = ("NOSUB000001", "Sans sous-titres", "2024-02-02T10:00:00+00:00")


class World:
    def __init__(
        self,
        config: Config,
        backend: Backend,
        spawn: FakeSpawn | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.feed: list[tuple[str, str, str]] = list(OLD)
        self.telegram: list[dict[str, Any]] = []
        self.store = Store(config.db_path)
        self.transcriber = FakeTranscriber(missing={NOSUB[0]})
        self.claude = fake_anthropic()
        self.spawn = spawn or FakeSpawn(default=claude_code_ok())

        def youtube(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "www.youtube.com"
            return httpx.Response(200, text=feed_xml(self.feed))

        rss = RssSource(httpx.Client(transport=httpx.MockTransport(youtube)))
        self.telegram_notifier = self.make_telegram(self._telegram_ok)

        summarizer: Summarizer
        if backend == "claude_api":
            summarizer = ClaudeApiSummarizer(self.claude, config.claude_model)
        else:
            summarizer = ClaudeCodeSummarizer(
                model=config.claude_model,
                binary=config.summarize.claude_code_bin,
                timeout_s=config.summarize.timeout_s,
                spawn=self.spawn,
            )
        self.pipeline = Pipeline(
            config=config,
            store=self.store,
            source_factory=lambda _p: rss,
            transcriber=self.transcriber,
            summarizer=summarizer,
            notifier_factory=lambda _c: self.telegram_notifier,
        )

    def _telegram_ok(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org"
        self.telegram.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    @staticmethod
    def make_telegram(handler: Callable[[httpx.Request], httpx.Response]) -> TelegramNotifier:
        return TelegramNotifier("TOKEN", "42", httpx.Client(transport=httpx.MockTransport(handler)))

    def replace_telegram(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.telegram_notifier = self.make_telegram(handler)
        self.pipeline._notifiers.clear()

    @property
    def llm_calls(self) -> int:
        """Nombre d'appels au modèle, quel que soit le backend."""
        if self.backend == "claude_api":
            return int(self.claude.messages.create.call_count)
        return len(self.spawn.calls)

    def last_prompt(self) -> str:
        if self.backend == "claude_api":
            return str(self.claude.messages.create.call_args.kwargs["messages"][0]["content"])
        args = self.spawn.calls[-1][0]
        return args[args.index("-p") + 1]


WorldFactory = Callable[..., World]


def world_factory(tmp_path: Path, backend: Backend) -> WorldFactory:
    from tests.helpers import make_config

    def make(config: Config | None = None, **overrides: Any) -> World:
        cfg = config or make_config(tmp_path, **overrides)
        return World(cfg, backend)

    return make
