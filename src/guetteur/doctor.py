"""`guetteur doctor` : vérifie l'environnement et affiche un tableau OK/KO."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from guetteur.config import Config
from guetteur.store import Store
from guetteur.summarize.base import SummarizeError
from guetteur.summarize.claude_code import ClaudeCodeSummarizer

Which = Callable[[str], str | None]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _version(path: str) -> str:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else ""


def check_claude(
    config: Config, which: Which = shutil.which, summarizer: ClaudeCodeSummarizer | None = None
) -> list[Check]:
    binary = config.summarize.claude_code_bin
    path = which(binary)
    if path is None:
        return [
            Check("claude : binaire", False, f"« {binary} » introuvable dans le PATH"),
            Check("claude : session", False, "non testée (binaire absent)"),
        ]
    version = _version(path)
    checks = [Check("claude : binaire", True, f"{path} {version}".strip())]
    s = summarizer or ClaudeCodeSummarizer(
        model=config.claude_model, binary=path, timeout_s=min(config.summarize.timeout_s, 90)
    )
    try:
        answer = s.ping()
        checks.append(Check("claude : session", True, f"ping → {answer[:40]!r}"))
    except SummarizeError as exc:
        checks.append(Check("claude : session", False, str(exc)))
    return checks


def check_ffmpeg(which: Which = shutil.which) -> Check:
    path = which("ffmpeg")
    if path is None:
        return Check("ffmpeg", False, "introuvable (requis pour le secours whisper)")
    return Check("ffmpeg", True, f"{path} {_version(path)}".strip()[:80])


def check_database(config: Config) -> Check:
    try:
        store = Store(config.db_path)
        try:
            counts = store.counts()
        finally:
            store.close()
    except (sqlite3.Error, OSError) as exc:
        return Check("base SQLite", False, f"{config.db_path} : {exc}")
    total = sum(counts.values())
    detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "vide"
    return Check("base SQLite", True, f"{config.db_path} ({total} vidéos : {detail})")


def _tokens(name: str, pairs: tuple[tuple[str, str], ...]) -> Check:
    missing = [key for key, value in pairs if not value]
    if missing:
        return Check(name, False, "manquant : " + ", ".join(missing))
    return Check(name, True, " et ".join(key for key, _ in pairs) + " présents")


def check_secrets(config: Config) -> list[Check]:
    s = config.secrets
    channels = {p.notify for p in config.playlists} or {"telegram"}
    checks: list[Check] = []
    if "telegram" in channels:
        checks.append(
            _tokens(
                "tokens Telegram",
                (
                    ("TELEGRAM_BOT_TOKEN", s.telegram_bot_token),
                    ("TELEGRAM_CHAT_ID", s.telegram_chat_id),
                ),
            )
        )
    if "whatsapp" in channels:
        checks.append(
            _tokens(
                "tokens WhatsApp",
                (("WA_TOKEN", s.wa_token), ("WA_PHONE_ID", s.wa_phone_id), ("WA_TO", s.wa_to)),
            )
        )
    if config.summarize.provider == "claude_api":
        checks.append(_tokens("ANTHROPIC_API_KEY", (("ANTHROPIC_API_KEY", s.anthropic_api_key),)))
    return checks


def run_checks(
    config: Config, which: Which = shutil.which, claude: ClaudeCodeSummarizer | None = None
) -> list[Check]:
    backend = f"{config.summarize.provider} ({config.claude_model})"
    checks = [Check("backend de résumé", True, backend)]
    if config.summarize.provider == "claude_code":
        checks += check_claude(config, which, claude)
    checks.append(check_ffmpeg(which))
    checks.append(check_database(config))
    checks += check_secrets(config)
    return checks


def render_table(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{'Vérification'.ljust(width)}  État  Détail", f"{'-' * width}  ----  ------"]
    for c in checks:
        lines.append(f"{c.name.ljust(width)}  {'OK ' if c.ok else 'KO '}   {c.detail}")
    return "\n".join(lines)
