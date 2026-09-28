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


# --- 3. shellcheck --------------------------------------------------------------------


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
