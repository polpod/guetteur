from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from guetteur.config import ConfigError, Secrets, load_config, parse_config
from guetteur.summarize import build_summarizer
from guetteur.summarize.claude_api import ClaudeApiSummarizer
from guetteur.summarize.claude_code import ClaudeCodeSummarizer

ROOT = Path(__file__).resolve().parent.parent


def test_defaults() -> None:
    cfg = parse_config({"playlists": [{"id": "PL1"}]}, Secrets())
    assert cfg.poll_interval_seconds == 300
    assert cfg.claude_model == "claude-sonnet-4-6"
    assert cfg.max_videos_per_cycle == 3
    p = cfg.playlists[0]
    assert (p.label, p.language, p.notify, p.private) == ("PL1", "fr", "telegram", False)
    assert cfg.db_path == Path("data/guetteur.db")
    assert cfg.token_path == Path("data/token.json")
    assert cfg.summarize.provider == "claude_code"
    assert cfg.summarize.claude_code_bin == "claude"
    assert cfg.summarize.timeout_s == 180


def test_summarize_section() -> None:
    cfg = parse_config(
        {"summarize": {"provider": "claude_api", "claude_code_bin": "/x/claude", "timeout_s": 60}},
        Secrets(),
    )
    assert cfg.summarize.provider == "claude_api"
    assert cfg.summarize.claude_code_bin == "/x/claude"
    assert cfg.summarize.timeout_s == 60.0


@pytest.mark.parametrize(
    "section",
    [{"provider": "openai"}, {"timeout_s": 0}, {"timeout_s": "vite"}, {"claude_code_bin": " "}],
)
def test_invalid_summarize_section(section: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_config({"summarize": section}, Secrets())


def test_claude_code_backend_needs_no_api_key() -> None:
    cfg = parse_config({}, Secrets())
    assert isinstance(build_summarizer(cfg), ClaudeCodeSummarizer)


def test_claude_api_without_key_fails_clearly() -> None:
    cfg = parse_config({"summarize": {"provider": "claude_api"}}, Secrets())
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_summarizer(cfg)


def test_claude_api_with_key() -> None:
    cfg = parse_config({"summarize": {"provider": "claude_api"}}, Secrets(anthropic_api_key="k"))
    assert isinstance(build_summarizer(cfg), ClaudeApiSummarizer)


def test_invalid_channel() -> None:
    with pytest.raises(ConfigError):
        parse_config({"playlists": [{"id": "PL1", "notify": "sms"}]}, Secrets())


def test_invalid_interval() -> None:
    with pytest.raises(ConfigError):
        parse_config({"general": {"poll_interval_seconds": 0}}, Secrets())


def test_duplicate_playlists() -> None:
    with pytest.raises(ConfigError):
        parse_config({"playlists": [{"id": "A"}, {"id": "A"}]}, Secrets())


def test_shipped_config_is_valid() -> None:
    data = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    cfg = parse_config(data, Secrets())
    assert cfg.playlists
    assert {p.notify for p in cfg.playlists} <= {"telegram", "whatsapp"}


def test_playlist_keys_without_header_are_rejected() -> None:
    with pytest.raises(ConfigError, match=r"\[\[playlists\]\]"):
        parse_config({"id": "PL1", "label": "x"}, Secrets())


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.toml")
