#!/usr/bin/env bash
# À lancer sur l'HÔTE Proxmox (root). Crée un LXC Debian 12 non privilégié pour GUETTEUR
# (2 vCPU, 2 Go de RAM, disque 16 Go, DHCP), le démarre, puis y exécute install-lxc.sh.
#
#   bash scripts/proxmox-create-lxc.sh <CTID> [STORAGE] [BRIDGE]
#   ex. : bash scripts/proxmox-create-lxc.sh 120 local-lvm vmbr0
#
# Idempotent : si le CTID existe déjà (et porte bien le nom d'hôte attendu), le conteneur
# n'est pas recréé ; il est démarré si besoin et install-lxc.sh est relancé (mise à jour).
#
# Variables facultatives : CT_HOSTNAME (guetteur), TEMPLATE_STORAGE (local),
# REPO_URL (dépôt à cloner), WITH_WHISPER=1, FORCE=1 (accepter un CTID au nom différent).
set -euo pipefail

CTID="${1:-}"
STORAGE="${2:-local-lvm}"
BRIDGE="${3:-vmbr0}"
CT_HOSTNAME="${CT_HOSTNAME:-guetteur}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
CORES=2
MEMORY_MB=2048
SWAP_MB=512
DISK_GB=16
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SCRIPT="$SCRIPT_DIR/install-lxc.sh"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mErreur :\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$CTID" && "$CTID" =~ ^[0-9]+$ ]] || die "usage : $0 <CTID> [STORAGE] [BRIDGE]"
[[ $EUID -eq 0 ]] || die "à lancer en root sur l'hôte Proxmox"
command -v pct >/dev/null 2>&1 || die "pct introuvable : ce script tourne sur l'hôte Proxmox"
[[ -f "$INSTALL_SCRIPT" ]] || die "install-lxc.sh introuvable à côté de ce script ($INSTALL_SCRIPT)"

latest_template() {
    pveam available --section system \
        | awk '$2 ~ /^debian-12-standard_.*_amd64\.tar\.zst$/ { print $2 }' \
        | sort -V | tail -n 1
}

if pct status "$CTID" >/dev/null 2>&1; then
    current="$(pct config "$CTID" | awk '/^hostname:/ { print $2 }')"
    if [[ "$current" != "$CT_HOSTNAME" && "${FORCE:-0}" != "1" ]]; then
        die "le CTID $CTID existe déjà avec le nom « $current » (attendu : $CT_HOSTNAME). \
Choisissez un autre CTID, ou FORCE=1 pour l'utiliser quand même."
    fi
    log "Le conteneur $CTID ($current) existe déjà : pas de recréation"
else
    log "Modèle Debian 12"
    pveam update >/dev/null
    template="$(latest_template)"
    [[ -n "$template" ]] || die "aucun modèle debian-12-standard disponible (pveam available)"
    if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$template"; then
        log "Téléchargement de $template dans $TEMPLATE_STORAGE"
        pveam download "$TEMPLATE_STORAGE" "$template"
    fi

    log "Création du conteneur $CTID ($CT_HOSTNAME) sur $STORAGE, pont $BRIDGE"
    pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$template" \
        --hostname "$CT_HOSTNAME" \
        --ostype debian \
        --unprivileged 1 \
        --features nesting=1 \
        --cores "$CORES" \
        --memory "$MEMORY_MB" \
        --swap "$SWAP_MB" \
        --rootfs "$STORAGE:$DISK_GB" \
        --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
        --onboot 1 \
        --description "GUETTEUR : veille YouTube résumée par Claude"
fi

if [[ "$(pct status "$CTID" | awk '{ print $2 }')" != "running" ]]; then
    log "Démarrage du conteneur $CTID"
    pct start "$CTID"
fi

log "Attente du réseau (DHCP + DNS)"
for _ in $(seq 1 30); do
    if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 \
    || die "le conteneur $CTID n'a pas de réseau (DHCP sur $BRIDGE ?)"

log "Installation de GUETTEUR dans le conteneur"
pct push "$CTID" "$INSTALL_SCRIPT" /root/install-lxc.sh --perms 0755
pct exec "$CTID" -- env \
    REPO_URL="${REPO_URL:-}" WITH_WHISPER="${WITH_WHISPER:-0}" \
    bash /root/install-lxc.sh

ip="$(pct exec "$CTID" -- hostname -I | awk '{ print $1 }')"
log "Conteneur $CTID prêt : ${ip:-IP inconnue}. Connectez-vous : ssh root@${ip:-<ip>} (ou pct enter $CTID)"
