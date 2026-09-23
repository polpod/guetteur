"""Doublures de test partagées."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from xml.sax.saxutils import escape

from guetteur.config import Config, PlaylistConfig, Secrets, TranscriptConfig
from guetteur.models import Segment, Transcript
from guetteur.notify.base import Message, Notifier, NotifyError
from guetteur.transcript.base import NoTranscriptError

FIXTURES = Path(__file__).parent / "fixtures"


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    base: dict[str, Any] = {
        "playlists": (PlaylistConfig(id="PLtest123", label="Veille IA"),),
        "data_dir": tmp_path,
        "transcript": TranscriptConfig(max_retries=3),
        "secrets": Secrets(telegram_bot_token="TOKEN", telegram_chat_id="42"),
    }
    base.update(overrides)
    return Config(**base)


def feed_xml(entries: list[tuple[str, str, str]]) -> str:
    """Flux YouTube minimal : (video_id, titre, date ISO)."""
    items = "".join(
        f"""
 <entry>
  <id>yt:video:{vid}</id>
  <yt:videoId>{vid}</yt:videoId>
  <title>{escape(title)}</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v={vid}"/>
  <author><name>Chaîne Test</name></author>
  <published>{date}</published>
 </entry>"""
        for vid, title, date in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns="http://www.w3.org/2005/Atom"><title>Test</title>'
        f"{items}\n</feed>"
    )


def summary_payload(n_points: int = 6) -> dict[str, Any]:
    return {
        "title": "Titre résumé",
        "tldr": "Première phrase. Seconde phrase.",
        "key_points": [{"seconds": i * 60, "text": f"Point numéro {i}."} for i in range(n_points)],
        "why_it_matters": "Parce que c'est important.",
    }


def fake_anthropic(payloads: list[dict[str, Any]] | None = None) -> MagicMock:
    """Client anthropic factice : messages.create renvoie successivement les payloads JSON."""
    client = MagicMock()
    queue = list(payloads or [summary_payload()])

    def create(**kwargs: Any) -> SimpleNamespace:
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(data))],
        )

    client.messages.create.side_effect = create
    return client


class FakeTranscriber:
    def __init__(self, missing: set[str] | None = None) -> None:
        self.missing = missing or set()
        self.calls: list[str] = []

    def get(self, video_id: str) -> Transcript:
        self.calls.append(video_id)
        if video_id in self.missing:
            raise NoTranscriptError(f"pas de sous-titres pour {video_id}")
        return Transcript(
            video_id=video_id,
            language="fr",
            source="youtube",
            segments=(Segment(0.0, "Bonjour."), Segment(61.5, "Suite du propos.")),
        )


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[Message] = []
        self._fail_times = fail_times

    def send(self, message: Message) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise NotifyError("panne simulée")
        self.sent.append(message)


# --- doublures du binaire Claude Code --------------------------------------------------------


def cli_envelope(result: str, is_error: bool = False, **extra: Any) -> bytes:
    """Sortie de `claude -p ... --output-format json`."""
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": result,
        "num_turns": 1,
        **extra,
    }
    return json.dumps(payload).encode()


class FakeProcess:
    def __init__(
        self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, hang: bool = False
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._rc = returncode
        self._hang = hang
        self._done = False
        self.stdin: bytes | None = None
        self.killed = False

    @property
    def returncode(self) -> int | None:
        return self._rc if self._done else None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin = input
        if self._hang:
            await asyncio.sleep(3600)
        self._done = True
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self._done = True
        self._rc = -9

    async def wait(self) -> int:
        return self._rc


class FakeSpawn:
    """Remplace asyncio.create_subprocess_exec ; enregistre chaque appel."""

    def __init__(self, *processes: FakeProcess, default: FakeProcess | None = None) -> None:
        self._queue = list(processes)
        self._default = default
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *args: str, **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
        if self._queue:
            proc = self._queue.pop(0)
        elif self._default is not None:
            proc = FakeProcess(self._default._stdout, self._default._stderr, self._default._rc)
        else:
            raise AssertionError("appel inattendu au binaire claude")
        self.processes.append(proc)
        return proc


def claude_code_ok(payload: dict[str, Any] | None = None) -> FakeProcess:
    return FakeProcess(stdout=cli_envelope(json.dumps(payload or summary_payload())))
