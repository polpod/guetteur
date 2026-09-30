#!/usr/bin/env bash
# Mise à jour de GUETTEUR dans le conteneur LXC (en root) : git pull, uv sync --no-dev
# avec les mêmes extras qu'à l'install (notebooklm toujours ; whisper si config le dit),
# unités systemd à jour, redémarrage du service, guetteur doctor, 30 lignes de journal.
#
# git et uv tournent sous « guetteur » (sudo -u -H env), jamais sous root :
# /opt/guetteur appartient à guetteur, donc `git pull` en root déclenche « dubious
# ownership » (safe.directory) et un chown de rattrapage laisse .git/index en
# root:root au prochain cycle. config.toml est marqué `skip-worktree` après le
# premier pull pour que les adaptations locales survivent aux mises à jour.
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

# Toutes les commandes git et uv passent par ce helper : sudo réinitialise
# l'environnement (même avec -H), on repose donc explicitement les variables
# git indispensables plutôt que d'espérer -E.
as_service() {
    sudo -u "$SERVICE_USER" -H env \
        GIT_TERMINAL_PROMPT=0 \
        GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
        "$@"
}
gitc() { as_service git -C "$INSTALL_DIR" "$@"; }

log "git pull (sous $SERVICE_USER)"
before="$(gitc rev-parse --short HEAD)"
gitc pull --ff-only
after="$(gitc rev-parse --short HEAD)"
log "$before → $after"

# config.toml est marqué skip-worktree APRÈS le premier pull : sinon un fichier
# local adapté (clés LXC, chemins, poll_interval…) fait échouer `git pull` avec
# « would be overwritten by merge ». Idempotent : `ls-files -v` préfixe la ligne
# par « S » quand le drapeau est déjà posé, on ne repose rien dans ce cas.
# Les nouvelles clés ajoutées côté upstream n'arrivent plus automatiquement —
# le diff affiché en fin de script les rend visibles pour recopie manuelle.
if [[ -f "$INSTALL_DIR/config.toml" ]]; then
    flag="$(gitc ls-files -v -- config.toml | awk '{ print substr($0, 1, 1); exit }')"
    if [[ "$flag" != "S" ]]; then
        gitc update-index --skip-worktree config.toml
        log "config.toml marqué skip-worktree : les adaptations locales survivront aux pull"
    fi
fi

# --extra notebooklm est TOUJOURS ajouté : sans lui, uv sync désinstalle
# notebooklm-py du venv et l'archivage casse au prochain cycle (bug prod).
# --extra whisper est plus lourd : on l'ajoute seulement si config.toml le
# demande, pour ne pas gonfler le venv sur les instances qui n'en veulent pas.
extras=(--extra notebooklm)
if whisper_wanted "$INSTALL_DIR/config.toml"; then
    extras+=(--extra whisper)
fi
log "uv sync --no-dev ${extras[*]}"
as_service bash -c '
    cd "$1" && shift
    exec "'"$UV_BIN"'" sync --frozen --no-dev "$@"
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

# Nouvelles clés apparues côté upstream : config.toml est skip-worktree, donc
# `git pull` ne peut plus les faire remonter. On affiche le diff pour que
# l'admin les recopie à la main dans le config.toml local si besoin.
example="$INSTALL_DIR/config.toml.lxc.example"
local_cfg="$INSTALL_DIR/config.toml"
if [[ -f "$example" && -f "$local_cfg" ]] && ! diff -q "$example" "$local_cfg" >/dev/null 2>&1; then
    log "config.toml.lxc.example ↔ config.toml : diff ci-dessous"
    printf '    \033[2m(config.toml est skip-worktree ; recopier à la main les clés voulues)\033[0m\n'
    diff -u "$example" "$local_cfg" || true
fi
