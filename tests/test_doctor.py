from __future__ import annotations

from pathlib import Path

from guetteur.config import Secrets, SummarizeConfig
from guetteur.doctor import Which, render_table, run_checks
from guetteur.summarize.claude_code import ClaudeCodeSummarizer
from tests.helpers import FakeProcess, FakeSpawn, cli_envelope, make_config


def _which(found: set[str]) -> Which:
    return lambda name: f"/usr/bin/{name}" if name in found else None


def _claude(stdout: bytes, rc: int = 0) -> ClaudeCodeSummarizer:
    return ClaudeCodeSummarizer(
        model="m", spawn=FakeSpawn(FakeProcess(stdout=stdout, returncode=rc))
    )


def test_all_ok(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    checks = run_checks(
        config,
        which=_which({"claude", "ffmpeg"}),
        claude=_claude(cli_envelope("pong")),
    )
    assert all(c.ok for c in checks), render_table(checks)
    names = [c.name for c in checks]
    assert names == [
        "backend de résumé",
        "claude : binaire",
        "claude : session",
        "ffmpeg",
        "base SQLite",
        "tokens Telegram",
    ]
    rows = render_table(checks).splitlines()[2:]
    assert len(rows) == len(checks)
    assert all("  OK " in row and "  KO " not in row for row in rows)


def test_not_logged_in_and_missing_tools(tmp_path: Path) -> None:
    config = make_config(tmp_path, secrets=Secrets())
    not_logged = cli_envelope("Not logged in · Please run /login", is_error=True)
    checks = {
        c.name: c
        for c in run_checks(
            config,
            which=_which({"claude"}),
            claude=_claude(not_logged, rc=1),
        )
    }
    assert checks["claude : binaire"].ok
    assert not checks["claude : session"].ok
    assert "claude auth login" in checks["claude : session"].detail
    assert not checks["ffmpeg"].ok
    assert not checks["tokens Telegram"].ok
    assert "TELEGRAM_BOT_TOKEN" in checks["tokens Telegram"].detail


def test_missing_claude_binary(tmp_path: Path) -> None:
    checks = {c.name: c for c in run_checks(make_config(tmp_path), which=_which(set()))}
    assert not checks["claude : binaire"].ok
    assert not checks["claude : session"].ok


def test_claude_api_provider_checks_key(tmp_path: Path) -> None:
    config = make_config(tmp_path, summarize=SummarizeConfig(provider="claude_api"))
    checks = {c.name: c for c in run_checks(config, which=_which({"ffmpeg"}))}
    assert "claude : binaire" not in checks
    assert not checks["ANTHROPIC_API_KEY"].ok
