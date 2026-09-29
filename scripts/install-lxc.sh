#!/usr/bin/env bash
# Installation de GUETTEUR sans Docker sur Debian 12 (dans le conteneur LXC, en root).
#
#   bash install-lxc.sh
#
# Lancé automatiquement par proxmox-create-lxc.sh. Idempotent : chaque outil n'est installé
# que s'il manque, le dépôt est mis à jour s'il est déjà cloné, l'utilisateur, les locales
# et le vault sont posés une seule fois.
#
# Variables facultatives : REPO_URL (sinon git@github.com:polpod/guetteur.git, puis HTTPS),
# INSTALL_DIR (/opt/guetteur), SERVICE_USER (guetteur), WITH_WHISPER=1, WITH_NOTEBOOKLM=1,
# UPDATE_CLAUDE=1, WITH_VAULT=1 (clone du vault Obsidian dans data/vault),
# VAULT_REMOTE (git@github.com:polpod/vault-veille.git par défaut).
#
# Testing : `INSTALL_LXC_TESTING=1 . install-lxc.sh` charge les fonctions sans exécuter le
# corps principal (voir tests/test_install_lxc.py).
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/guetteur}"
SERVICE_USER="${SERVICE_USER:-guetteur}"
REPO_SSH="git@github.com:polpod/guetteur.git"
REPO_HTTPS="https://github.com/polpod/guetteur.git"
REPO_URL="${REPO_URL:-}"
WITH_WHISPER="${WITH_WHISPER:-0}"
WITH_NOTEBOOKLM="${WITH_NOTEBOOKLM:-1}" # extra epinglé, audité (voir audit §8-9)
WITH_VAULT="${WITH_VAULT:-1}"           # clone du vault Obsidian (deploy key GitHub)
VAULT_REMOTE="${VAULT_REMOTE:-git@github.com:polpod/vault-veille.git}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS=(
    guetteur.service
    guetteur-health.service
    guetteur-health.timer
    guetteur-nlm-refresh.service
    guetteur-nlm-refresh.timer
)
# Wheel notebooklm-py 0.8.3, sha256 audité (voir audit-notebooklm/RAPPORT.md §9).
NLM_VERSION="0.8.3"
NLM_HASH="sha256:7e3e02057b3acf354d3dbc337c08869d2a4954c9c324f3271e272236cfcc2bfc"

# uv est installé par le script officiel dans /usr/local/bin ; on n'utilise QUE le chemin
# absolu, jamais `uv` via PATH (sudo -u réinitialise PATH, le service systemd aussi).
UV_BIN="/usr/local/bin/uv"

# Empreintes SHA256 publiées par GitHub (source :
# https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
# À réviser si GitHub tourne ses clés — c'est la seule source d'autorité, jamais un
# `StrictHostKeyChecking=no` ni un `ssh-keyscan` sans vérification.
GITHUB_FINGERPRINTS="SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s
SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM
SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '    \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mErreur :\033[0m %s\n' "$*" >&2; exit 1; }

# Lit sur stdin la sortie de `ssh-keyscan -t ed25519,ecdsa,rsa github.com`, calcule
# l'empreinte SHA256 de chaque clé et n'écrit sur stdout que celles publiées par
# GitHub (GITHUB_FINGERPRINTS). Retourne 0 si au moins une clé a été validée, 1
# sinon — testé par tests/test_install_lxc.py.
verify_github_keys() {
    local validated=0 line fp tmp
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        printf '%s\n' "$line" >"$tmp"
        if ! fp="$(ssh-keygen -lf "$tmp" 2>/dev/null | awk '{ print $2 }')"; then
            continue
        fi
        [[ -z "$fp" ]] && continue
        if printf '%s\n' "$GITHUB_FINGERPRINTS" | grep -qxF "$fp"; then
            printf '%s\n' "$line"
            validated=$((validated + 1))
        fi
    done
    rm -f "$tmp"
    if ((validated > 0)); then
        return 0
    fi
    return 1
}

# Sourcé depuis les tests : on s'arrête ici, seule la définition des fonctions
# est chargée dans le shell appelant.
if [[ "${INSTALL_LXC_TESTING:-0}" == "1" ]]; then
    return 0 2>/dev/null || exit 0
fi

[[ $EUID -eq 0 ]] || die "à lancer en root"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "debian" ]] || log "Attention : prévu pour Debian 12, système : ${PRETTY_NAME:-?}"
export DEBIAN_FRONTEND=noninteractive

