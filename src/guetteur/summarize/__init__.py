"""Backends de résumé : « claude_code » (binaire Claude Code) ou « claude_api » (SDK)."""

from __future__ import annotations

from guetteur.config import Config, ConfigError
from guetteur.summarize.base import (
    SummarizeError,
    Summarizer,
    SummarizerUnavailableError,
    SummaryMeta,
)

__all__ = [
    "SummarizeError",
    "Summarizer",
    "SummarizerUnavailableError",
    "SummaryMeta",
    "build_summarizer",
]


def build_summarizer(config: Config) -> Summarizer:
    s = config.summarize
    if s.provider == "claude_code":
        from guetteur.summarize.claude_code import ClaudeCodeSummarizer

        return ClaudeCodeSummarizer(
            model=config.claude_model, binary=s.claude_code_bin, timeout_s=s.timeout_s
        )

    if not config.secrets.anthropic_api_key:
        raise ConfigError(
            'summarize.provider = "claude_api" mais ANTHROPIC_API_KEY est absente de '
            'l\'environnement (.env). Renseignez-la ou passez provider = "claude_code".'
        )
    import anthropic

    from guetteur.summarize.claude_api import ClaudeApiSummarizer

    client = anthropic.Anthropic(api_key=config.secrets.anthropic_api_key, timeout=s.timeout_s)
    return ClaudeApiSummarizer(client, config.claude_model)
