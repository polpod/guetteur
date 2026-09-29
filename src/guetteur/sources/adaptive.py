"""AdaptiveApiKeySource : route entre l'API par clé et le RSS suivant le quota du jour.

Règles :
- Avant chaque appel, si `quota_used >= HARD_LIMIT` (défaut 9500), on n'appelle PAS l'API :
  on délègue directement à la source RSS et on préserve les ~500 unités restantes du jour.
- Chaque appel API compte 1 unité — incrémenté par la source de bas niveau via `on_call`.
- Alertes Telegram, une seule fois par jour et par palier : à `ALERT_SOFT` (8000) et
  à `HARD_LIMIT` (9500). Les drapeaux d'alerte sont stockés en table `meta` et remis à
  zéro au changement de jour PT (voir `Store.youtube_quota_*`).
- Sur 403 quotaExceeded (arrive quand plusieurs cycles concurrents s'exécutent ou quand
  le compteur local est en retard) : on force le compteur au plafond et on retente le
  même fetch en RSS pour ne pas rater les vidéos du cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from guetteur.models import Video
from guetteur.sources.api import QuotaExceededError, YouTubeApiKeySource
from guetteur.sources.rss import RssSource
from guetteur.store import Store, utcnow

log = logging.getLogger(__name__)

# Palier logiciel : au-delà, on continue mais on prévient — utile pour dimensionner
# le nombre de playlists ou monter à 20 000 unités/jour côté GCP.
ALERT_SOFT_THRESHOLD = 8000
# Plafond dur : Google en accorde 10 000/jour par défaut. On laisse un coussin de 500
# pour l'exceptionnel `guetteur backfill` ou une seconde playlist ajoutée en cours de
# journée sans casser le service.
HARD_LIMIT = 9500

Alert = Callable[[str], None]


class AdaptiveApiKeySource:
    def __init__(
        self,
        api_source: YouTubeApiKeySource,
        rss_source: RssSource,
        store: Store,
        alert: Alert | None = None,
        hard_limit: int = HARD_LIMIT,
        alert_soft: int = ALERT_SOFT_THRESHOLD,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._api = api_source
        self._rss = rss_source
        self._store = store
        self._alert: Alert = alert or (lambda _msg: None)
        self._hard_limit = hard_limit
        self._alert_soft = alert_soft
        self._now = now

    def fetch(self, playlist_id: str) -> list[Video]:
        used = self._store.youtube_quota_used(self._now())
        if used >= self._hard_limit:
            log.info(
                "adaptive.rss_fallback_quota",
                extra={"playlist_id": playlist_id, "quota_used": used},
            )
            return self._rss.fetch(playlist_id)
        try:
            videos = self._api.fetch(playlist_id)
        except QuotaExceededError as exc:
            # Google a compté ; on marque et on bascule.
            self._store.youtube_quota_mark_exhausted(self._hard_limit, self._now())
            log.warning(
                "adaptive.quota_exceeded_fallback",
                extra={"playlist_id": playlist_id, "error": str(exc)},
            )
            self._maybe_alert()
            return self._rss.fetch(playlist_id)
        # L'incrément a été fait par YouTubeApiKeySource.on_call. On lit la nouvelle
        # valeur et on émet éventuellement une alerte.
        self._maybe_alert()
        return videos

    def _maybe_alert(self) -> None:
        used = self._store.youtube_quota_used(self._now())
        if used >= self._hard_limit and self._store.youtube_quota_alert_needed("9k5", self._now()):
            self._alert(
                f"YouTube quota : plafond {self._hard_limit} atteint ({used}/10000). "
                "Bascule sur RSS jusqu'à minuit Pacifique."
            )
        elif used >= self._alert_soft and self._store.youtube_quota_alert_needed("8k", self._now()):
            self._alert(
                f"YouTube quota : {used}/10000 consommés aujourd'hui "
                f"(seuil d'alerte {self._alert_soft}). Bascule RSS prévue à {self._hard_limit}."
            )
