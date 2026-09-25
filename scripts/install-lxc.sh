#!/usr/bin/env bash
# Installation de GUETTEUR sans Docker sur Debian 12 (dans le conteneur LXC, en root).
#
#   bash install-lxc.sh
#
# Lancé automatiquement par proxmox-create-lxc.sh. Idempotent : chaque outil n'est installé
# que s'il manque, le dépôt est mis à jour s'il est déjà cloné.
#
# Variables facultatives : REPO_URL (sinon git@github.com:polpod/guetteur.git, puis HTTPS),
# INSTALL_DIR (/opt/guetteur), SERVICE_USER (guetteur), WITH_WHISPER=1, UPDATE_CLAUDE=1.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/guetteur}"
SERVICE_USER="${SERVICE_USER:-guetteur}"
REPO_SSH="git@github.com:polpod/guetteur.git"
REPO_HTTPS="https://github.com/polpod/guetteur.git"
REPO_URL="${REPO_URL:-}"
WITH_WHISPER="${WITH_WHISPER:-0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS=(guetteur.service guetteur-health.service guetteur-health.timer)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '    \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mErreur :\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer en root"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "debian" ]] || log "Attention : prévu pour Debian 12, système : ${PRETTY_NAME:-?}"
export DEBIAN_FRONTEND=noninteractive

