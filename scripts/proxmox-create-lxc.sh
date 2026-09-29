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
    # firewall=1 sur net0 active le pare-feu Proxmox pour ce NIC (le fichier
    # /etc/pve/firewall/CTID.fw écrit plus bas ne s'applique QUE si ce drapeau est
    # posé, en plus des cases datacenter/nœud/CT).
    pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$template" \
        --hostname "$CT_HOSTNAME" \
        --ostype debian \
        --unprivileged 1 \
        --features nesting=1 \
        --cores "$CORES" \
        --memory "$MEMORY_MB" \
        --swap "$SWAP_MB" \
        --rootfs "$STORAGE:$DISK_GB" \
        --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp,firewall=1" \
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
    WITH_VAULT="${WITH_VAULT:-1}" WITH_NOTEBOOKLM="${WITH_NOTEBOOKLM:-1}" \
    bash /root/install-lxc.sh

# --- Pare-feu Proxmox par conteneur -------------------------------------------------
# On écrit /etc/pve/firewall/<CTID>.fw : sortie fermée par défaut, DNS d'abord, puis
# DROP journalisé vers le LAN, puis ACCEPT vers l'Internet (443, 22, 80, ICMP).
# On active aussi le drapeau firewall=1 sur net0 (indispensable pour que le .fw
# s'applique) et on pose enable: 1 dans host.fw pour ce nœud. cluster.fw (datacenter)
# reste manuel — l'avertissement en fin de script rappelle les règles IN ACCEPT
# vers 8006/22 à mettre AVANT d'activer le firewall du datacenter.
FW_DIR="/etc/pve/firewall"
FW_FILE="$FW_DIR/${CTID}.fw"
HOST_FW="$FW_DIR/host.fw"
if [[ ! -d "$FW_DIR" ]]; then
    mkdir -p "$FW_DIR"
fi

# Idempotence : si le CTID existait déjà avec un net0 sans firewall=1, on l'ajoute
# sans perdre l'existant (bridge, IP fixe éventuelle, etc.).
if ! pct config "$CTID" | grep -qE '^net0:.*(\s|,)firewall=1(\s|,|$)'; then
    net0_cur="$(pct config "$CTID" | awk -F': ' '/^net0:/ { print $2 }')"
    if [[ -n "$net0_cur" ]]; then
        log "net0 : ajout du drapeau firewall=1 (préserve : $net0_cur)"
        pct set "$CTID" --net0 "${net0_cur},firewall=1"
    fi
fi

log "Pare-feu Proxmox : $FW_FILE"
cat >"$FW_FILE" <<'FW'
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: DROP
log_level_in: info
log_level_out: info

[RULES]
# DNS d'abord (les résolutions DOIVENT passer avant les DROP LAN qui suivent).
OUT ACCEPT -p udp -dport 53 -log nolog # DNS UDP
OUT ACCEPT -p tcp -dport 53 -log nolog # DNS TCP (résolution de gros paquets)

# DROP explicite et journalisé vers le LAN (RFC 1918) : le conteneur ne doit pas
# scanner ni atteindre l'hôte Proxmox, les NAS, les autres LXC, etc.
OUT DROP -dest 192.168.0.0/16 -log info # LAN /16
OUT DROP -dest 10.0.0.0/8 -log info     # LAN /8
OUT DROP -dest 172.16.0.0/12 -log info  # LAN /12 (docker default incl.)

# Sortie Internet autorisée.
OUT ACCEPT -p tcp -dport 443 -log nolog # HTTPS (api.telegram.org, api.anthropic.com, github.com, notebooklm.google.com)
OUT ACCEPT -p tcp -dport 22 -log nolog  # git@github.com (vault sync)
OUT ACCEPT -p tcp -dport 80 -log nolog  # apt (deb.debian.org), redirections HTTP
OUT ACCEPT -p icmp -log nolog           # ping/MTU discovery
FW
chmod 640 "$FW_FILE" 2>/dev/null || true # /etc/pve est un pmxcfs FUSE, ignorer les perms si refusées

# host.fw : active le pare-feu au niveau du nœud (une des trois cases à cocher).
# Idempotent : on ne clobber pas un host.fw existant qui aurait déjà d'autres règles.
if [[ ! -f "$HOST_FW" ]]; then
    log "Pare-feu Proxmox : $HOST_FW (enable: 1)"
    cat >"$HOST_FW" <<'HOSTFW'
[OPTIONS]
enable: 1
HOSTFW
    chmod 640 "$HOST_FW" 2>/dev/null || true
elif ! grep -qE '^enable:\s*1' "$HOST_FW"; then
    log "Attention : $HOST_FW existe SANS « enable: 1 » — l'ajouter à la main."
fi

ip="$(pct exec "$CTID" -- hostname -I | awk '{ print $1 }')"
log "Conteneur $CTID prêt : ${ip:-IP inconnue}. Connectez-vous : ssh root@${ip:-<ip>} (ou pct enter $CTID)"

printf '\n\033[1;33m'
cat <<'WARN'
╔══════════════════════════════════════════════════════════════════════════╗
║  Pare-feu conteneur, appliqué par ce script :                            ║
║    • /etc/pve/firewall/CTID.fw écrit (policy DROP + règles GUETTEUR)     ║
║    • net0 : drapeau firewall=1 posé (pct set --net0 …,firewall=1)        ║
║    • /etc/pve/firewall/host.fw écrit avec « enable: 1 » (nœud)           ║
║                                                                          ║
║  Reste MANUEL dans Datacenter → Firewall :                               ║
║    • « Firewall = Yes » (cluster.fw), qui active TOUT le pare-feu        ║
║                                                                          ║
║  AVANT de cocher « Firewall = Yes » au niveau datacenter, cluster.fw     ║
║  DOIT contenir des règles IN ACCEPT depuis vos sous-réseaux d'admin,     ║
║  sinon vous perdez l'accès à Proxmox et à SSH :                          ║
║                                                                          ║
║    Datacenter → Firewall → Add :                                         ║
║      Direction=in  Action=ACCEPT  Source=<votre-LAN>/24  Dport=8006      ║
║      Direction=in  Action=ACCEPT  Source=<votre-LAN>/24  Dport=22        ║
║                                                                          ║
║  Une fois activé : bash scripts/firewall-check.sh CTID  (depuis l'hôte)  ║
╚══════════════════════════════════════════════════════════════════════════╝
WARN
printf '\033[0m\n'
