"""Backend claude_code : le subprocess `claude` est entièrement simulé."""

from __future__ import annotations

import json

import pytest

from guetteur.models import Segment, Transcript, Video
from guetteur.summarize.base import (
    CHUNK_CHARS,
    SUMMARY_SCHEMA,
    SYSTEM_PROMPT,
    SummarizeError,
    SummarizerUnavailableError,
    SummaryMeta,
)
from guetteur.summarize.claude_code import (
    ClaudeCodeError,
    ClaudeCodeNotFoundError,
    ClaudeCodeNotLoggedInError,
    ClaudeCodeSummarizer,
    ClaudeCodeTimeoutError,
)
from tests.helpers import (
    FakeProcess,
    FakeSpawn,
    claude_code_ok,
    cli_envelope,
    summary_payload,
)

VIDEO = Video("VID123", "Une vidéo", "Une chaîne", None, "https://www.youtube.com/watch?v=VID123")
META = SummaryMeta(video=VIDEO, language="fr")
TRANSCRIPT = Transcript(
    "VID123", "en", "youtube", (Segment(0.0, "Hello."), Segment(61.0, "World."))
)


def _summarizer(spawn: FakeSpawn, **kwargs: object) -> ClaudeCodeSummarizer:
    return ClaudeCodeSummarizer(
        model="claude-sonnet-4-6",
        spawn=spawn,
        env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-secret", "HOME": "/home/g"},
        **kwargs,  # type: ignore[arg-type]
    )


def _flag(args: tuple[str, ...], name: str) -> str:
    return args[args.index(name) + 1]


# --- cas nominal -------------------------------------------------------------------------


def test_valid_output_is_parsed() -> None:
    spawn = FakeSpawn(claude_code_ok())
    summary = _summarizer(spawn).summarize(TRANSCRIPT, META)

    assert summary.title == "Titre résumé"
    assert len(summary.key_points) == 6
    assert summary.key_points[1].seconds == 60


def test_command_line_is_safe_and_complete() -> None:
    spawn = FakeSpawn(claude_code_ok())
    _summarizer(spawn, binary="/opt/claude").summarize(TRANSCRIPT, META)

    args, kwargs = spawn.calls[0]
    assert args[0] == "/opt/claude"
    assert "Langue du résumé : fr" in _flag(args, "-p")
    assert _flag(args, "--output-format") == "json"
    assert _flag(args, "--model") == "claude-sonnet-4-6"
    assert _flag(args, "--system-prompt") == SYSTEM_PROMPT
    assert json.loads(_flag(args, "--json-schema")) == SUMMARY_SCHEMA
    # Aucun outil : liste vide + mode plan ; pas de réglages/hooks/MCP utilisateur.
    assert _flag(args, "--tools") == ""
    assert _flag(args, "--permission-mode") == "plan"
    assert "--strict-mcp-config" in args
    assert _flag(args, "--setting-sources") == ""
    # La transcription passe par stdin, pas par la ligne de commande.
    assert "[61s] World." not in " ".join(args)
    stdin = spawn.processes[0].stdin
    assert stdin is not None and "[61s] World." in stdin.decode()
    # Pas de shell, et la session Claude Code est utilisée plutôt qu'une clé API.
    assert "shell" not in kwargs
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert kwargs["env"]["HOME"] == "/home/g"


def test_result_in_markdown_fence_is_accepted() -> None:
    fenced = "```json\n" + json.dumps(summary_payload()) + "\n```"
    spawn = FakeSpawn(FakeProcess(stdout=cli_envelope(fenced)))
    assert _summarizer(spawn).summarize(TRANSCRIPT, META).title == "Titre résumé"


def test_structured_output_fallback() -> None:
    out = cli_envelope("Voici le résumé.", structured_output=summary_payload())
    spawn = FakeSpawn(FakeProcess(stdout=out))
    assert _summarizer(spawn).summarize(TRANSCRIPT, META).title == "Titre résumé"


