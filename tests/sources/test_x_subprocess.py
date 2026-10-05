"""Durcissement du sous-processus twitter-cli (A1-A3 du Lot 8b)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from guetteur.sources.x_subprocess import (
    FORBIDDEN_ENV_VARS,
    TwitterCliError,
    XSourceEnv,
    _build_safe_env,
    _classify_error,
    run_twitter_cli,
    verify_env,
)


def test_verify_env_detects_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWITTER_BROWSER", "chrome")
    bad = verify_env()
    assert "TWITTER_BROWSER" in bad


def test_verify_env_empty_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in FORBIDDEN_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    assert verify_env() == []


def test_build_safe_env_strips_path_and_adds_cookies(tmp_path: Path) -> None:
    xenv = XSourceEnv(
        binary="twitter",
        home=tmp_path / "x",
        venv_bin=tmp_path / "venv" / "bin",
        auth_token="aa",
        ct0="bb",
    )
    env = _build_safe_env(xenv, {"PATH": "/dangerous/path", "FOO": "bar"})
    assert env["PATH"] == f"{tmp_path / 'venv' / 'bin'}:/usr/bin:/bin"
    assert env["HOME"] == str(tmp_path / "x")
    assert env["TWITTER_AUTH_TOKEN"] == "aa"
    assert env["TWITTER_CT0"] == "bb"
    assert "FOO" not in env  # pas d'héritage du parent


def test_run_twitter_cli_rejects_missing_binary(tmp_path: Path) -> None:
    xenv = XSourceEnv(
        binary="/does/not/exist/twitter",
        home=tmp_path / "x",
        venv_bin=None,
        auth_token="a",
        ct0="b",
    )
    with pytest.raises(TwitterCliError) as exc:
        run_twitter_cli(["whoami", "--json"], xenv)
    assert exc.value.code == "missing_binary"


def test_run_twitter_cli_argv_and_env_passed(tmp_path: Path) -> None:
    """Vérifie que l'argv et l'env sont bien ceux qu'on attend : pas de shell,
    pas de PATH hérité."""
    xenv = XSourceEnv(
        binary="/bin/true",  # existe toujours
        home=tmp_path / "x",
        venv_bin=tmp_path / "venv" / "bin",
        auth_token="a",
        ct0="b",
    )

    captured: dict[str, object] = {}

    def fake_runner(
        cmd: list[str], env: dict[str, str], cwd: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["env"] = env
        captured["cwd"] = cwd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True}), stderr="")

    out = run_twitter_cli(["whoami", "--json"], xenv, runner=fake_runner)
    assert out == {"ok": True}
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "/bin/true" or cmd[0].endswith("/bin/true")
    assert cmd[1:] == ["whoami", "--json"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "TWITTER_BROWSER" not in env
    assert env["TWITTER_AUTH_TOKEN"] == "a"
    assert str(tmp_path / "venv" / "bin") in env["PATH"]
    assert "/usr/local/bin" not in env["PATH"]  # uv potentiellement caché
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("shell") is False
    assert kwargs.get("timeout") == 60.0


def test_run_twitter_cli_classifies_rate_limit(tmp_path: Path) -> None:
    xenv = XSourceEnv(
        binary="/bin/true",
        home=tmp_path / "x",
        venv_bin=None,
        auth_token="a",
        ct0="b",
    )

    def fake_runner(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Rate limited (code 88)"
        )

    with pytest.raises(TwitterCliError) as exc:
        run_twitter_cli(["bookmarks"], xenv, runner=fake_runner)
    assert exc.value.code == "rate_limit"


def test_run_twitter_cli_classifies_automated(tmp_path: Path) -> None:
    xenv = XSourceEnv(
        binary="/bin/true",
        home=tmp_path / "x",
        venv_bin=None,
        auth_token="a",
        ct0="b",
    )

    def fake_runner(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr='Error: "automated behavior" (226)'
        )

    with pytest.raises(TwitterCliError) as exc:
        run_twitter_cli(["bookmarks"], xenv, runner=fake_runner)
    assert exc.value.code == "automated"


def test_run_twitter_cli_timeout(tmp_path: Path) -> None:
    xenv = XSourceEnv(
        binary="/bin/true",
        home=tmp_path / "x",
        venv_bin=None,
        auth_token="a",
        ct0="b",
    )

    def fake_runner(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 60))

    with pytest.raises(TwitterCliError) as exc:
        run_twitter_cli(["bookmarks"], xenv, runner=fake_runner)
    assert "timeout" in str(exc.value).lower()


def test_run_twitter_cli_invalid_json(tmp_path: Path) -> None:
    xenv = XSourceEnv(
        binary="/bin/true",
        home=tmp_path / "x",
        venv_bin=None,
        auth_token="a",
        ct0="b",
    )

    def fake_runner(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 0, stdout="pas du json", stderr="")

    with pytest.raises(TwitterCliError) as exc:
        run_twitter_cli(["whoami"], xenv, runner=fake_runner)
    assert exc.value.code == "invalid_json"


def test_run_twitter_cli_creates_home_0700(tmp_path: Path) -> None:
    home = tmp_path / "x"
    xenv = XSourceEnv(
        binary="/bin/true", home=home, venv_bin=None, auth_token="a", ct0="b"
    )

    def fake_runner(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    run_twitter_cli(["whoami"], xenv, runner=fake_runner)
    assert home.exists()
    assert (home.stat().st_mode & 0o777) == 0o700


def test_classify_error_variants() -> None:
    assert _classify_error("HTTP 401 Unauthorized") == "auth"
    assert _classify_error("Cookie expired") == "auth"
    assert _classify_error("HTTP 429 Too Many Requests") == "rate_limit"
    assert _classify_error("automated behavior detected") == "automated"
    assert _classify_error("User not found (404)") == "not_found"
    assert _classify_error("something else") == "other"
