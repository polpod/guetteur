"""Tests du lot déploiement.

Trois blocs :
1. `check_vault` (doctor.check_vault) : ligne « vault : remote joignable » avec
   un remote git réel local (bare) pour valider l'appel sans réseau.
2. Parsing de `/etc/pve/firewall/<CTID>.fw` généré par proxmox-create-lxc.sh :
   ordre des règles OUT (DNS avant DROP LAN, etc.), policy DROP, log_level info.
3. shellcheck sur tous les scripts .sh du projet.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from guetteur.config import ApplicabilityConfig, Config, ObsidianConfig
from guetteur.doctor import check_vault
from tests.helpers import make_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PROXMOX_SCRIPT = SCRIPTS_DIR / "proxmox-create-lxc.sh"


# --- 1. check_vault -------------------------------------------------------------------


def _config_with_vault(tmp_path: Path, **overrides: Any) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    obs_defaults: dict[str, Any] = {
        "enabled": True,
        "path": vault,
        "git_sync": True,
        "git_remote": "",
        "veille_dir": "Veille",
        "projets_dir": "Projets",
    }
    obs_defaults.update(overrides)
    return make_config(
        tmp_path,
        obsidian=ObsidianConfig(**obs_defaults),
        applicability=ApplicabilityConfig(enabled=False),
    )


def test_vault_disabled_returns_nothing(tmp_path: Path) -> None:
    cfg = _config_with_vault(tmp_path)
    cfg = make_config(
        tmp_path,
        obsidian=ObsidianConfig(enabled=False),
    )
    assert check_vault(cfg) == []


def test_vault_sync_disabled_returns_recap_line(tmp_path: Path) -> None:
    cfg = _config_with_vault(tmp_path, git_sync=False, git_remote="")
    checks = check_vault(cfg)
    assert len(checks) == 1
    c = checks[0]
    assert c.name == "vault : remote joignable"
    assert c.ok
    assert "désactivé" in c.detail


def test_vault_remote_reachable_via_bare_repo(tmp_path: Path) -> None:
    """Un bare repo local sert de remote joignable — pas de réseau requis."""
    bare = tmp_path / "vault-veille.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    # Publie un commit initial : sans lui, `git ls-remote HEAD` renvoie 2
    # (aucune ref) et ce n'est pas ce qu'on veut tester.
    work = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", "--quiet", str(bare), str(work)], check=True, capture_output=True
    )
    (work / "README.md").write_text("seed", encoding="utf-8")
    for cmd in (
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "T"],
        ["git", "add", "README.md"],
        ["git", "commit", "-q", "-m", "seed"],
        ["git", "push", "-q", "origin", "HEAD:main"],
    ):
        subprocess.run(cmd, cwd=work, check=True, capture_output=True)

    cfg = _config_with_vault(tmp_path, git_remote=str(bare))
    checks = check_vault(cfg)
    assert len(checks) == 1
    c = checks[0]
    assert c.ok, c.detail
    assert str(bare) in c.detail
    assert "HEAD" in c.detail


def test_vault_remote_unreachable_is_ko(tmp_path: Path) -> None:
    fake = tmp_path / "does-not-exist.git"
    cfg = _config_with_vault(tmp_path, git_remote=str(fake))
    checks = check_vault(cfg)
    assert len(checks) == 1
    assert not checks[0].ok
    assert str(fake) in checks[0].detail


def test_vault_ls_remote_timeout_is_ko(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_vault(tmp_path, git_remote="git@github.com:polpod/vault-veille.git")

    def _timeout(*_a: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=10)

    monkeypatch.setattr("guetteur.doctor.subprocess.run", _timeout)
    checks = check_vault(cfg)
    assert len(checks) == 1
    assert not checks[0].ok
    assert "timeout" in checks[0].detail.lower()
    assert "10" in checks[0].detail


# --- 2. Parsing du .fw généré par proxmox-create-lxc.sh --------------------------------


@pytest.fixture
def fw_file(tmp_path: Path) -> Iterator[Path]:
    """Extrait le heredoc `FW ... FW` de proxmox-create-lxc.sh et l'écrit dans
    tmp_path/120.fw sans exécuter le script (aucun accès Proxmox nécessaire)."""
    src = PROXMOX_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"cat >\"\$FW_FILE\" <<'FW'\n(.*?)\nFW\n", src, re.DOTALL)
    assert match, "heredoc FW introuvable dans proxmox-create-lxc.sh"
    body = match.group(1)
    fw = tmp_path / "120.fw"
    fw.write_text(body, encoding="utf-8")
    yield fw


def _rules(fw: Path) -> list[str]:
    """Renvoie la liste des règles OUT non commentées, dans l'ordre du fichier."""
    lines: list[str] = []
    in_rules = False
    for raw in fw.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line == "[RULES]":
            in_rules = True
            continue
        if line.startswith("[") and in_rules:
            break
        if in_rules and line and not line.startswith("#"):
            lines.append(line)
    return lines