def test_long_transcript_is_chunked() -> None:
    segments = tuple(Segment(i * 10.0, "x" * 990) for i in range(int(CHUNK_CHARS * 2.5 / 1000)))
    long_transcript = Transcript("VID123", "en", "youtube", segments)
    final = summary_payload(7) | {"title": "Fusion"}
    spawn = FakeSpawn(claude_code_ok(), claude_code_ok(), claude_code_ok(), claude_code_ok(final))

    summary = _summarizer(spawn).summarize(long_transcript, META)

    assert len(spawn.calls) == 4
    assert "partie 1/3" in _flag(spawn.calls[0][0], "-p")
    last_stdin = spawn.processes[3].stdin
    assert last_stdin is not None and b"<resumes_partiels>" in last_stdin
    assert summary.title == "Fusion"


# --- erreurs -----------------------------------------------------------------------------


def test_truncated_output_is_retryable_error() -> None:
    truncated = cli_envelope(json.dumps(summary_payload()))[:80]
    spawn = FakeSpawn(FakeProcess(stdout=truncated))
    with pytest.raises(ClaudeCodeError, match="tronquée") as exc_info:
        _summarizer(spawn).summarize(TRANSCRIPT, META)
    assert not isinstance(exc_info.value, SummarizerUnavailableError)


def test_truncated_summary_json_in_result() -> None:
    spawn = FakeSpawn(FakeProcess(stdout=cli_envelope('{"title": "coupé", "tldr": "')))
    with pytest.raises(ClaudeCodeError, match="pas du JSON"):
        _summarizer(spawn).summarize(TRANSCRIPT, META)


def test_not_logged_in_exit_code_1() -> None:
    out = cli_envelope("Not logged in · Please run /login", is_error=True)
    spawn = FakeSpawn(FakeProcess(stdout=out, returncode=1))
    with pytest.raises(ClaudeCodeNotLoggedInError, match="claude auth login"):
        _summarizer(spawn).summarize(TRANSCRIPT, META)


def test_not_logged_in_on_stderr_without_json() -> None:
    spawn = FakeSpawn(FakeProcess(stderr=b"Error: Not logged in", returncode=1))
    with pytest.raises(SummarizerUnavailableError):
        _summarizer(spawn).summarize(TRANSCRIPT, META)


def test_successful_summary_mentioning_login_is_not_an_error() -> None:
    payload = summary_payload() | {"tldr": "Le bug « not logged in » est corrigé. Voilà."}
    spawn = FakeSpawn(claude_code_ok(payload))
    assert "not logged in" in _summarizer(spawn).summarize(TRANSCRIPT, META).tldr


def test_other_failure_is_retryable() -> None:
    out = cli_envelope("API Error: 529 Overloaded", is_error=True)
    spawn = FakeSpawn(FakeProcess(stdout=out, returncode=1))
    with pytest.raises(ClaudeCodeError, match="529") as exc_info:
        _summarizer(spawn).summarize(TRANSCRIPT, META)
    assert not isinstance(exc_info.value, SummarizerUnavailableError)


def test_timeout_kills_process() -> None:
    spawn = FakeSpawn(FakeProcess(hang=True))
    with pytest.raises(ClaudeCodeTimeoutError, match="pas répondu"):
        _summarizer(spawn, timeout_s=0.05).summarize(TRANSCRIPT, META)
    assert spawn.processes[0].killed
    assert isinstance(ClaudeCodeTimeoutError("x"), SummarizeError)


def test_missing_binary() -> None:
    async def missing(*_args: str, **_kwargs: object) -> FakeProcess:
        raise FileNotFoundError("claude")

    s = ClaudeCodeSummarizer(model="m", binary="claude-absent", spawn=missing)
    with pytest.raises(ClaudeCodeNotFoundError, match="claude-absent"):
        s.summarize(TRANSCRIPT, META)


def test_ping() -> None:
    spawn = FakeSpawn(FakeProcess(stdout=cli_envelope("pong")))
    assert _summarizer(spawn).ping() == "pong"
    args = spawn.calls[0][0]
    assert _flag(args, "-p") == "ping"
    assert _flag(args, "--output-format") == "json"
    assert "--json-schema" not in args
