"""Tests de la sélection automatique de la source par build_pipeline._build_public_source.

3 modes (auto/rss/api) combinés avec la présence/absence de YOUTUBE_API_KEY, plus
les cas playlists privées (OAuth toujours préservé, indépendamment de la clé)."""

from __future__ import annotations

from pathlib import Path

import pytest

from guetteur.config import ConfigError, Secrets
from guetteur.main import _build_public_source
from guetteur.sources.adaptive import AdaptiveApiKeySource
from guetteur.sources.rss import RssSource
from guetteur.store import Store
from tests.helpers import make_config


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "guetteur.db")


def _rss() -> RssSource:
    return RssSource()


def test_auto_without_key_falls_back_to_rss(tmp_path: Path, store: Store) -> None:
    cfg = make_config(tmp_path, secrets=Secrets(youtube_api_key=""))
    src = _build_public_source(cfg, store, _rss())
    assert isinstance(src, RssSource), type(src)


def test_auto_with_key_selects_adaptive_api(tmp_path: Path, store: Store) -> None:
    cfg = make_config(
        tmp_path,
        source="auto",
        secrets=Secrets(youtube_api_key="AIza-test"),
    )
    src = _build_public_source(cfg, store, _rss())
    assert isinstance(src, AdaptiveApiKeySource)


def test_forced_rss_ignores_key(tmp_path: Path, store: Store) -> None:
    cfg = make_config(
        tmp_path,
        source="rss",
        secrets=Secrets(youtube_api_key="AIza-test"),
    )
    src = _build_public_source(cfg, store, _rss())
    assert isinstance(src, RssSource)


def test_forced_api_requires_key(tmp_path: Path, store: Store) -> None:
    cfg = make_config(tmp_path, source="api", secrets=Secrets(youtube_api_key=""))
    with pytest.raises(ConfigError, match="YOUTUBE_API_KEY"):
        _build_public_source(cfg, store, _rss())


def test_forced_api_with_key_selects_adaptive(tmp_path: Path, store: Store) -> None:
    cfg = make_config(
        tmp_path,
        source="api",
        secrets=Secrets(youtube_api_key="AIza-test"),
    )
    src = _build_public_source(cfg, store, _rss())
    assert isinstance(src, AdaptiveApiKeySource)


def test_poll_interval_defaults_to_60_when_api_capable() -> None:
    """poll_interval_seconds descend à 60 s par défaut dès qu'il y a une clé API
    (mode auto ou api). En rss ou sans clé, on reste sur 300 s."""
    from guetteur.config import parse_config

    cfg = parse_config({"playlists": []}, Secrets(youtube_api_key="AIza"))
    assert cfg.poll_interval_seconds == 60
    cfg = parse_config({"playlists": []}, Secrets(youtube_api_key=""))
    assert cfg.poll_interval_seconds == 300
    # Mode api forcé, quelle que soit la clé, on est à 60 par défaut.
    cfg = parse_config({"general": {"source": "api"}, "playlists": []}, Secrets())
    assert cfg.poll_interval_seconds == 60
    # Mode rss forcé, même avec clé : 300.
    cfg = parse_config(
        {"general": {"source": "rss"}, "playlists": []},
        Secrets(youtube_api_key="AIza"),
    )
    assert cfg.poll_interval_seconds == 300


def test_poll_interval_explicit_wins_over_default() -> None:
    from guetteur.config import parse_config

    cfg = parse_config(
        {"general": {"poll_interval_seconds": 120}, "playlists": []},
        Secrets(youtube_api_key="AIza"),
    )
    assert cfg.poll_interval_seconds == 120


def test_invalid_source_value_is_rejected() -> None:
    from guetteur.config import parse_config

    with pytest.raises(ConfigError, match=r"general\.source"):
        parse_config({"general": {"source": "graphql"}, "playlists": []}, Secrets())