def test_fw_options_are_drop_by_default(fw_file: Path) -> None:
    text = fw_file.read_text(encoding="utf-8")
    assert "[OPTIONS]" in text
    assert re.search(r"^enable:\s*1\b", text, re.M)
    assert re.search(r"^policy_in:\s*DROP\b", text, re.M)
    assert re.search(r"^policy_out:\s*DROP\b", text, re.M)
    # Journalisation activée pour tracer un blocage.
    assert re.search(r"^log_level_(in|out):\s*info\b", text, re.M)


def test_fw_dns_rules_come_before_lan_drops(fw_file: Path) -> None:
    """Point critique : sans DNS avant les DROP LAN, la résolution de noms
    passerait par la policy DROP par défaut et casserait tout appel HTTPS."""
    rules = _rules(fw_file)
    dns_udp = next(i for i, r in enumerate(rules) if "dport 53" in r and "-p udp" in r)
    dns_tcp = next(i for i, r in enumerate(rules) if "dport 53" in r and "-p tcp" in r)
    lan_indices = [
        i
        for i, r in enumerate(rules)
        if "DROP" in r and any(cidr in r for cidr in ("192.168.", "10.0.0.0/8", "172.16.0.0/12"))
    ]
    assert lan_indices, "aucune règle DROP LAN dans le .fw"
    first_lan_drop = min(lan_indices)
    assert dns_udp < first_lan_drop, rules
    assert dns_tcp < first_lan_drop, rules


def test_fw_rules_accept_expected_egress_ports(fw_file: Path) -> None:
    rules = _rules(fw_file)
    accept_ports = {
        int(m.group(1))
        for r in rules
        if r.startswith("OUT ACCEPT")
        for m in [re.search(r"-dport (\d+)", r)]
        if m
    }
    assert {80, 443, 22, 53} <= accept_ports, sorted(accept_ports)
    # ICMP est ACCEPT sans port.
    assert any("-p icmp" in r and r.startswith("OUT ACCEPT") for r in rules)


def test_fw_drop_covers_all_rfc1918_ranges(fw_file: Path) -> None:
    rules = _rules(fw_file)
    dropped = [r for r in rules if r.startswith("OUT DROP")]
    joined = " ".join(dropped)
    for cidr in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"):
        assert cidr in joined, f"{cidr} absent des DROP LAN"


# --- 3. Pare-feu Proxmox : firewall=1 sur net0, host.fw enable:1 ----------------------


def test_pct_net0_carries_firewall_flag_at_create() -> None:
    """La création (pct create --net0 ...) DOIT poser firewall=1 sur le NIC :
    sans lui, /etc/pve/firewall/CTID.fw n'est pas appliqué même si Datacenter
    et Nœud ont la case cochée."""
    src = PROXMOX_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'--net0\s+"([^"]+)"', src)
    assert match, "--net0 introuvable dans pct create"
    assert "firewall=1" in match.group(1), match.group(1)


def test_pct_set_adds_firewall_when_missing() -> None:
    """Idempotence : sur un CTID préexistant sans firewall=1, le script doit
    exécuter pct set --net0 en conservant la config actuelle (…,firewall=1)."""
    src = PROXMOX_SCRIPT.read_text(encoding="utf-8")
    # On cherche à la fois le grep de détection et l'appel pct set correspondant.
    assert re.search(r"grep -qE '\^net0:.*.*firewall=1", src), (
        "détection d'un net0 sans firewall=1 absente"
    )
    assert re.search(r'pct set "\$CTID" --net0 "\$\{?net0_cur\}?,firewall=1"', src), (
        "pct set --net0 …,firewall=1 absent"
    )


def test_host_fw_written_with_enable_1() -> None:
    """Sans host.fw enable: 1, le nœud ignore les règles CT.fw. Le script doit
    écrire ce fichier (idempotence : seulement s'il n'existe pas déjà)."""
    src = PROXMOX_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"cat >\"\$HOST_FW\" <<'HOSTFW'\n(.*?)\nHOSTFW\n", src, re.DOTALL)
    assert match, "heredoc HOSTFW introuvable"
    body = match.group(1)
    assert re.search(r"^\[OPTIONS\]", body, re.M)
    assert re.search(r"^enable:\s*1\b", body, re.M)


def test_host_fw_creation_is_idempotent() -> None:
    """Ne clobber pas un host.fw existant qui aurait déjà des règles utilisateur."""
    src = PROXMOX_SCRIPT.read_text(encoding="utf-8")
    assert 'if [[ ! -f "$HOST_FW" ]]; then' in src


# --- 4. Durcissement systemd : ProtectHome=read-only + ReadWritePaths -----------------


