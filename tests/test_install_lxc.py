"""Tests unitaires de la fonction `verify_github_keys` de scripts/install-lxc.sh.

La fonction filtre les lignes ssh-keyscan par empreinte SHA256 GitHub. On la charge
en sourçant le script avec `INSTALL_LXC_TESTING=1` (garde en tête du script) pour
éviter d'exécuter le corps principal (qui exige root).

Quatre cas :
1. Les 3 clés publiées : toutes validées, sortie identique à l'entrée.
2. Deux clés valides mélangées avec du bruit : seules les valides sortent.
3. Une seule clé valide : elle seule sort.
4. Aucune clé valide (paires ed25519 fraîches, empreintes ne correspondant à rien) :
   sortie vide et code retour non nul.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_LXC = REPO_ROOT / "scripts" / "install-lxc.sh"

# Les 3 clés publiques de github.com telles qu'elles apparaissent dans la sortie de
# ssh-keyscan (host algo base64). Empreintes SHA256 publiées :
#   ed25519 → SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
#   ecdsa   → SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM
#   rsa     → SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s
GH_ED25519 = (
    "github.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)
GH_ECDSA = (
    "github.com ecdsa-sha2-nistp256 "
    "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgw"
    "FB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg="
)
GH_RSA = (
    "github.com ssh-rsa "
    "AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNU"
    "kY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAO"
    "xSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScq"
    "b2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA1"
    "79cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4"
    "GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPU"
    "WZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/"
    "wsjk="
)

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("bash") is None,
    reason="ssh-keygen ou bash indisponibles",
)


def _run_verify(input_text: str) -> subprocess.CompletedProcess[str]:
    """Sourcé avec INSTALL_LXC_TESTING=1 : seule la définition des fonctions est chargée."""
    env = dict(os.environ)
    env["INSTALL_LXC_TESTING"] = "1"
    return subprocess.run(
        ["bash", "-c", f'. "{INSTALL_LXC}" && verify_github_keys'],
        input=input_text,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _stdout_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.strip()]


def _fake_pubkey_line(tmp_path: Path, tag: str) -> str:
    """Génère une paire ed25519 éphémère et renvoie une ligne format ssh-keyscan."""
    key = tmp_path / f"fake_{tag}"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q", "-C", tag],
        check=True,
        capture_output=True,
    )
    algo, blob = (key.with_suffix(".pub")).read_text().strip().split()[:2]
    # ssh-keyscan préfixe par le host — imitons github.com.
    return f"github.com {algo} {blob}"


# --- Cas 1 : les 3 clés valides -------------------------------------------------------


def test_verify_all_three_github_keys_pass() -> None:
    payload = "\n".join([GH_ED25519, GH_ECDSA, GH_RSA]) + "\n"
    result = _run_verify(payload)
    assert result.returncode == 0, result.stderr
    lines = _stdout_lines(result)
    assert set(lines) == {GH_ED25519, GH_ECDSA, GH_RSA}


# --- Cas 2 : deux clés valides + une invalide ------------------------------------------


def test_verify_two_valid_one_invalid(tmp_path: Path) -> None:
    fake = _fake_pubkey_line(tmp_path, "noise")
    payload = "\n".join([GH_ED25519, fake, GH_ECDSA]) + "\n"
    result = _run_verify(payload)
    assert result.returncode == 0, result.stderr
    lines = _stdout_lines(result)
    assert set(lines) == {GH_ED25519, GH_ECDSA}
    assert fake not in lines


# --- Cas 3 : une seule clé valide, deux invalides --------------------------------------


def test_verify_only_ed25519_survives(tmp_path: Path) -> None:
    fake1 = _fake_pubkey_line(tmp_path, "noise1")
    fake2 = _fake_pubkey_line(tmp_path, "noise2")
    payload = "\n".join([fake1, GH_ED25519, fake2]) + "\n"
    result = _run_verify(payload)
    assert result.returncode == 0, result.stderr
    lines = _stdout_lines(result)
    assert lines == [GH_ED25519]


# --- Cas 4 : aucune clé valide ---------------------------------------------------------


def test_verify_no_valid_keys_returns_nonzero(tmp_path: Path) -> None:
    fake1 = _fake_pubkey_line(tmp_path, "a")
    fake2 = _fake_pubkey_line(tmp_path, "b")
    fake3 = _fake_pubkey_line(tmp_path, "c")
    payload = "\n".join([fake1, fake2, fake3]) + "\n"
    result = _run_verify(payload)
    assert result.returncode != 0
    assert _stdout_lines(result) == []


# --- Bruit de ssh-keyscan : commentaires et lignes vides ignorés -----------------------


def test_verify_ignores_comments_and_blank_lines() -> None:
    payload = (
        "# github.com:22 SSH-2.0-babeld-abc\n"
        "\n"
        f"{GH_ED25519}\n"
        "# another comment\n"
        f"{GH_RSA}\n"
    )
    result = _run_verify(payload)
    assert result.returncode == 0, result.stderr
    lines = _stdout_lines(result)
    assert set(lines) == {GH_ED25519, GH_RSA}
