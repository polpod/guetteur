"""Appel durci à `twitter` (twitter-cli), conforme à l'audit §9.

Garanties (A1 à A3 du Lot 8b) :

- Argv en liste, pas de shell (`shell=False`), pas d'interpolation.
- PATH du sous-processus **remplacé** par `/usr/bin:/bin:<venv>/bin` ; `uv` n'y
  est pas, donc le fallback d'extraction cookies de twitter-cli (voir audit §3
  point 2) ne peut pas atteindre un binaire `uv` injecté.
- HOME du sous-processus fixé par la config `[x_source] home`, séparé du home
  de l'utilisateur du service, chmod 0700 attendu.
- Env à liste d'autorisation stricte : TWITTER_AUTH_TOKEN, TWITTER_CT0 (et
  LANG/LC_ALL conservés pour l'encodage). TWITTER_BROWSER et
  TWITTER_CHROME_PROFILE sont REFUSÉS au démarrage (voir `verify_env`), pas
  simplement filtrés : la présence seule indique un contrat cassé.
- Timeout 60 s par appel.
- stdout/stderr capturés et passés à `redact()` avant tout logging.

Expose :
- `TwitterCliError` — tout échec, avec le code lisible.
- `verify_env()` — vérifie l'env de GUETTEUR AVANT le lancement du service.
- `run_twitter_cli(args, config, …)` — appel typé qui renvoie le JSON parsé.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from guetteur.archive.base import redact

log = logging.getLogger(__name__)

# Variables d'env refusées : elles orientent twitter-cli vers l'extraction
# navigateur, incompatible avec le service 24/7 et avec notre durcissement.
FORBIDDEN_ENV_VARS = ("TWITTER_BROWSER", "TWITTER_CHROME_PROFILE")

# PATH du sous-processus : /usr/bin, /bin et le venv de GUETTEUR, dans cet
# ordre. On exclut volontairement /usr/local/bin et /home/<user>/.local/bin
# pour que `uv` installé en user ne soit pas récupérable.
_SAFE_PATH_BASE = ["/usr/bin", "/bin"]

DEFAULT_TIMEOUT_S = 60.0


class TwitterCliError(RuntimeError):
    """Échec de twitter-cli (401/403, 429, binaire absent, JSON invalide…).

    `code` donne un signal structuré : `auth`, `rate_limit`, `automated`,
    `not_found`, `invalid_json`, `missing_binary`, `other`.
    """

    def __init__(self, message: str, *, code: str = "other", stderr: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.stderr = stderr


@dataclass(frozen=True)
class XSourceEnv:
    binary: str
    home: Path
    venv_bin: Path | None
    auth_token: str
    ct0: str


def verify_env(env: dict[str, str] | None = None) -> list[str]:
    """Retourne les variables d'env interdites présentes dans `env` (ou
    `os.environ` par défaut). Un service bien configuré ne doit retourner
    aucune ligne : voir A2 du Lot 8b."""
    env = env if env is not None else dict(os.environ)
    return [name for name in FORBIDDEN_ENV_VARS if env.get(name)]


def _build_safe_env(xenv: XSourceEnv, parent_env: dict[str, str]) -> dict[str, str]:
    """Env minimal passé au sous-processus. Pas de PATH hérité du parent."""
    path_parts = list(_SAFE_PATH_BASE)
    if xenv.venv_bin is not None:
        path_parts.insert(0, str(xenv.venv_bin))
    child = {
        "PATH": ":".join(path_parts),
        "HOME": str(xenv.home),
        "TWITTER_AUTH_TOKEN": xenv.auth_token,
        "TWITTER_CT0": xenv.ct0,
    }
    # LC/LANG : purement cosmétique (encodage de la sortie), inoffensif.
    for key in ("LANG", "LC_ALL", "LC_MESSAGES", "LC_CTYPE"):
        value = parent_env.get(key)
        if value:
            child[key] = value
    return child


def _ensure_home(home: Path) -> None:
    """Crée le HOME dédié avec droits 0700, idempotent."""
    home.mkdir(parents=True, exist_ok=True)
    # Best-effort : si le dossier existe déjà avec des droits plus permissifs
    # on resserre. Si on n'a pas les droits (dossier d'un autre user), on log.
    try:
        home.chmod(0o700)
    except PermissionError:
        log.warning("x_source.home_chmod_denied", extra={"home": str(home)})


def run_twitter_cli(
    args: list[str],
    xenv: XSourceEnv,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: Any = None,
) -> dict[str, Any] | list[Any]:
    """Exécute `twitter <args>` et parse la sortie JSON.

    - argv en liste, shell=False, cwd=HOME dédié.
    - env strictement filtré par `_build_safe_env`.
    - stdout/stderr capturés, tronqués et passés dans `redact()` avant log.
    - timeout : TwitterCliError(code="other", "timeout").

    Reconnaît et re-code les erreurs classiques :
    - exit 1 + stderr avec « automated behavior » → `automated`
    - exit 1 + stderr avec « 401 »/« 403 »/« Cookie expired » → `auth`
    - exit 1 + stderr avec « 429 »/« Rate limited » → `rate_limit`
    """
    binary = shutil.which(xenv.binary) or xenv.binary
    if not Path(binary).exists():
        raise TwitterCliError(
            f"Binaire twitter-cli introuvable : {binary!r}", code="missing_binary"
        )
    _ensure_home(xenv.home)
    env = _build_safe_env(xenv, dict(os.environ))

    cmd: list[str] = [binary, *args]
    log.debug("x_source.subprocess", extra={"argv_head": cmd[:3]})

    try:
        process = (runner or subprocess.run)(
            cmd,
            env=env,
            cwd=str(xenv.home),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning(
            "x_source.timeout",
            extra={"timeout_s": timeout_s, "stdout": redact(str(exc.stdout or "")[:300])},
        )
        raise TwitterCliError(
            f"twitter-cli : timeout après {timeout_s}s", code="other"
        ) from exc
    except FileNotFoundError as exc:
        raise TwitterCliError(
            f"Binaire introuvable : {binary!r}", code="missing_binary"
        ) from exc

    stdout = redact(str(process.stdout or "")[:4000])
    stderr = redact(str(process.stderr or "")[:4000])
    if process.returncode != 0:
        code = _classify_error(stderr)
        log.info(
            "x_source.nonzero",
            extra={"returncode": process.returncode, "code": code, "stderr": stderr[:300]},
        )
        raise TwitterCliError(
            f"twitter-cli exit {process.returncode} ({code})",
            code=code,
            stderr=stderr,
        )
    try:
        data = json.loads(process.stdout or "null")
    except json.JSONDecodeError as exc:
        log.warning("x_source.invalid_json", extra={"stdout": stdout[:300]})
        raise TwitterCliError(
            f"Sortie JSON invalide : {exc}", code="invalid_json", stderr=stderr
        ) from exc
    if not isinstance(data, (dict, list)):
        raise TwitterCliError(
            f"Sortie JSON inattendue (type {type(data).__name__})",
            code="invalid_json",
            stderr=stderr,
        )
    return data


def _classify_error(stderr: str) -> str:
    low = (stderr or "").lower()
    if "automated behavior" in low or "code 226" in low or "automated" in low:
        return "automated"
    if "401" in low or "403" in low or "cookie expired" in low:
        return "auth"
    if "429" in low or "rate limited" in low or "code 88" in low or "code 348" in low:
        return "rate_limit"
    if "not found" in low or "404" in low:
        return "not_found"
    return "other"


def default_venv_bin() -> Path:
    """`<sys.executable>`'s dir, utilisé par défaut pour le PATH enfant."""
    return Path(sys.executable).parent