DEPLOY_DIR = REPO_ROOT / "deploy"
HARDENED_UNITS = (
    "guetteur.service",
    "guetteur-health.service",
    "guetteur-nlm-refresh.service",
)


def _unit_directives(unit: Path) -> dict[str, list[str]]:
    """Renvoie un dict {clé: [valeurs]} des directives [Service] (systemd
    autorise la répétition d'une clé, notamment Environment=)."""
    out: dict[str, list[str]] = {}
    in_service = False
    for raw in unit.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line == "[Service]":
            in_service = True
            continue
        if line.startswith("[") and in_service:
            break
        if in_service and "=" in line and not line.startswith("#"):
            key, val = line.split("=", 1)
            out.setdefault(key.strip(), []).append(val.strip())
    return out


@pytest.mark.parametrize("name", HARDENED_UNITS)
def test_unit_protect_home_is_read_only(name: str) -> None:
    """ProtectHome=yes masque /home/guetteur en entier — uv (~/.cache),
    Claude Code (~/.claude) et le binaire notebooklm (~/.local) plantent
    avec 203/EXEC ou EACCES. read-only autorise la lecture tout en
    protégeant les autres users."""
    directives = _unit_directives(DEPLOY_DIR / name)
    assert directives.get("ProtectHome") == ["read-only"], directives.get("ProtectHome")


@pytest.mark.parametrize("name", HARDENED_UNITS)
def test_unit_read_write_paths_are_specific(name: str) -> None:
    """ReadWritePaths énumère uniquement les chemins strictement nécessaires."""
    directives = _unit_directives(DEPLOY_DIR / name)
    rwp = directives.get("ReadWritePaths", [])
    assert rwp, "ReadWritePaths absent"
    paths = rwp[0].split()
    expected = {
        "/opt/guetteur/data",
        "/home/guetteur/.claude",
        "/home/guetteur/.claude.json",
        "/home/guetteur/.cache",
    }
    assert expected <= set(paths), f"{name} manque : {expected - set(paths)}"


@pytest.mark.parametrize("name", HARDENED_UNITS)
def test_unit_uv_cache_dir_is_under_data(name: str) -> None:
    """uv doit écrire son cache sous /opt/guetteur/data, sinon il tape
    ~/.cache qui est en read-only et le sync/tool plante."""
    directives = _unit_directives(DEPLOY_DIR / name)
    envs = directives.get("Environment", [])
    matches = [e for e in envs if e.startswith("UV_CACHE_DIR=")]
    assert len(matches) == 1, envs
    _, value = matches[0].split("=", 1)
    assert value == "/opt/guetteur/data/.uv-cache"


def test_install_lxc_creates_service_home_paths() -> None:
    """install-lxc.sh doit créer .claude/, .cache/, .claude.json et le cache uv,
    tous en propriétaire guetteur — systemd refuse de bind-mount un chemin
    inexistant, donc l'unité ne démarre pas si le script les oublie."""
    src = (SCRIPTS_DIR / "install-lxc.sh").read_text(encoding="utf-8")
    # install -d (dossiers) pour .claude / .cache / .uv-cache.
    for path_frag in (
        r'\$service_home/\.claude"',
        r'\$service_home/\.cache"',
        r'\$INSTALL_DIR/data/\.uv-cache"',
    ):
        pattern = rf'install -d -o "\$SERVICE_USER".*{path_frag}'
        assert re.search(pattern, src), f"install -d manquant pour {path_frag}"
    # .claude.json : fichier JSON vide si absent.
    assert '"$service_home/.claude.json"' in src
    assert "printf '{}\\n' >" in src


def test_install_lxc_installs_notebooklm_as_service_user() -> None:
    """La régression : `uv tool install` sous root plaçait le binaire dans
    /root/.local, illisible pour guetteur (ProtectHome=read-only). Doit
    tourner sous sudo -u guetteur avec UV_CACHE_DIR."""
    src = (SCRIPTS_DIR / "install-lxc.sh").read_text(encoding="utf-8")
    assert re.search(
        r'sudo -u "\$SERVICE_USER" -H env UV_CACHE_DIR="\$INSTALL_DIR/data/\.uv-cache"\s*\\\s*\n\s*"\$UV_BIN" tool install',
        src,
    ), "uv tool install doit tourner sous $SERVICE_USER avec UV_CACHE_DIR"
    assert 'nlm_bin="/home/$SERVICE_USER/.local/bin/notebooklm"' in src


# --- 5. shellcheck --------------------------------------------------------------------


def _shell_scripts() -> list[Path]:
    return sorted(p for p in SCRIPTS_DIR.glob("*.sh") if p.is_file())


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck non installé")
@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_shellcheck_clean(script: Path) -> None:
    proc = subprocess.run(
        ["shellcheck", "--severity=warning", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"shellcheck sur {script.name} :\n{proc.stdout}\n{proc.stderr}"
    )
