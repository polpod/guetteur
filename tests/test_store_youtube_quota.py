"""Tests du compteur YouTube quota (table meta) : reset à minuit heure du Pacifique,
dé-doublonnage des alertes, exhausted forcé sur 403."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from guetteur.store import Store


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db.sqlite")


def test_counter_starts_at_zero(tmp_path: Path) -> None:
    s = _store(tmp_path)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert s.youtube_quota_used(now) == 0


def test_bump_increments_and_persists(tmp_path: Path) -> None:
    s = _store(tmp_path)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert s.youtube_quota_bump(now=now) == 1
    assert s.youtube_quota_bump(3, now=now) == 4
    assert s.youtube_quota_used(now) == 4


def test_counter_resets_at_new_pt_day(tmp_path: Path) -> None:
    """Google reset le quota à 00:00 heure du Pacifique (UTC-8). Un incrément
    à 07:00 UTC est encore la veille en PT (23:00 J-1) ; à 09:00 UTC on est
    déjà passé au jour J en PT (01:00 J)."""
    s = _store(tmp_path)
    late_yesterday_pt = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)  # 23:00 PT le 28
    early_today_pt = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)  # 01:00 PT le 29
    s.youtube_quota_bump(5, now=late_yesterday_pt)
    assert s.youtube_quota_used(late_yesterday_pt) == 5
    # Passage à un nouveau jour PT → compteur remis à zéro.
    assert s.youtube_quota_used(early_today_pt) == 0


def test_mark_exhausted_sets_counter_to_hard_limit(tmp_path: Path) -> None:
    s = _store(tmp_path)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    s.youtube_quota_bump(10, now=now)
    s.youtube_quota_mark_exhausted(9500, now)
    assert s.youtube_quota_used(now) == 9500


def test_alert_dedup_per_level_per_day(tmp_path: Path) -> None:
    s = _store(tmp_path)
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert s.youtube_quota_alert_needed("8k", now) is True
    assert s.youtube_quota_alert_needed("8k", now) is False
    # Niveau différent, alerte indépendante.
    assert s.youtube_quota_alert_needed("9k5", now) is True
    # Nouveau jour PT → réarmement.
    tomorrow = now + timedelta(days=1)
    assert s.youtube_quota_alert_needed("8k", tomorrow) is True
