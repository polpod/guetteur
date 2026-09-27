from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from guetteur.config import ArchiveConfig, Secrets, SummarizeConfig
from guetteur.doctor import Which, check_notebooklm, render_table, run_checks
from guetteur.store import Store
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
        # Archivage désactivé par défaut : une seule ligne récapitulative.
        "notebooklm : archivage",
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


# --- doctor NotebookLM ------------------------------------------------------------------


def _enabled(tmp_path: Path, **overrides: Any) -> ArchiveConfig:
    home = tmp_path / "nlm"
    home.mkdir(mode=0o700)
    defaults: dict[str, Any] = {
        "enabled": True,
        "notebook_name": "Veille YouTube",
        "account": "guetteur.veille@gmail.com",
        "home": home,
        "max_sources_per_notebook": 45,
    }
    defaults.update(overrides)
    return ArchiveConfig(**defaults)


def test_notebooklm_disabled_shows_single_row(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    checks = {c.name: c for c in check_notebooklm(config)}
    assert "notebooklm : archivage" in checks
    assert checks["notebooklm : archivage"].ok
    assert "désactivé" in checks["notebooklm : archivage"].detail


def test_notebooklm_all_ok(tmp_path: Path) -> None:
    """L'archivage activé produit 4 lignes : version, permissions, session, compte."""
    config = make_config(tmp_path, archive=_enabled(tmp_path))
    store = Store(tmp_path / "guetteur.db")

    class _Archiver:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def auth_check(self) -> str | None:
            return "guetteur.veille@gmail.com"

    try:
        with (
            patch("importlib.metadata.version", return_value="0.8.3"),
            patch("guetteur.archive.notebooklm.NotebookLMArchiver", _Archiver),
        ):
            names = [c.name for c in check_notebooklm(config, store=store)]
            checks = {c.name: c for c in check_notebooklm(config, store=store)}
    finally:
        store.close()
    assert names == [
        "notebooklm : version",
        "notebooklm : permissions home",
        "notebooklm : session",
        "notebooklm : compte",
    ]
    for c in checks.values():
        assert c.ok, c.detail


def test_notebooklm_version_mismatch_is_ko(tmp_path: Path) -> None:
    """Version installée différente de pinned_version → KO."""
    cfg = make_config(tmp_path, archive=_enabled(tmp_path, pinned_version="0.9.9"))
    with patch("importlib.metadata.version", return_value="0.8.3"):
        checks = {c.name: c for c in check_notebooklm(cfg)}
    assert not checks["notebooklm : version"].ok
    assert "0.8.3" in checks["notebooklm : version"].detail
    assert "0.9.9" in checks["notebooklm : version"].detail


def test_notebooklm_account_mismatch_is_ko(tmp_path: Path) -> None:
    """Compte Google connecté ≠ compte attendu → KO."""
    cfg = make_config(tmp_path, archive=_enabled(tmp_path))
    store = Store(tmp_path / "guetteur.db")

    class _Archiver:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def auth_check(self) -> str | None:
            return "autre.compte@gmail.com"  # ≠ config.archive.account

    try:
        with (
            patch("importlib.metadata.version", return_value="0.8.3"),
            patch("guetteur.archive.notebooklm.NotebookLMArchiver", _Archiver),
        ):
            checks = {c.name: c for c in check_notebooklm(cfg, store=store)}
    finally:
        store.close()
    assert not checks["notebooklm : compte"].ok
    assert "autre.compte@gmail.com" in checks["notebooklm : compte"].detail
    assert "guetteur.veille@gmail.com" in checks["notebooklm : compte"].detail


def test_notebooklm_home_bad_perm_is_ko(tmp_path: Path) -> None:
    home = tmp_path / "loose"
    home.mkdir(mode=0o755)
    cfg = make_config(
        tmp_path,
        archive=ArchiveConfig(
            enabled=True,
            notebook_name="Veille",
            home=home,
            max_sources_per_notebook=10,
        ),
    )
    with patch("importlib.metadata.version", return_value="0.8.3"):
        checks = {c.name: c for c in check_notebooklm(cfg)}
    assert not checks["notebooklm : permissions home"].ok
    assert "755" in checks["notebooklm : permissions home"].detail


def test_notebooklm_missing_package_is_ko(tmp_path: Path) -> None:
    """notebooklm-py absent → ligne « version » KO avec commande d'installation."""
    from importlib.metadata import PackageNotFoundError

    cfg = make_config(tmp_path, archive=_enabled(tmp_path))
    with patch("importlib.metadata.version", side_effect=PackageNotFoundError("notebooklm-py")):
        checks = {c.name: c for c in check_notebooklm(cfg)}
    assert not checks["notebooklm : version"].ok
    assert "uv sync" in checks["notebooklm : version"].detail
    # La suite est court-circuitée : pas de check session/compte.
    assert "notebooklm : session" not in checks


def test_notebooklm_forbidden_env_visible_in_permissions_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une variable interdite est signalée dans la ligne permissions (KO)."""
    monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", "anything")
    cfg = make_config(tmp_path, archive=_enabled(tmp_path))
    with patch("importlib.metadata.version", return_value="0.8.3"):
        checks = {c.name: c for c in check_notebooklm(cfg)}
    assert not checks["notebooklm : permissions home"].ok
    assert "NOTEBOOKLM_REFRESH_CMD" in checks["notebooklm : permissions home"].detail
