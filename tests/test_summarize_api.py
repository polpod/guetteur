from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from guetteur.models import Segment, Transcript, Video
from guetteur.summarize.base import (
    CHUNK_CHARS,
    MAX_KEY_POINTS,
    SUMMARY_SCHEMA,
    SYSTEM_PROMPT,
    SummarizeError,
    SummarizerUnavailableError,
    SummaryMeta,
    split_transcript,
    summary_from_json,
    summary_to_json,
)
from guetteur.summarize.claude_api import ClaudeApiSummarizer
from tests.helpers import fake_anthropic, summary_payload

VIDEO = Video("VID123", "Une vidéo", "Une chaîne", None, "https://www.youtube.com/watch?v=VID123")
META = SummaryMeta(video=VIDEO, language="fr")


def _transcript(n: int = 3, text: str = "phrase") -> Transcript:
    return Transcript("VID123", "en", "youtube", tuple(Segment(i * 10.0, text) for i in range(n)))


def test_system_prompt_is_french_and_complete() -> None:
    for expected in (
        "title",
        "tldr",
        "2 phrases",
        "entre 5 et 8 points clés",
        "why_it_matters",
        "1 phrase",
        "horodatage",
        "N'invente rien",
    ):
        assert expected in SYSTEM_PROMPT
    assert set(SUMMARY_SCHEMA["required"]) == {
        "title",
        "tldr",
        "key_points",
        "why_it_matters",
        "announced_items",
    }


def test_summarize_sends_expected_request() -> None:
    client = fake_anthropic()
    summary = ClaudeApiSummarizer(client, "claude-sonnet-4-6").summarize(_transcript(), META)

    kwargs: dict[str, Any] = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["system"] == SYSTEM_PROMPT
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    user = kwargs["messages"][0]["content"]
    assert "Langue du résumé : fr" in user
    assert "[10s] phrase" in user
    assert "Une vidéo" in user

    assert summary.title == "Titre résumé"
    assert len(summary.key_points) == 6
    assert summary.key_points[1].seconds == 60
    assert summary.reading_time_minutes >= 1


def test_key_points_capped_and_seconds_clamped() -> None:
    payload = summary_payload(12)
    payload["key_points"][0]["seconds"] = -5
    client = fake_anthropic([payload])
    summary = ClaudeApiSummarizer(client, "m").summarize(_transcript(), META)
    assert len(summary.key_points) == MAX_KEY_POINTS
    assert summary.key_points[0].seconds == 0


def test_long_transcript_is_chunked_then_merged() -> None:
    # ~2,5 fois la taille d'un chunk => 3 morceaux + 1 appel de fusion.
    line = "x" * 990
    transcript = _transcript(n=int(CHUNK_CHARS * 2.5 / 1000), text=line)
    partial = summary_payload(5)
    final = summary_payload(7) | {"title": "Fusion"}
    client = fake_anthropic([partial, partial, partial, final])

    summary = ClaudeApiSummarizer(client, "m").summarize(transcript, META)

    calls = client.messages.create.call_args_list
    assert len(calls) == 4
    for c in calls[:3]:
        assert len(c.kwargs["messages"][0]["content"]) < CHUNK_CHARS + 1_000
    assert "partie 1/3" in calls[0].kwargs["messages"][0]["content"]
    assert "<resumes_partiels>" in calls[3].kwargs["messages"][0]["content"]
    assert summary.title == "Fusion"


def test_split_transcript_keeps_lines_whole() -> None:
    text = "\n".join(f"[{i}s] " + "y" * 95 for i in range(5000))
    chunks = split_transcript(text, max_chars=50_000)
    assert all(len(c) <= 50_000 for c in chunks)
    assert "\n".join(chunks) == text


def test_truncated_response_raises() -> None:
    client = fake_anthropic()
    client.messages.create.side_effect = None
    client.messages.create.return_value = SimpleNamespace(
        stop_reason="max_tokens", content=[SimpleNamespace(type="text", text="{")]
    )
    with pytest.raises(SummarizeError):
        ClaudeApiSummarizer(client, "m").summarize(_transcript(), META)


def test_api_error_is_wrapped() -> None:
    client = fake_anthropic()
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client.messages.create.side_effect = anthropic.APIConnectionError(request=request)
    with pytest.raises(SummarizeError):
        ClaudeApiSummarizer(client, "m").summarize(_transcript(), META)


def test_auth_error_makes_backend_unavailable() -> None:
    client = fake_anthropic()
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(401, request=request)
    client.messages.create.side_effect = anthropic.AuthenticationError(
        "invalid x-api-key", response=response, body=None
    )
    with pytest.raises(SummarizerUnavailableError, match="ANTHROPIC_API_KEY"):
        ClaudeApiSummarizer(client, "m").summarize(_transcript(), META)


def test_summary_json_roundtrip() -> None:
    client = fake_anthropic()
    summary = ClaudeApiSummarizer(client, "m").summarize(_transcript(), META)
    raw = summary_to_json(summary)
    assert json.loads(raw)["title"] == "Titre résumé"
    assert summary_from_json(raw) == summary
