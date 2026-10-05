"""Collecte Lot 8b : filtrage retweets, dédup, backoff 429, gel 403."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from guetteur.config import XSourceConfig
from guetteur.sources.x_retweets import (
    MAX_INTERVAL_MINUTES,
    collect,
    freeze_reason,
    interval_minutes,
    is_frozen,
    is_initialized,
    mark_initialized,
    unfreeze,
)
from guetteur.sources.x_subprocess import XSourceEnv
from guetteur.store import Store


def _env(tmp_path: Path) -> XSourceEnv:
    return XSourceEnv(
        binary="/bin/true",
        home=tmp_path / "x",
        venv_bin=None,
        auth_token="a",
        ct0="b",
    )


def _xsource(tmp_path: Path, **overrides: object) -> XSourceConfig:
    from dataclasses import replace

    base = XSourceConfig(
        enabled=True,
        account="me",
        watch_handle="mon_compte",
        poll_minutes=30,
        bookmarks=False,
        max_fetch=50,
        twitter_cli_bin="/bin/true",
        home=tmp_path / "x",
        pinned_version="0.8.5",
        timeout_s=60.0,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _mk_runner(payload) -> callable:  # type: ignore[valid-type,no-untyped-def]
    def _runner(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    return _runner


def test_first_run_marks_seen_without_processing(tmp_path: Path, store: Store) -> None:
    payload = [
        {
            "id": "1",
            "retweetedStatus": {"id": "99", "screenName": "elon"},
        }
    ]
    stats = collect(
        store, _xsource(tmp_path), _env(tmp_path), source="x_retweets", runner=_mk_runner(payload)
    )
    assert stats.new_items == 0  # pas de nouveaux au 1er lancement
    assert is_initialized(store, "x_retweets")
    # Item inséré mais marqué sent/vu
    item = store.item_by_url("https://x.com/elon/status/99")
    assert item is not None
    assert item.sent_at is None  # mark_seen → sent sans sent_at
    assert not item.really_sent


def test_second_run_detects_new_retweets(tmp_path: Path, store: Store) -> None:
    # Premier passage, initialise.
    collect(
        store, _xsource(tmp_path), _env(tmp_path), source="x_retweets", runner=_mk_runner([])
    )
    # Second passage, un retweet apparaît.
    payload = [
        {
            "id": "2",
            "retweetedStatus": {"id": "100", "screenName": "elon"},
        }
    ]
    stats = collect(
        store,
        _xsource(tmp_path),
        _env(tmp_path),
        source="x_retweets",
        runner=_mk_runner(payload),
    )
    assert stats.new_items == 1


def test_filter_only_retweets_and_quotes(tmp_path: Path, store: Store) -> None:
    mark_initialized(store, "x_retweets")
    payload = [
        {"id": "1", "text": "Mon propre tweet"},  # ignoré
        {
            "id": "2",
            "retweetedStatus": {"id": "200", "screenName": "foo"},
        },
        {
            "id": "3",
            "isQuoted": True,
            "quotedStatus": {"id": "300", "screenName": "bar"},
        },
    ]
    stats = collect(
        store,
        _xsource(tmp_path),
        _env(tmp_path),
        source="x_retweets",
        runner=_mk_runner(payload),
    )
    assert stats.new_items == 2
    assert store.item_by_url("https://x.com/foo/status/200") is not None
    assert store.item_by_url("https://x.com/bar/status/300") is not None


def test_rate_limit_doubles_interval(tmp_path: Path, store: Store) -> None:
    mark_initialized(store, "x_retweets")

    def rate_limited(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 429 rate limited")

    cfg = _xsource(tmp_path, poll_minutes=30)
    collect(store, cfg, _env(tmp_path), source="x_retweets", runner=rate_limited)
    assert interval_minutes(store, "x_retweets", 30) == 60  # min(240, max(10, 30*2))
    collect(store, cfg, _env(tmp_path), source="x_retweets", runner=rate_limited)
    assert interval_minutes(store, "x_retweets", 30) == 120
    # Plafonne à 4 h.
    for _ in range(5):
        collect(store, cfg, _env(tmp_path), source="x_retweets", runner=rate_limited)
    assert interval_minutes(store, "x_retweets", 30) == MAX_INTERVAL_MINUTES


def test_automated_behavior_freezes_source(tmp_path: Path, store: Store) -> None:
    mark_initialized(store, "x_retweets")

    def automated(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr='Error: "automated behavior" detected'
        )

    stats = collect(
        store, _xsource(tmp_path), _env(tmp_path), source="x_retweets", runner=automated
    )
    assert stats.frozen
    assert is_frozen(store, "x_retweets")
    assert "automated" in freeze_reason(store, "x_retweets")
    # Second appel : nop (déjà gelée)
    stats2 = collect(
        store, _xsource(tmp_path), _env(tmp_path), source="x_retweets", runner=automated
    )
    assert stats2.frozen


def test_auth_error_freezes(tmp_path: Path, store: Store) -> None:
    mark_initialized(store, "x_retweets")

    def auth_bad(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403 cookie expired")

    stats = collect(
        store, _xsource(tmp_path), _env(tmp_path), source="x_retweets", runner=auth_bad
    )
    assert stats.frozen
    assert "cookies" in freeze_reason(store, "x_retweets")


def test_resume_clears_freeze(tmp_path: Path, store: Store) -> None:
    mark_initialized(store, "x_retweets")

    def auth_bad(cmd, env, cwd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 401 ")

    collect(store, _xsource(tmp_path), _env(tmp_path), source="x_retweets", runner=auth_bad)
    assert is_frozen(store, "x_retweets")
    unfreeze(store, "x_retweets")
    assert not is_frozen(store, "x_retweets")


def test_dedup_by_url(tmp_path: Path, store: Store) -> None:
    mark_initialized(store, "x_retweets")
    payload = [
        {"id": "a", "retweetedStatus": {"id": "99", "screenName": "x"}},
        {"id": "b", "retweetedStatus": {"id": "99", "screenName": "x"}},
    ]
    stats = collect(
        store,
        _xsource(tmp_path),
        _env(tmp_path),
        source="x_retweets",
        runner=_mk_runner(payload),
    )
    assert stats.new_items == 1  # second est ignoré (même URL)


def test_bookmarks_uses_distinct_source(tmp_path: Path, store: Store) -> None:
    mark_initialized(store, "x_bookmarks")
    payload = [
        {"id": "500", "url": "https://x.com/user/status/500"},
    ]
    stats = collect(
        store,
        _xsource(tmp_path, bookmarks=True),
        _env(tmp_path),
        source="x_bookmarks",
        runner=_mk_runner(payload),
    )
    assert stats.new_items == 1
    item = store.item_by_url("https://x.com/user/status/500")
    assert item is not None
    assert item.source == "x_bookmarks"


@pytest.fixture
def store() -> Store:
    return Store(":memory:")
