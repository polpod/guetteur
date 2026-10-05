"""`guetteur doctor` : vérifie l'environnement et affiche un tableau OK/KO."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from guetteur.config import Config
from guetteur.store import Store
from guetteur.summarize.base import SummarizeError
from guetteur.summarize.claude_code import ClaudeCodeSummarizer

Which = Callable[[str], str | None]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _version(path: str) -> str:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else ""


def check_claude(
    config: Config, which: Which = shutil.which, summarizer: ClaudeCodeSummarizer | None = None
) -> list[Check]:
    binary = config.summarize.claude_code_bin
    path = which(binary)
    if path is None:
        return [
            Check("claude : binaire", False, f"« {binary} » introuvable dans le PATH"),
            Check("claude : session", False, "non testée (binaire absent)"),
        ]
    version = _version(path)
    checks = [Check("claude : binaire", True, f"{path} {version}".strip())]
    s = summarizer or ClaudeCodeSummarizer(
        model=config.claude_model, binary=path, timeout_s=min(config.summarize.timeout_s, 90)
    )
    try:
        answer = s.ping()
        checks.append(Check("claude : session", True, f"ping → {answer[:40]!r}"))
    except SummarizeError as exc:
        checks.append(Check("claude : session", False, str(exc)))
    return checks


def check_ffmpeg(which: Which = shutil.which) -> Check:
    path = which("ffmpeg")
    if path is None:
        return Check("ffmpeg", False, "introuvable (requis pour le secours whisper)")
    return Check("ffmpeg", True, f"{path} {_version(path)}".strip()[:80])


def check_database(config: Config) -> Check:
    try:
        store = Store(config.db_path)
        try:
            counts = store.counts()
        finally:
            store.close()
    except (sqlite3.Error, OSError) as exc:
        return Check("base SQLite", False, f"{config.db_path} : {exc}")
    total = sum(counts.values())
    detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "vide"
    return Check("base SQLite", True, f"{config.db_path} ({total} vidéos : {detail})")


def _tokens(name: str, pairs: tuple[tuple[str, str], ...]) -> Check:
    missing = [key for key, value in pairs if not value]
    if missing:
        return Check(name, False, "manquant : " + ", ".join(missing))
    return Check(name, True, " et ".join(key for key, _ in pairs) + " présents")


def check_secrets(config: Config) -> list[Check]:
    s = config.secrets
    channels = {p.notify for p in config.playlists} or {"telegram"}
    checks: list[Check] = []
    if "telegram" in channels:
        checks.append(
            _tokens(
                "tokens Telegram",
                (
                    ("TELEGRAM_BOT_TOKEN", s.telegram_bot_token),
                    ("TELEGRAM_CHAT_ID", s.telegram_chat_id),
                ),
            )
        )
    if "whatsapp" in channels:
        checks.append(
            _tokens(
                "tokens WhatsApp",
                (("WA_TOKEN", s.wa_token), ("WA_PHONE_ID", s.wa_phone_id), ("WA_TO", s.wa_to)),
            )
        )
    if config.summarize.provider == "claude_api":
        checks.append(_tokens("ANTHROPIC_API_KEY", (("ANTHROPIC_API_KEY", s.anthropic_api_key),)))
    return checks


def check_vault(config: Config) -> list[Check]:
    """Ligne « vault : remote joignable » ajoutée quand [obsidian] est activé avec
    un remote git : on tente un `git ls-remote` (timeout 10 s) via SSH, sans écrire.
    En mode `git_sync = false` ou `git_remote = ""`, on ne renvoie qu'une ligne
    récapitulative pour rester informatif."""
    if not config.obsidian.enabled:
        return []
    if not config.obsidian.git_sync or not config.obsidian.git_remote:
        return [
            Check(
                "vault : remote joignable",
                True,
                "sync git désactivé (git_sync = false ou git_remote vide)",
            )
        ]
    remote = config.obsidian.git_remote
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--", remote, "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [Check("vault : remote joignable", False, f"timeout > 10 s ({remote})")]
    except OSError as exc:
        return [Check("vault : remote joignable", False, f"git introuvable : {exc}")]
    if proc.returncode == 0:
        head = (proc.stdout.split() or [""])[0][:12]
        return [Check("vault : remote joignable", True, f"{remote} (HEAD {head})")]
    err = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    return [Check("vault : remote joignable", False, f"{remote} : {err[0][:120]}")]


def check_youtube_source(config: Config) -> list[Check]:
    """Deux lignes :

    - « youtube : source » — mode configuré (auto/rss/api) et résolution effective
      (« api : X playlist(s), rss : Y playlist(s), oauth : Z privée(s) »).
    - « youtube : clé API » (seulement si mode auto/api ET clé présente) — vérifie
      qu'elle est acceptée en pingant playlistItems.list?maxResults=1 sur la
      première playlist publique, et affiche le quota consommé du jour.
    """
    import httpx

    from guetteur.sources.api import PLAYLIST_ITEMS_URL

    api_key = config.secrets.youtube_api_key
    mode = config.source
    public_playlists = [p for p in config.playlists if not p.private]
    private_playlists = [p for p in config.playlists if p.private]

    if mode == "rss" or (mode == "auto" and not api_key):
        effective = "rss"
    elif mode == "api" and not api_key:
        return [
            Check(
                "youtube : source",
                False,
                "general.source = 'api' mais YOUTUBE_API_KEY absente dans .env",
            )
        ]
    else:
        effective = "api"

    parts = []
    if public_playlists:
        parts.append(f"{effective} : {len(public_playlists)} playlist(s)")
    if private_playlists:
        parts.append(f"oauth : {len(private_playlists)} privée(s)")
    detail_source = f"{mode} → {', '.join(parts)}" if parts else f"{mode} → aucune playlist"
    checks: list[Check] = [Check("youtube : source", True, detail_source)]

    if effective != "api" or not api_key:
        return checks

    # Ping playlistItems.list : coûte 1 unité de quota, ne compte pas côté compteur
    # local (test ponctuel, admin).
    if not public_playlists:
        checks.append(
            Check(
                "youtube : clé API",
                True,
                "clé présente ; aucune playlist publique à tester",
            )
        )
        return checks
    test_pid = public_playlists[0].id
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                PLAYLIST_ITEMS_URL,
                params={
                    "part": "id",
                    "playlistId": test_pid,
                    "maxResults": 1,
                    "key": api_key,
                },
            )
    except httpx.HTTPError as exc:
        # On n'interpole pas `exc` : certaines sous-classes httpx (TimeoutException,
        # ProxyError…) peuvent inclure l'URL — qui porte `?key=...`.
        checks.append(Check("youtube : clé API", False, f"réseau : {type(exc).__name__}"))
        return checks
    if resp.status_code == 200:
        used = _read_quota_used(config)
        checks.append(
            Check(
                "youtube : clé API",
                True,
                f"ping {test_pid} OK (quota consommé aujourd'hui : {used}/10000)",
            )
        )
        return checks
    if resp.status_code == 403:
        checks.append(
            Check(
                "youtube : clé API",
                False,
                f"403 : clé refusée ou quota déjà épuisé ({resp.text[:120]})",
            )
        )
    elif resp.status_code == 404:
        checks.append(
            Check(
                "youtube : clé API",
                False,
                f"404 sur {test_pid} : la playlist ne devrait pas être privée",
            )
        )
    else:
        checks.append(
            Check("youtube : clé API", False, f"HTTP {resp.status_code} : {resp.text[:120]}")
        )
    return checks


def _read_quota_used(config: Config) -> int:
    try:
        store = Store(config.db_path)
    except Exception:
        return 0
    try:
        return store.youtube_quota_used()
    finally:
        store.close()


def check_notebooklm(config: Config, store: Store | None = None) -> list[Check]:
    """4 lignes attendues par l'utilisateur en Lot 3 : version épinglée, permissions
    du home dédié, session (auth check), compte Google. Toutes gérées sans lever."""
    from guetteur.archive.base import FORBIDDEN_ENV_VARS, ArchiveError

    if not config.archive.enabled:
        return [Check("notebooklm : archivage", True, "désactivé (archive.enabled = false)")]

    checks: list[Check] = []

    # 1. version
    expected = config.archive.pinned_version
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            got = version("notebooklm-py")
            ok_v = got == expected
            checks.append(
                Check(
                    "notebooklm : version",
                    ok_v,
                    f"{got} (attendu {expected})" if not ok_v else f"{got}",
                )
            )
        except PackageNotFoundError:
            checks.append(
                Check(
                    "notebooklm : version",
                    False,
                    f"notebooklm-py absent : uv sync --frozen --no-dev --extra notebooklm "
                    f"(épinglage attendu : {expected})",
                )
            )
            return checks
    except ImportError:  # pragma: no cover
        checks.append(Check("notebooklm : version", False, "importlib.metadata indisponible"))
        return checks

    # 2. permissions du home dédié
    home = config.archive.home
    if not home.exists():
        checks.append(
            Check(
                "notebooklm : permissions home",
                False,
                f"{home} absent — sera créé au 1er démarrage (mkdir 0700)",
            )
        )
    else:
        mode = home.stat().st_mode & 0o777
        details = [f"{home} (0{mode:o})"]
        ok_perms = mode == 0o700
        for filename in ("storage_state.json", "master_token.json"):
            for p in home.rglob(filename):
                m = p.stat().st_mode & 0o777
                details.append(f"{p.name}=0{m:o}")
                if m != 0o600:
                    ok_perms = False
        forbidden = sorted(v for v in FORBIDDEN_ENV_VARS if v in os.environ)
        if forbidden:
            ok_perms = False
            details.append(f"env interdit : {','.join(forbidden)}")
        checks.append(Check("notebooklm : permissions home", ok_perms, "; ".join(details)))

    # 3. session — la vérification vit dans le client, on l'appelle pour de vrai
    #    (l'échec attrape ArchiveError et donne le motif redacté).
    account_email: str | None = None
    try:
        from guetteur.archive.notebooklm import NotebookLMArchiver

        managed_store = store or Store(config.db_path)
        close_after = store is None
        try:
            archiver = NotebookLMArchiver(config.archive, managed_store)
            account_email = archiver.auth_check()
        finally:
            if close_after:
                managed_store.close()
        checks.append(
            Check(
                "notebooklm : session",
                account_email is not None,
                f"connecté ({account_email})" if account_email else "aucun email renvoyé",
            )
        )
    except ArchiveError as exc:
        checks.append(Check("notebooklm : session", False, str(exc)))
        return checks
    except Exception as exc:  # pragma: no cover
        checks.append(Check("notebooklm : session", False, f"{type(exc).__name__}: {exc}"))
        return checks

    # 4. compte
    expected_account = config.archive.account
    if not expected_account:
        checks.append(
            Check(
                "notebooklm : compte",
                True,
                f"aucun compte attendu (archive.account vide) — connecté : {account_email or '?'}",
            )
        )
    else:
        ok_a = account_email == expected_account
        detail = (
            f"{account_email} = {expected_account}"
            if ok_a
            else f"{account_email} ≠ {expected_account} (attendu)"
        )
        checks.append(Check("notebooklm : compte", ok_a, detail))
    return checks


def check_x_source(config: Config) -> list[Check]:
    """Lot 8b : vérifie la configuration twitter-cli et le compte connecté.

    - version du paquet installée == `x_source.pinned_version` ;
    - variables d'env TWITTER_AUTH_TOKEN + TWITTER_CT0 présentes ;
    - variables d'env interdites (TWITTER_BROWSER / TWITTER_CHROME_PROFILE)
      ABSENTES — refus de démarrer si une seule est posée ;
    - HOME dédié existe en 0700 ;
    - `twitter whoami --json` renvoie exactement `account`.
    """
    if not config.x_source.enabled:
        return [Check("x_source : collecte", True, "désactivé (x_source.enabled = false)")]
    checks: list[Check] = []

    expected = config.x_source.pinned_version
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            got = version("twitter-cli")
            ok_v = got == expected
            checks.append(
                Check("x_source : version", ok_v, f"attendu {expected}, trouvé {got}")
            )
        except PackageNotFoundError:
            checks.append(
                Check(
                    "x_source : version",
                    False,
                    f"twitter-cli absent : uv sync --no-dev --no-build --extra x "
                    f"(attendu {expected})",
                )
            )
    except ImportError:
        checks.append(Check("x_source : version", False, "importlib.metadata indisponible"))

    import os

    from guetteur.sources.x_subprocess import FORBIDDEN_ENV_VARS, XSourceEnv, verify_env

    bad = verify_env()
    checks.append(
        Check(
            "x_source : variables interdites",
            not bad,
            "aucune" if not bad else f"présentes : {', '.join(bad)} — refuser de démarrer",
        )
    )
    _ = FORBIDDEN_ENV_VARS  # garde l'import visible (traçabilité audit)

    auth_token = os.environ.get("TWITTER_AUTH_TOKEN", "")
    ct0 = os.environ.get("TWITTER_CT0", "")
    checks.append(
        Check(
            "x_source : cookies",
            bool(auth_token and ct0),
            "TWITTER_AUTH_TOKEN et TWITTER_CT0 présents"
            if auth_token and ct0
            else "TWITTER_AUTH_TOKEN et/ou TWITTER_CT0 manquant dans .env",
        )
    )

    home = config.x_source.home
    try:
        home.mkdir(parents=True, exist_ok=True)
        mode = home.stat().st_mode & 0o777
        ok_perms = mode == 0o700 and home.is_dir()
        checks.append(
            Check(
                "x_source : HOME 0700",
                ok_perms,
                f"{home} mode={mode:04o}" if ok_perms else f"{home} mode={mode:04o} (attendu 0700)",
            )
        )
    except OSError as exc:
        checks.append(Check("x_source : HOME 0700", False, f"{home} : {exc}"))

    # Si cookies manquants ou variables interdites : on ne tente pas whoami.
    if not (auth_token and ct0) or bad:
        checks.append(
            Check("x_source : compte connecté", False, "cookies ou env à corriger avant whoami")
        )
        return checks

    try:
        xenv = XSourceEnv(
            binary=config.x_source.twitter_cli_bin,
            home=home,
            venv_bin=None,
            auth_token=auth_token,
            ct0=ct0,
        )
        from guetteur.sources.x_retweets import whoami

        got_handle = whoami(xenv)
        expected_handle = config.x_source.account.lstrip("@")
        ok_a = got_handle.lower() == expected_handle.lower()
        detail = (
            f"@{got_handle} = @{expected_handle}"
            if ok_a
            else f"@{got_handle} ≠ @{expected_handle} (attendu)"
        )
        checks.append(Check("x_source : compte connecté", ok_a, detail))
    except Exception as exc:
        checks.append(Check("x_source : compte connecté", False, f"{type(exc).__name__}: {exc}"))
    return checks


def run_checks(
    config: Config, which: Which = shutil.which, claude: ClaudeCodeSummarizer | None = None
) -> list[Check]:
    backend = f"{config.summarize.provider} ({config.claude_model})"
    checks = [Check("backend de résumé", True, backend)]
    if config.summarize.provider == "claude_code":
        checks += check_claude(config, which, claude)
    checks.append(check_ffmpeg(which))
    checks.append(check_database(config))
    checks += check_secrets(config)
    checks += check_youtube_source(config)
    checks += check_vault(config)
    checks += check_notebooklm(config)
    checks += check_x_source(config)
    return checks


def render_table(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{'Vérification'.ljust(width)}  État  Détail", f"{'-' * width}  ----  ------"]
    for c in checks:
        lines.append(f"{c.name.ljust(width)}  {'OK ' if c.ok else 'KO '}   {c.detail}")
    return "\n".join(lines)