log "Paquets système"
missing=()
for pkg in ca-certificates curl git ffmpeg sudo gnupg locales openssh-client; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if ((${#missing[@]})); then
    apt-get update -q
    apt-get install -y -q --no-install-recommends "${missing[@]}"
fi
ok "git $(git --version | awk '{ print $3 }'), curl, ffmpeg, locales"

log "Locales fr_FR.UTF-8 et en_US.UTF-8"
# `locale -a` sort du fr_FR.utf8 ou fr_FR.UTF-8 selon les versions — on compare sans
# tiret ni casse via tr.
have_locale() {
    local target
    target="$(printf '%s' "$1" | tr -d '-' | tr '[:upper:]' '[:lower:]')"
    locale -a 2>/dev/null \
        | tr -d '-' \
        | tr '[:upper:]' '[:lower:]' \
        | grep -qxF "$target"
}
if ! have_locale "fr_FR.UTF-8" || ! have_locale "en_US.UTF-8"; then
    # Décommente les deux lignes dans /etc/locale.gen (idempotent : si déjà décommenté,
    # le sed est un no-op) puis regénère.
    sed -i -E \
        -e 's/^# *(fr_FR\.UTF-8 UTF-8)/\1/' \
        -e 's/^# *(en_US\.UTF-8 UTF-8)/\1/' \
        /etc/locale.gen
    locale-gen fr_FR.UTF-8 en_US.UTF-8 >/dev/null
    update-locale LANG=fr_FR.UTF-8 LANGUAGE=fr_FR:fr
fi
export LANG=fr_FR.UTF-8 LC_ALL=fr_FR.UTF-8
ok "locales fr_FR.UTF-8, en_US.UTF-8"

log "uv (script officiel)"
if [[ ! -x "$UV_BIN" ]]; then
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
# L'installeur peut silencieusement tomber en /root/.local/bin si UV_INSTALL_DIR est
# ignoré : on vérifie explicitement plutôt que d'afficher un ✓ vide (l'appel initial
# `command -v uv` réussirait à cause du hash bash, mais `sudo -u guetteur uv ...`
# planterait plus loin — cf. incident /usr/local/bin PATH).
[[ -x "$UV_BIN" ]] || die "uv absent après installation ($UV_BIN). PATH du service ?"
ok "$("$UV_BIN" --version)"

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

log "Chemins inscriptibles du service (ProtectHome=read-only)"
# Le service tourne avec ReadWritePaths=/opt/guetteur/data /home/guetteur/.claude
# /home/guetteur/.claude.json /home/guetteur/.cache — chaque chemin DOIT exister
# au démarrage sinon systemd refuse d'appliquer le bind-mount et l'unité échoue.
service_home="/home/$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$service_home/.claude"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$service_home/.cache"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$service_home/.local"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$service_home/.local/bin"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$INSTALL_DIR/data"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$INSTALL_DIR/data/.uv-cache"
# .claude.json : Claude Code y écrit son état ; ReadWritePaths ne peut lier qu'un
# chemin existant, on crée un JSON vide si le binaire n'est pas encore passé.
if [[ ! -e "$service_home/.claude.json" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 /dev/null "$service_home/.claude.json"
    printf '{}\n' >"$service_home/.claude.json"
    chown "$SERVICE_USER:$SERVICE_USER" "$service_home/.claude.json"
fi
ok "$service_home/.claude, .claude.json, .cache, $INSTALL_DIR/data/.uv-cache (owner $SERVICE_USER)"

if [[ "$WITH_VAULT" == "1" ]]; then
    log "Vault Obsidian ($VAULT_REMOTE)"
    ssh_dir="/home/$SERVICE_USER/.ssh"
    key_path="$ssh_dir/id_ed25519"
    known_hosts="$ssh_dir/known_hosts"
    vault_dir="$INSTALL_DIR/data/vault"

    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$ssh_dir"

    # 1. Clé ed25519 dédiée pour la deploy key (idempotent : ne régénère pas si présente).
    if [[ ! -f "$key_path" ]]; then
        sudo -u "$SERVICE_USER" -H ssh-keygen -t ed25519 -N "" \
            -C "guetteur@$(hostname)" -f "$key_path" -q
    fi
    chmod 0700 "$ssh_dir"
    chmod 0600 "$key_path"
    chmod 0644 "$key_path.pub"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$ssh_dir"

    # 2. known_hosts : récupère les clés courantes via ssh-keyscan puis n'écrit QUE
    #    celles dont l'empreinte SHA256 fait partie de la liste publiée par GitHub
    #    (au moins une match requise). Idempotent — on retire toute ancienne entrée
    #    github.com et on écrit les clés validées.
    gh_kh="$(mktemp)"
    gh_valid="$(mktemp)"
    # shellcheck disable=SC2064
    trap "rm -f '$gh_kh' '$gh_valid'" RETURN
    if ! ssh-keyscan -t ed25519,ecdsa,rsa -T 10 github.com >"$gh_kh" 2>/dev/null; then
        die "ssh-keyscan github.com échoué (réseau ? proxy ?)"
    fi
    if ! verify_github_keys <"$gh_kh" >"$gh_valid"; then
        die "aucune clé ssh-keyscan github.com ne correspond aux empreintes publiées \
(https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)."
    fi
    n_valid="$(grep -c . "$gh_valid" || true)"
    touch "$known_hosts"
    grep -v '^github\.com ' "$known_hosts" >"$known_hosts.tmp" || true
    cat "$gh_valid" >>"$known_hosts.tmp"
    mv "$known_hosts.tmp" "$known_hosts"
    chown "$SERVICE_USER:$SERVICE_USER" "$known_hosts"
    chmod 0600 "$known_hosts"
    rm -f "$gh_kh" "$gh_valid"
    trap - RETURN
    ok "known_hosts GitHub vérifiés ($n_valid empreinte(s) SHA256 sur 3 attendues)"

    # 3. Clone du vault. Si la clé n'est pas encore autorisée sur GitHub, on affiche
    #    la clé publique et on attend. En mode non interactif (pas de TTY), on n'attend
    #    pas : l'utilisateur relancera le script après ajout.
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$INSTALL_DIR/data"
    vault_git_ssh="ssh -o BatchMode=yes -i $key_path -o UserKnownHostsFile=$known_hosts \
-o StrictHostKeyChecking=yes"
    if [[ ! -d "$vault_dir/.git" ]]; then
        # Tester d'abord l'accès (silencieux) : évite d'afficher la clé si la deploy
        # key est déjà en place (relance d'install).
        if ! sudo -u "$SERVICE_USER" -H env GIT_SSH_COMMAND="$vault_git_ssh" \
            git ls-remote --exit-code "$VAULT_REMOTE" HEAD >/dev/null 2>&1; then
            printf '\n\033[1;33m────────────────────────────────────────────────────────────\033[0m\n'
            printf 'Clé publique du conteneur — à ajouter comme \033[1mdeploy key\033[0m\n'
            printf 'AVEC \033[1maccès en écriture\033[0m sur %s :\n\n' "$VAULT_REMOTE"
            cat "$key_path.pub"
            printf '\nOuvrez : https://github.com/polpod/vault-veille/settings/keys/new\n'
            printf '    - Title : guetteur (%s)\n' "$(hostname)"
            printf '    - Key   : coller la clé ci-dessus\n'
            printf '    - Cocher « Allow write access »\n'
            printf '\033[1;33m────────────────────────────────────────────────────────────\033[0m\n\n'
            if [[ -t 0 ]] || [[ -r /dev/tty ]]; then
                printf 'Appuyez sur ENTRÉE une fois la clé ajoutée... '
                if [[ -t 0 ]]; then
                    read -r _
                else
                    read -r _ </dev/tty
                fi
            else
                log "Pas de terminal : arrêt. Ajoutez la deploy key puis relancez ce script."
                exit 0
            fi
        fi
        sudo -u "$SERVICE_USER" -H env GIT_SSH_COMMAND="$vault_git_ssh" \
            git clone --quiet "$VAULT_REMOTE" "$vault_dir" \
            || die "clone du vault échoué. Deploy key en écriture ajoutée sur $VAULT_REMOTE ?"
    else
        # Idempotent : pull ff-only, sans échouer si le remote n'est pas joignable.
        sudo -u "$SERVICE_USER" -H env GIT_SSH_COMMAND="$vault_git_ssh" \
            git -C "$vault_dir" pull --ff-only --quiet \
            || log "vault : git pull impossible (remote injoignable), clone local conservé"
    fi

    # 4. user.name / user.email dans ce clone (jamais en global : le service peut
    #    signer des commits GUETTEUR sans polluer la config globale de l'utilisateur).
    sudo -u "$SERVICE_USER" -H git -C "$vault_dir" config user.name "GUETTEUR"
    sudo -u "$SERVICE_USER" -H git -C "$vault_dir" config user.email "guetteur@localhost"
    ok "vault $vault_dir prêt ($(sudo -u "$SERVICE_USER" -H \
        git -C "$vault_dir" rev-parse --short HEAD 2>/dev/null || echo 'branche vide'))"
fi

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
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_clone'" EXIT
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
[[ "$WITH_WHISPER" == "1" ]] && extra+=(--extra whisper)
[[ "$WITH_NOTEBOOKLM" == "1" ]] && extra+=(--extra notebooklm)
# On passe le chemin absolu de uv dans le sub-shell : sudo -u réinitialise PATH,
# et l'utilisateur guetteur n'a pas /usr/local/bin dans son login PATH par défaut.
sudo -u "$SERVICE_USER" -H bash -c '
    cd "$1" && shift
    "'"$UV_BIN"'" python install 3.12
    "'"$UV_BIN"'" sync --frozen --no-dev --python 3.12 "$@"
' _ "$INSTALL_DIR" "${extra[@]}"
ok "environnement prêt ($INSTALL_DIR/.venv)"

if [[ "$WITH_NOTEBOOKLM" == "1" ]]; then
    log "notebooklm-py $NLM_VERSION (uv tool sous $SERVICE_USER, wheel épinglé par hash)"
    # Contraintes uv : version + hash sha256 audité. Wheel seulement (--no-sources
    # empêche toute source-dist qui exécuterait le hook de build).
    nlm_constraints="$(mktemp)"
    # shellcheck disable=SC2064
    trap "rm -f '$nlm_constraints'" EXIT
    cat >"$nlm_constraints" <<EOF
notebooklm-py==${NLM_VERSION} --hash=${NLM_HASH}
EOF
    # Le fichier de contraintes est en 0600 root:root après mktemp — le sub-shell
    # sudo -u ne peut pas le lire. On l'ouvre en 0644 (contenu public : version + sha).
    chmod 0644 "$nlm_constraints"
    # Installation SOUS guetteur : sinon /usr/local/bin/notebooklm pointerait dans
    # /root/.local/share/uv/tools/, inaccessible au service (203/EXEC + ProtectHome).
    # UV_CACHE_DIR dédié pour ne pas cracher dans /home/guetteur/.cache (que le
    # service voit en lecture seule à travers ProtectHome=read-only).
    if ! sudo -u "$SERVICE_USER" -H env UV_CACHE_DIR="$INSTALL_DIR/data/.uv-cache" \
        "$UV_BIN" tool install --force --constraints "$nlm_constraints" \
        "notebooklm-py==${NLM_VERSION}" >/dev/null; then
        die "installation de notebooklm-py $NLM_VERSION en échec (hash sha256 ?)."
    fi
    # uv tool install place l'entry point dans ~/.local/bin ; on symlink pour le
    # PATH du service (qui ne connaît que /usr/local/bin:/usr/bin:/bin).
    nlm_bin="/home/$SERVICE_USER/.local/bin/notebooklm"
    [[ -x "$nlm_bin" ]] || die "notebooklm introuvable après uv tool install ($nlm_bin)"
    ln -sf "$nlm_bin" /usr/local/bin/notebooklm
    ok "$(sudo -u "$SERVICE_USER" -H /usr/local/bin/notebooklm --version 2>&1 | head -1)"
    # NOTEBOOKLM_HOME dédié, 0700, possédé par guetteur.
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$INSTALL_DIR/data/nlm"
    ok "$INSTALL_DIR/data/nlm (0700, $SERVICE_USER)"
fi

log "Commande « guetteur » (/usr/local/bin/guetteur)"
cat >/usr/local/bin/guetteur <<WRAPPER
#!/bin/sh
# Lance la CLI GUETTEUR depuis $INSTALL_DIR, sous l'utilisateur $SERVICE_USER.
cd "$INSTALL_DIR" || exit 1
if [ "\$(id -un)" = "$SERVICE_USER" ]; then
    exec $UV_BIN run --no-sync guetteur "\$@"
fi
exec sudo -u "$SERVICE_USER" -H $UV_BIN run --no-sync guetteur "\$@"
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
# Le refresh NotebookLM ne démarre que quand un master_token.json ou un
# storage_state.json existe déjà dans le home dédié (poussés depuis WSL).
if [[ "$WITH_NOTEBOOKLM" == "1" ]] && \
    [[ -s "$INSTALL_DIR/data/nlm/master_token.json" \
        || -s "$INSTALL_DIR/data/nlm/storage_state.json" ]]; then
    systemctl enable guetteur-nlm-refresh.timer >/dev/null
    systemctl start guetteur-nlm-refresh.timer
    ok "refresh NotebookLM activé (toutes les 6 h)"
else
    systemctl disable guetteur-nlm-refresh.timer >/dev/null 2>&1 || true
    ok "refresh NotebookLM en attente (poussez le profil depuis WSL — voir README)"
fi

cat <<NEXT

Installation terminée. Il reste 4 commandes à lancer dans le conteneur :

  1. install -m 600 -o $SERVICE_USER -g $SERVICE_USER /root/.env $INSTALL_DIR/.env
       (après : scp .env root@<ip-du-lxc>:/root/.env depuis votre PC)
  2. sudo -u $SERVICE_USER -i claude auth login
  3. guetteur doctor
  4. systemctl enable --now guetteur

NEXT
