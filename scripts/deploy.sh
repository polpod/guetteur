#!/usr/bin/env bash
# Mise à jour de GUETTEUR dans le conteneur LXC (en root) : git pull, uv sync --no-dev,
# unités systemd à jour, redémarrage du service, 30 dernières lignes du journal.
#
#   bash /opt/guetteur/scripts/deploy.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/guetteur}"
SERVICE_USER="${SERVICE_USER:-guetteur}"
UNITS=(guetteur.service guetteur-health.service guetteur-health.timer)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mErreur :\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "à lancer en root"
[[ -d "$INSTALL_DIR/.git" ]] || die "$INSTALL_DIR n'est pas un dépôt git (install-lxc.sh ?)"

export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
gitc() { git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" "$@"; }

log "git pull"
before="$(gitc rev-parse --short HEAD)"
gitc pull --ff-only
after="$(gitc rev-parse --short HEAD)"
log "$before → $after"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

log "uv sync --no-dev"
sudo -u "$SERVICE_USER" -H bash -c 'cd "$1" && uv sync --frozen --no-dev' _ "$INSTALL_DIR"

reload=0
for unit in "${UNITS[@]}"; do
    src="$INSTALL_DIR/deploy/$unit"
    dst="/etc/systemd/system/$unit"
    tmp="$(mktemp)"
    sed "s#/opt/guetteur#$INSTALL_DIR#g; s#^User=.*#User=$SERVICE_USER#; \
s#^Group=.*#Group=$SERVICE_USER#" "$src" >"$tmp"
    if ! cmp -s "$tmp" "$dst"; then
        install -m 644 "$tmp" "$dst"
        log "unité mise à jour : $unit"
        reload=1
    fi
    rm -f "$tmp"
done
((reload)) && systemctl daemon-reload

log "systemctl restart guetteur"
systemctl restart guetteur.service
sleep 3
journalctl -u guetteur.service -n 30 --no-pager
