#!/usr/bin/env bash
# Installation de GUETTEUR sans Docker sur Debian 12 (conteneur LXC Proxmox).
#
#   sudo REPO_URL=https://github.com/vous/guetteur.git bash scripts/install-lxc.sh
#
# Sans REPO_URL, le dossier contenant ce script est copié dans /opt/guetteur.
# WITH_WHISPER=1 ajoute faster-whisper (secours de transcription).
# Relancer le script met à jour l'installation (git pull / copie, puis uv sync).
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/guetteur}"
SERVICE_USER="${SERVICE_USER:-guetteur}"
REPO_URL="${REPO_URL:-}"
WITH_WHISPER="${WITH_WHISPER:-0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mErreur :\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer en root (sudo bash scripts/install-lxc.sh)"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "debian" ]] || log "Attention : testé sur Debian 12, système détecté : ${PRETTY_NAME:-?}"

log "Paquets système (ffmpeg, git, curl, sqlite3…)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    ca-certificates curl git gnupg ffmpeg sqlite3 sudo

log "Node.js 22 (NodeSource)"
if ! node --version 2>/dev/null | grep -q '^v22\.'; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -q nodejs
fi
node --version

log "Claude Code (npm i -g @anthropic-ai/claude-code)"
npm install -g --no-fund --no-audit @anthropic-ai/claude-code
claude --version

log "uv (dans /usr/local/bin)"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv --version

log "Utilisateur système « $SERVICE_USER »"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    # Un vrai HOME est nécessaire : la session Claude Code est stockée dans ~/.claude.
    useradd --system --create-home --home-dir "/home/$SERVICE_USER" \
        --shell /bin/bash "$SERVICE_USER"
fi

log "Code dans $INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
    sudo -u "$SERVICE_USER" -H git -C "$INSTALL_DIR" pull --ff-only
elif [[ -n "$REPO_URL" ]]; then
    git clone "$REPO_URL" "$INSTALL_DIR"
elif [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
    mkdir -p "$INSTALL_DIR"
    tar -C "$SRC_DIR" --exclude=.venv --exclude=.git --exclude=data --exclude=.env \
        --exclude='*/__pycache__' --exclude=.mypy_cache --exclude=.ruff_cache \
        --exclude=.pytest_cache -cf - . | tar -C "$INSTALL_DIR" -xf -
fi
mkdir -p "$INSTALL_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

log "Python 3.12 (uv) et dépendances"
EXTRA=""
[[ "$WITH_WHISPER" == "1" ]] && EXTRA="--extra whisper"
sudo -u "$SERVICE_USER" -H bash -c "
    set -euo pipefail
    cd '$INSTALL_DIR'
    uv python install 3.12
    uv sync --frozen --no-dev --python 3.12 $EXTRA
"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    log "Création de $INSTALL_DIR/.env (à compléter)"
    install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
        "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi

log "Service systemd"
install -m 644 "$INSTALL_DIR/deploy/guetteur.service" /etc/systemd/system/guetteur.service
sed -i "s#/opt/guetteur#$INSTALL_DIR#g; s#^User=.*#User=$SERVICE_USER#; s#^Group=.*#Group=$SERVICE_USER#" \
    /etc/systemd/system/guetteur.service
systemctl daemon-reload
systemctl enable guetteur.service >/dev/null

cat <<NEXT

Installation terminée. Étapes suivantes :

  1. Connecter Claude Code pour l'utilisateur $SERVICE_USER (session SSH interactive) :
       sudo -u $SERVICE_USER -i claude auth login
  2. Renseigner les secrets et les playlists :
       nano $INSTALL_DIR/.env
       nano $INSTALL_DIR/config.toml
  3. Vérifier puis démarrer :
       cd $INSTALL_DIR && sudo -u $SERVICE_USER -H uv run --no-sync guetteur doctor
       systemctl start guetteur && journalctl -u guetteur -f
NEXT
