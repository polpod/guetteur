"""`guetteur health` : la base répond et le service a tourné récemment (heartbeat écrit à chaque
cycle). Avec --alert (timer systemd toutes les 15 min), une alerte Telegram est envoyée si KO,
au plus une par heure."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from guetteur.config import Config
from guetteur.notify.base import Message, Notifier, NotifyError
from guetteur.store import Store, utcnow
from guetteur.summarize.format import escape_markdown_v2

log = logging.getLogger(__name__)

HEARTBEAT_FACTOR = 3  # KO si aucun cycle depuis 3 fois poll_interval_seconds
ALERT_EVERY = timedelta(hours=1)
LAST_ALERT_KEY = "last_health_alert"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str


def _age(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"


def check_health(config: Config, store: Store, now: datetime | None = None) -> list[HealthCheck]:
    now = now or utcnow()
    checks: list[HealthCheck] = []
    try:
        counts = store.counts()
        total = sum(counts.values())
        failed = counts.get("failed", 0)
        checks.append(
            HealthCheck("base", True, f"{config.db_path} ({total} vidéos, {failed} failed)")
        )
    except sqlite3.Error as exc:
        return [HealthCheck("base", False, f"{config.db_path} : {exc}")]

    limit = timedelta(seconds=HEARTBEAT_FACTOR * config.poll_interval_seconds)
    beat = store.heartbeat()
    if beat is None:
        checks.append(HealthCheck("heartbeat", False, "aucun cycle enregistré"))
    else:
        age = now - beat
        ok = age < limit
        detail = f"dernier cycle il y a {_age(age)} (seuil {_age(limit)})"
        checks.append(HealthCheck("heartbeat", ok, detail))
    return checks


def alert_text(checks: list[HealthCheck]) -> str:
    lines = ["🚨 GUETTEUR : healthcheck KO"]
    lines += [f"• {c.name} : {'OK' if c.ok else 'KO'} — {c.detail}" for c in checks]
    lines.append("Vérifiez : journalctl -u guetteur -n 50")
    return "\n".join(lines)


def maybe_alert(
    store: Store,
    checks: list[HealthCheck],
    notifier_factory: Callable[[], Notifier],
    now: datetime | None = None,
) -> str:
    """Retourne "ok" (rien à signaler), "throttled" (alerte déjà envoyée il y a moins d'une
    heure), "sent" ou "error"."""
    if all(c.ok for c in checks):
        return "ok"
    now = now or utcnow()
    last_raw = store.get_meta(LAST_ALERT_KEY)
    if last_raw and now - datetime.fromisoformat(last_raw) < ALERT_EVERY:
        return "throttled"
    text = alert_text(checks)
    try:
        notifier_factory().send(Message(markdown_v2=escape_markdown_v2(text), plain=text))
    except NotifyError as exc:
        log.error("health.alert_failed", extra={"error": str(exc)})
        return "error"
    store.set_meta(LAST_ALERT_KEY, now.isoformat(timespec="seconds"))
    log.warning("health.alert_sent", extra={"checks": [c.name for c in checks if not c.ok]})
    return "sent"
