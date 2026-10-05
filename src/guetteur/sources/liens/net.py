"""Client HTTP partagé pour la lecture de liens, avec garde SSRF.

Toute requête sortante passe par `safe_fetch` qui :
- impose http(s) uniquement,
- résout le hostname en IP et refuse loopback, lien-local, privé, réservé,
  multicast ou le point d'entrée métadonnées cloud (169.254.169.254 et
  fd00:ec2::254). La vérification tourne SUR CHAQUE redirection — un serveur
  qui renvoie 302 vers http://127.0.0.1 est bloqué même si l'URL initiale
  était publique,
- borne les redirections à 5 et interdit tout schéma hors http/https,
- borne la taille de la réponse (`max_bytes`) et le temps (`timeout_s`),
- envoie un User-Agent explicite GUETTEUR/<version>.

La classe `LinkFetchError` sépare les erreurs de garde (DNS, scheme, IP) des
erreurs réseau pures ; les deux restent lisibles pour l'opérateur Telegram.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# User-Agent explicite : évite que le site logue nos requêtes comme « python-httpx »
# anonyme, et permet aux admins de bloquer proprement si besoin.
USER_AGENT = "GUETTEUR/1.0 (+https://github.com/polpod/guetteur)"

# Taille max d'une réponse brute (avant extraction). 2 MB protège contre un
# serveur qui streame un gigaoctet pour fatiguer le client.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_S = 15.0
MAX_REDIRECTS = 5


class LinkFetchError(RuntimeError):
    """Erreur à la lecture d'un lien : SSRF guard, DNS, HTTP ou taille."""


@dataclass(frozen=True)
class FetchResult:
    url: str  # URL finale après redirections (toutes revérifiées)
    status_code: int
    headers: dict[str, str]
    text: str
    content_type: str


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Retourne un motif de refus humainement lisible, ou None si l'IP est OK.
    Protège contre : loopback, lien-local (AWS metadata 169.254.169.254),
    privé (10/8, 192.168/16, 172.16/12), réservé, multicast, non-globalement
    routable. On refuse même les IPv6 ULA (fc00::/7)."""
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (dont AWS/GCP metadata)"
    if ip.is_private:
        return "IP privée"
    if ip.is_reserved:
        return "IP réservée"
    if ip.is_multicast:
        return "multicast"
    if not ip.is_global:
        return "IP non routable globalement"
    return None


def resolve_safe(host: str) -> list[str]:
    """Résout `host` et refuse si une seule IP tombe dans un bloc interdit.
    Pas de « cherry-pick » : si getaddrinfo rend A1 publique et A2 privée on
    refuse — c'est de l'anti-rebinding, un attaquant qui contrôle le DNS peut
    renvoyer l'une puis l'autre."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise LinkFetchError(f"DNS impossible pour {host!r} : {exc}") from exc
    ips: list[str] = []
    for info in infos:
        raw = info[4][0]
        addr = str(raw)
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reason = _is_blocked_ip(ip)
        if reason is not None:
            raise LinkFetchError(f"Hôte {host!r} résout vers {addr} ({reason}) — refusé")
        ips.append(addr)
    if not ips:
        raise LinkFetchError(f"Aucune IP exploitable pour {host!r}")
    return ips


def _validate_url(url: str) -> str:
    """Vérifie schéma + host et retourne l'URL inchangée. SSRF guard."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LinkFetchError(f"Schéma refusé : {parsed.scheme!r} (http/https uniquement)")
    if not parsed.hostname:
        raise LinkFetchError(f"URL sans hôte : {url!r}")
    resolve_safe(parsed.hostname)
    return url


def _httpx_client(timeout_s: float) -> httpx.Client:
    # follow_redirects=False : on ré-émet nous-mêmes pour revalider à chaque saut.
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout=timeout_s,
        follow_redirects=False,
    )


def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Fetch une URL avec garde SSRF à chaque saut. Retourne un FetchResult.

    Lève `LinkFetchError` si :
    - schéma hors http/https ;
    - hôte dans un bloc interdit (loopback, privé, lien-local, métadonnées) ;
    - trop de redirections ;
    - taille au-delà de `max_bytes` ;
    - erreur HTTP (status >= 400).
    """
    _validate_url(url)
    current = url
    owns_client = client is None
    cli = client or _httpx_client(timeout_s)
    try:
        for hop in range(MAX_REDIRECTS + 1):
            try:
                resp = cli.request(method, current, headers=headers)
            except httpx.HTTPError as exc:
                raise LinkFetchError(f"HTTP {method} {current} : {exc}") from exc
            if resp.is_redirect:
                loc = resp.headers.get("location", "")
                if not loc:
                    raise LinkFetchError(f"Redirection sans Location depuis {current}")
                # Résolution relative + revalidation complète.
                current = str(httpx.URL(current).join(loc))
                _validate_url(current)
                if hop >= MAX_REDIRECTS:
                    raise LinkFetchError(f"Trop de redirections (>{MAX_REDIRECTS}) depuis {url}")
                continue
            if resp.status_code >= 400:
                raise LinkFetchError(f"HTTP {resp.status_code} sur {current}")
            # Taille : lire via .content lève RequestError si trop gros ; on vérifie
            # Content-Length d'abord puis la taille réelle du body.
            cl_raw = resp.headers.get("content-length")
            if cl_raw and cl_raw.isdigit() and int(cl_raw) > max_bytes:
                raise LinkFetchError(
                    f"Content-Length {cl_raw} > plafond {max_bytes} sur {current}"
                )
            body = resp.content
            if len(body) > max_bytes:
                raise LinkFetchError(f"Réponse de {len(body)} octets > plafond {max_bytes}")
            ct = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            return FetchResult(
                url=current,
                status_code=resp.status_code,
                headers={str(k).lower(): str(v) for k, v in resp.headers.items()},
                text=resp.text,
                content_type=ct,
            )
        raise LinkFetchError(f"Trop de redirections (>{MAX_REDIRECTS}) depuis {url}")
    finally:
        if owns_client:
            cli.close()


def safe_json(url: str, **kwargs: Any) -> Any:
    """safe_fetch + parse JSON. Lève LinkFetchError si JSON invalide."""
    import json

    result = safe_fetch(url, **kwargs)
    try:
        return json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise LinkFetchError(f"JSON invalide depuis {url} : {exc}") from exc