log "Paquets système"
missing=()
for pkg in ca-certificates curl git ffmpeg sudo gnupg; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if ((${#missing[@]})); then
    apt-get update -q
    apt-get install -y -q --no-install-recommends "${missing[@]}"
fi
ok "git $(git --version | awk '{ print $3 }'), curl, ffmpeg"

log "uv (script officiel)"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
ok "$(uv --version)"

log "Node.js 22 (NodeSource)"
if ! node --version 2>/dev/null | grep -q '^v22\.'; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -q nodejs
fi
ok "node $(node --version)"

log "Claude Code (@anthropic-ai/claude-code, global)"
if ! command -v claude >/dev/null 2>&1 || [[ "${UPDATE_CLAUDE:-0}" == "1" ]]; then
    npm install -g --no-fund --no-audit @anthropic-ai/claude-code
fi
ok "$(claude --version)"

log "Utilisateur système « $SERVICE_USER »"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    # Un vrai HOME : la session Claude Code est stockée dans ~/.claude.
    useradd --system --create-home --home-dir "/home/$SERVICE_USER" \
        --shell /bin/bash "$SERVICE_USER"
fi
ok "$(id "$SERVICE_USER")"

log "Code dans $INSTALL_DIR"
export GIT_TERMINAL_PROMPT=0 # jamais de demande interactive d'identifiants
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
gitc() { git -c safe.directory="$INSTALL_DIR" "$@"; }
copy_local_sources() {
    tar -C "$SRC_DIR" --exclude=.venv --exclude=data --exclude=.env \
        --exclude='*/__pycache__' --exclude=.mypy_cache --exclude=.ruff_cache \
        --exclude=.pytest_cache -cf - . | tar -C "$INSTALL_DIR" -xf -
}
if [[ -d "$INSTALL_DIR/.git" ]]; then
    if gitc -C "$INSTALL_DIR" pull --ff-only --quiet; then
        ok "dépôt mis à jour ($(gitc -C "$INSTALL_DIR" rev-parse --short HEAD))"
    elif [[ -f "$SRC_DIR/pyproject.toml" && "$SRC_DIR" != "$INSTALL_DIR" ]]; then
        log "git pull impossible (dépôt injoignable) : copie de $SRC_DIR"
        copy_local_sources
    else
        # Réinstallation sans accès au dépôt : on garde le code en place et on continue.
        log "git pull impossible (dépôt injoignable) : code actuel conservé"
    fi
else
    # Clonage dans un dossier temporaire : un échec ne touche jamais $INSTALL_DIR (qui peut
    # déjà contenir .env et data/ d'une installation par copie).
    tmp_clone="$(mktemp -d)"
    trap 'rm -rf "$tmp_clone"' EXIT
    cloned=""
    for url in ${REPO_URL:+"$REPO_URL"} "$REPO_SSH" "$REPO_HTTPS"; do
        if git clone --quiet "$url" "$tmp_clone/repo" 2>/dev/null; then
            cloned="$url"
            break
        fi
        log "Clonage impossible depuis $url, essai suivant"
        rm -rf "$tmp_clone/repo"
    done
    if [[ -n "$cloned" ]]; then
        mkdir -p "$INSTALL_DIR"
        cp -a "$tmp_clone/repo/." "$INSTALL_DIR/"
        ok "cloné depuis $cloned"
    elif [[ -f "$SRC_DIR/pyproject.toml" && "$SRC_DIR" != "$INSTALL_DIR" ]]; then
        log "Dépôt injoignable : copie de $SRC_DIR"
        mkdir -p "$INSTALL_DIR"
        copy_local_sources
    else
        die "impossible de cloner le dépôt. Dépôt privé ? Ajoutez une clé de déploiement \
(ssh-keygen -t ed25519 puis GitHub → Settings → Deploy keys) ou passez REPO_URL."
    fi
fi
mkdir -p "$INSTALL_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod -R go-w "$INSTALL_DIR" # une copie depuis NTFS arrive en 777

log "Python 3.12 et dépendances (uv sync --no-dev)"
extra=()
[[ "$WITH_WHISPER" == "1" ]] && extra=(--extra whisper)
sudo -u "$SERVICE_USER" -H bash -c 'cd "$1" && shift && uv python install 3.12 \
    && uv sync --frozen --no-dev --python 3.12 "$@"' _ "$INSTALL_DIR" "${extra[@]}"
ok "environnement prêt ($INSTALL_DIR/.venv)"

log "Commande « guetteur » (/usr/local/bin/guetteur)"
cat >/usr/local/bin/guetteur <<WRAPPER
#!/bin/sh
# Lance la CLI GUETTEUR depuis $INSTALL_DIR, sous l'utilisateur $SERVICE_USER.
cd "$INSTALL_DIR" || exit 1
if [ "\$(id -un)" = "$SERVICE_USER" ]; then
    exec /usr/local/bin/uv run --no-sync guetteur "\$@"
fi
exec sudo -u "$SERVICE_USER" -H /usr/local/bin/uv run --no-sync guetteur "\$@"
WRAPPER
chmod 755 /usr/local/bin/guetteur
ok "guetteur status | doctor | health | retry | reset…"

log "Unités systemd"
for unit in "${UNITS[@]}"; do
    install -m 644 "$INSTALL_DIR/deploy/$unit" "/etc/systemd/system/$unit"
    sed -i "s#/opt/guetteur#$INSTALL_DIR#g; s#^User=.*#User=$SERVICE_USER#; \
s#^Group=.*#Group=$SERVICE_USER#" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

env_ready=0
session_ready=0
[[ -s "$INSTALL_DIR/.env" ]] && env_ready=1
if [[ -s "/home/$SERVICE_USER/.claude/.credentials.json" ]] \
    || grep -qs '^CLAUDE_CODE_OAUTH_TOKEN=.' "$INSTALL_DIR/.env"; then
    session_ready=1
fi
if ((env_ready && session_ready)); then
    systemctl enable guetteur.service guetteur-health.timer >/dev/null
    systemctl restart guetteur.service guetteur-health.timer
    ok "service activé et (re)démarré"
else
    # Désactivé tant que .env et la session Claude ne sont pas en place.
    systemctl disable guetteur.service guetteur-health.timer >/dev/null 2>&1 || true
    ok "unités installées, désactivées (.env : $env_ready, session claude : $session_ready)"
fi

cat <<NEXT

Installation terminée. Il reste 4 commandes à lancer dans le conteneur :

  1. install -m 600 -o $SERVICE_USER -g $SERVICE_USER /root/.env $INSTALL_DIR/.env
       (après : scp .env root@<ip-du-lxc>:/root/.env depuis votre PC)
  2. sudo -u $SERVICE_USER -i claude auth login
  3. guetteur doctor
  4. systemctl enable --now guetteur

NEXT
