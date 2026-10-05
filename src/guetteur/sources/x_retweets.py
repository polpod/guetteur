"""Lot 8b : collecte des retweets (et signets optionnels) via twitter-cli durci.

À chaque cycle :
1. Appelle `twitter user-posts <watch_handle> --max N --json` (ou
   `twitter bookmarks --max N --json` si l'option `bookmarks=true`).
2. Filtre les retweets et tweets cités.
3. Dédupique par id en base (table items).
4. Insère chaque nouveau comme item LIEN kind="tweet", source="x_retweets" (ou
   "x_bookmarks").
5. Le pipeline LIEN du Lot 8 prend ensuite le relais (fxtwitter → résumé →
   Telegram → Obsidian → archive).

Backoff et gel (A6) :
- 429 ou code=rate_limit : interval courant *= 2, plafonné à 4 h.
- 401/403 ou code=automated : gel de la source (frozen=True), alerte Telegram,
  reprise par `guetteur x resume`.
- 1er lancement : items existants marqués "sent" sans traitement
  (`mark_seen=True`) ; `guetteur x backfill --limit N` relance N tweets.

État persistant via `store.meta` (clefs `x_source.*`) :
- `last_cycle_at` : ISO datetime
- `interval_min_current` : intervalle courant en minutes (après backoff)
- `frozen` : "1" si gelée
- `frozen_reason` : message
- `initialized_<source>` : "1" quand le 1er lancement est passé
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from guetteur.config import XSourceConfig
from guetteur.items import ItemSource
from guetteur.sources.x_subprocess import (
    TwitterCliError,
    XSourceEnv,
    run_twitter_cli,
)
from guetteur.store import Store

log = logging.getLogger(__name__)

MAX_INTERVAL_MINUTES = 240  # 4 h, plafond du backoff
MIN_INTERVAL_MINUTES = 5
DEFAULT_MAX_FETCH = 50


@dataclass(frozen=True)
class CollectStats:
    source: ItemSource
    fetched: int
    new_items: int
    frozen: bool
    reason: str = ""


def _meta_key(source: ItemSource, suffix: str) -> str:
    return f"x_source.{source}.{suffix}"


def is_frozen(store: Store, source: ItemSource) -> bool:
    return store.get_meta(_meta_key(source, "frozen")) == "1"


def freeze_reason(store: Store, source: ItemSource) -> str:
    return store.get_meta(_meta_key(source, "frozen_reason")) or ""


def interval_minutes(store: Store, source: ItemSource, default: int) -> int:
    raw = store.get_meta(_meta_key(source, "interval_min_current"))
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def set_interval_minutes(store: Store, source: ItemSource, minutes: int) -> None:
    capped = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, minutes))
    store.set_meta(_meta_key(source, "interval_min_current"), str(capped))


def mark_frozen(store: Store, source: ItemSource, reason: str) -> None:
    store.set_meta(_meta_key(source, "frozen"), "1")
    store.set_meta(_meta_key(source, "frozen_reason"), reason)
    store.set_meta(
        _meta_key(source, "frozen_at"), datetime.now(UTC).isoformat(timespec="seconds")
    )


def unfreeze(store: Store, source: ItemSource) -> None:
    store.set_meta(_meta_key(source, "frozen"), "0")
    store.set_meta(_meta_key(source, "frozen_reason"), "")


def is_initialized(store: Store, source: ItemSource) -> bool:
    return store.get_meta(_meta_key(source, "initialized")) == "1"


def mark_initialized(store: Store, source: ItemSource) -> None:
    store.set_meta(_meta_key(source, "initialized"), "1")


def mark_last_cycle(store: Store, source: ItemSource) -> None:
    store.set_meta(
        _meta_key(source, "last_cycle_at"), datetime.now(UTC).isoformat(timespec="seconds")
    )


def last_cycle_at(store: Store, source: ItemSource) -> datetime | None:
    raw = store.get_meta(_meta_key(source, "last_cycle_at"))
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _handle_url(entry: dict[str, Any], handle: str) -> str:
    """Construit l'URL canonique https://x.com/<user>/status/<id>.

    `twitter-cli --json` renvoie typiquement : `{"id": "123", "url": "...", …}`.
    Si l'URL manque, on fabrique à partir du screen_name + id du retweet
    (`retweetedStatus.id`) ou du tweet cité (`quotedStatus.id`).
    """
    url = str(entry.get("url") or "").strip()
    if url.startswith("http"):
        return url
    # fallback : construire depuis id
    tweet_id = str(entry.get("id") or entry.get("tweet_id") or "")
    user = str(entry.get("screenName") or entry.get("author") or handle).lstrip("@")
    if tweet_id and user:
        return f"https://x.com/{user}/status/{tweet_id}"
    return ""


def _retweet_or_quote(entry: dict[str, Any]) -> tuple[str, str]:
    """Retourne (kind_hint, inner_tweet_url) si entry est un retweet ou cité,
    sinon ("", ""). kind_hint = 'retweet' | 'quote'."""
    rt = entry.get("retweetedStatus") or entry.get("retweeted_status")
    if isinstance(rt, dict):
        inner = str(rt.get("url") or "")
        if inner.startswith("http"):
            return "retweet", inner
        tid = str(rt.get("id") or rt.get("tweet_id") or "")
        user = str(rt.get("screenName") or rt.get("author") or "").lstrip("@")
        if tid and user:
            return "retweet", f"https://x.com/{user}/status/{tid}"
    if entry.get("isQuoted") or entry.get("is_quote_status"):
        q = entry.get("quotedStatus") or entry.get("quoted_status") or entry
        if isinstance(q, dict):
            qurl = str(q.get("url") or "")
            if qurl.startswith("http"):
                return "quote", qurl
            qid = str(q.get("id") or q.get("tweet_id") or "")
            qu = str(q.get("screenName") or q.get("author") or "").lstrip("@")
            if qid and qu:
                return "quote", f"https://x.com/{qu}/status/{qid}"
    return "", ""


def collect(
    store: Store,
    config: XSourceConfig,
    xenv: XSourceEnv,
    *,
    source: ItemSource = "x_retweets",
    runner: Any = None,
) -> CollectStats:
    """Un cycle de collecte. Soulève AUCUNE exception : les erreurs se
    traduisent en gel + stats (frozen=True) ou en nop (frozen déjà actif)."""
    if is_frozen(store, source):
        return CollectStats(source=source, fetched=0, new_items=0, frozen=True,
                            reason=freeze_reason(store, source))
    if source == "x_bookmarks":
        args = ["bookmarks", "--max", str(config.max_fetch), "--json"]
    else:
        args = [
            "user-posts",
            config.watch_handle.lstrip("@"),
            "--max",
            str(config.max_fetch),
            "--json",
        ]
    try:
        payload = run_twitter_cli(args, xenv, runner=runner)
    except TwitterCliError as exc:
        return _handle_cli_error(store, source, exc, config.poll_minutes)

    entries = payload if isinstance(payload, list) else payload.get("data") or []
    if not isinstance(entries, list):
        entries = []
    log.info("x_source.cycle", extra={"source": source, "entries": len(entries)})

    # Filtrage : on garde uniquement retweets et tweets cités.
    picked: list[str] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        if source == "x_bookmarks":
            url = _handle_url(raw_entry, config.watch_handle)
            if url:
                picked.append(url)
            continue
        kind, inner = _retweet_or_quote(raw_entry)
        if kind and inner:
            picked.append(inner)

    first_run = not is_initialized(store, source)
    new_count = 0
    for url in picked:
        _, inserted = store.add_link(url, "tweet", source, mark_seen=first_run)
        if inserted and not first_run:
            new_count += 1
    mark_initialized(store, source)
    mark_last_cycle(store, source)
    # Succès : reset de l'intervalle à la valeur configurée.
    set_interval_minutes(store, source, max(config.poll_minutes, MIN_INTERVAL_MINUTES))
    return CollectStats(
        source=source,
        fetched=len(entries),
        new_items=new_count,
        frozen=False,
    )


def _handle_cli_error(
    store: Store, source: ItemSource, exc: TwitterCliError, poll_minutes: int
) -> CollectStats:
    if exc.code == "rate_limit":
        # Double l'intervalle courant. Au premier 429, on part du poll_minutes
        # configuré (pas du minimum absolu) pour que `interval = poll * 2`
        # dès le premier backoff.
        current = interval_minutes(store, source, poll_minutes)
        new = min(MAX_INTERVAL_MINUTES, max(MIN_INTERVAL_MINUTES * 2, current * 2))
        set_interval_minutes(store, source, new)
        log.warning(
            "x_source.rate_limited",
            extra={"source": source, "interval_min_new": new},
        )
        return CollectStats(source=source, fetched=0, new_items=0, frozen=False,
                            reason=f"rate limited, intervalle={new} min")
    if exc.code in ("auth", "automated"):
        reason = (
            "challenge « automated behavior »"
            if exc.code == "automated"
            else "cookies rejetés (401/403)"
        )
        mark_frozen(store, source, reason)
        log.warning("x_source.frozen", extra={"source": source, "reason": reason})
        return CollectStats(source=source, fetched=0, new_items=0, frozen=True, reason=reason)
    # Autres erreurs : nop + log.
    log.warning("x_source.error", extra={"source": source, "code": exc.code})
    return CollectStats(
        source=source, fetched=0, new_items=0, frozen=False, reason=f"error: {exc.code}"
    )


def whoami(xenv: XSourceEnv, runner: Any = None) -> str:
    """Retourne le screen_name connecté. Lève TwitterCliError si KO."""
    data = run_twitter_cli(["whoami", "--json"], xenv, runner=runner)
    if isinstance(data, dict):
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(payload, dict):
            sn = (
                payload.get("screenName")
                or payload.get("screen_name")
                or payload.get("username")
                or ""
            )
            return str(sn).lstrip("@")
    raise TwitterCliError(
        "whoami : réponse inattendue", code="invalid_json"
    )
