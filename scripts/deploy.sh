#!/usr/bin/env bash
# Mise à jour de GUETTEUR dans le conteneur LXC (en root) : git pull, uv sync --no-dev
# avec les mêmes extras qu'à l'install (notebooklm toujours ; whisper si config le dit),
# unités systemd à jour, redémarrage du service, guetteur doctor, 30 lignes de journal.
#
#   bash /opt/guetteur/scripts/deploy.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/guetteur}"
SERVICE_USER="${SERVICE_USER:-guetteur}"
UV_BIN="/usr/local/bin/uv"
UNITS=(
    guetteur.service
    guetteur-health.service
    guetteur-health.timer
    guetteur-nlm-refresh.service
    guetteur-nlm-refresh.timer
)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mErreur :\033[0m %s\n' "$*" >&2; exit 1; }

# Renvoie 0 si config.toml déclare [transcript] whisper_enabled = true.
whisper_wanted() {
    local cfg="$1"
    [[ -f "$cfg" ]] || return 1
    awk '
        /^[[:space:]]*\[/ { section = $0 }
        section ~ /^\[transcript\]/ \
            && /^[[:space:]]*whisper_enabled[[:space:]]*=[[:space:]]*true([[:space:]]|$|#)/ \
            { found = 1 }
        END { exit !found }
    ' "$cfg"
}

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

# --extra notebooklm est TOUJOURS ajouté : sans lui, uv sync désinstalle
# notebooklm-py du venv et l'archivage casse au prochain cycle (bug prod).
# --extra whisper est plus lourd : on l'ajoute seulement si config.toml le
# demande, pour ne pas gonfler le venv sur les instances qui n'en veulent pas.
extras=(--extra notebooklm)
if whisper_wanted "$INSTALL_DIR/config.toml"; then
    extras+=(--extra whisper)
fi
log "uv sync --no-dev ${extras[*]}"
sudo -u "$SERVICE_USER" -H bash -c '
    cd "$1" && shift
    "'"$UV_BIN"'" sync --frozen --no-dev "$@"
' _ "$INSTALL_DIR" "${extras[@]}"

reload=0
for unit in "${UNITS[@]}"; do
    src="$INSTALL_DIR/deploy/$unit"
    dst="/etc/systemd/system/$unit"
    [[ -f "$src" ]] || continue
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

# guetteur doctor en fin de déploiement : si un extra a disparu, si la session
# claude est expirée, si le vault n'est plus joignable, on veut le savoir tout
# de suite — pas au prochain cycle (qui pourrait passer une vidéo en `failed`).
log "guetteur doctor"
if ! /usr/local/bin/guetteur doctor; then
    die "guetteur doctor : au moins une ligne KO — service redémarré mais l'état est dégradé."
fi
