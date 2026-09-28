#!/usr/bin/env bash
# À lancer sur l'HÔTE Proxmox (root) une fois le pare-feu activé au niveau
# Datacenter + Nœud + Conteneur.
#
#   bash scripts/firewall-check.sh <CTID>
#
# Depuis le conteneur, vérifie :
#   - api.telegram.org (HTTPS)        : OK attendu (Internet ouvert)
#   - github.com (SSH port 22)        : OK attendu (vault sync)
#   - <passerelle> (LAN)              : KO attendu (DROP LAN)
#   - <hôte Proxmox>:8006             : KO attendu (DROP LAN)
#   - DNS (getent hosts)              : OK attendu (règle DNS-first)
#
# Affiche OK/KO ligne par ligne, code de sortie 0 si toutes les attentes sont
# tenues, 1 sinon (utile en CI ou en post-install).
set -euo pipefail

CTID="${1:-}"
[[ -n "$CTID" && "$CTID" =~ ^[0-9]+$ ]] || { echo "Usage : $0 <CTID>" >&2; exit 2; }
command -v pct >/dev/null 2>&1 || { echo "pct introuvable : à lancer sur l'hôte Proxmox." >&2; exit 2; }
pct status "$CTID" >/dev/null 2>&1 || { echo "Conteneur $CTID inconnu." >&2; exit 2; }

# Passerelle et hôte Proxmox, vus depuis le conteneur.
gw="$(pct exec "$CTID" -- sh -c "ip route | awk '/^default/ { print \$3 }'" 2>/dev/null | head -1)"
host_ip="$(hostname -I | awk '{ print $1 }')"

failed=0
row() { # nom  attente  ok(0/1)   -> ligne OK/KO alignée
    local name="$1" expected="$2" got_ok="$3" want_ok
    [[ "$expected" == "OK" ]] && want_ok=1 || want_ok=0
    if [[ "$got_ok" == "$want_ok" ]]; then
        printf '  \033[32m%-3s\033[0m  %-38s (attendu %s)\n' "OK " "$name" "$expected"
    else
        printf '  \033[31m%-3s\033[0m  %-38s (attendu %s)\n' "KO " "$name" "$expected"
        failed=1
    fi
}

# 1. DNS : getent hosts (utilise le resolver, donc UDP/TCP 53).
if pct exec "$CTID" -- getent hosts api.telegram.org >/dev/null 2>&1; then
    row "DNS → api.telegram.org"       OK 1
else
    row "DNS → api.telegram.org"       OK 0
fi

# 2. HTTPS sortant vers l'Internet — timeout court pour ne pas bloquer si DROP.
if pct exec "$CTID" -- \
    timeout 5 bash -c "exec 3<>/dev/tcp/api.telegram.org/443 && echo hi >&3 && exec 3<&-" \
    >/dev/null 2>&1; then
    row "TCP → api.telegram.org:443"   OK 1
else
    row "TCP → api.telegram.org:443"   OK 0
fi

# 3. SSH sortant vers github.com (vault sync).
if pct exec "$CTID" -- \
    timeout 5 bash -c "exec 3<>/dev/tcp/github.com/22 && echo hi >&3 && exec 3<&-" \
    >/dev/null 2>&1; then
    row "TCP → github.com:22"          OK 1
else
    row "TCP → github.com:22"          OK 0
fi

# 4. Passerelle LAN — attendu KO (DROP LAN).
if [[ -n "$gw" ]]; then
    if pct exec "$CTID" -- timeout 3 ping -c 1 -W 2 "$gw" >/dev/null 2>&1; then
        row "ping → passerelle ($gw)"  KO 1
    else
        row "ping → passerelle ($gw)"  KO 0
    fi
else
    printf '  \033[33m??\033[0m   passerelle inconnue (ip route vide)\n'
fi

# 5. Hôte Proxmox:8006 — attendu KO (DROP LAN).
if [[ -n "$host_ip" ]]; then
    if pct exec "$CTID" -- \
        timeout 3 bash -c "exec 3<>/dev/tcp/$host_ip/8006 && echo hi >&3 && exec 3<&-" \
        >/dev/null 2>&1; then
        row "TCP → hôte:$host_ip:8006"     KO 1
    else
        row "TCP → hôte:$host_ip:8006"     KO 0
    fi
else
    printf '  \033[33m??\033[0m   IP hôte inconnue (hostname -I vide)\n'
fi

if ((failed)); then
    echo
    echo "→ Une ou plusieurs attentes ne sont pas tenues. Vérifiez que :"
    echo "  - Datacenter + Nœud + Conteneur ont Firewall = Yes ;"
    echo "  - le fichier /etc/pve/firewall/$CTID.fw est le nôtre ;"
    echo "  - une règle IN ACCEPT vers 8006/22 est en place AVANT l'activation datacenter."
    exit 1
fi
exit 0
