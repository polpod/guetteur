#!/usr/bin/env bash
# Installation de GUETTEUR sans Docker sur Debian 12 (dans le conteneur LXC, en root).
#
#   bash install-lxc.sh
#
# Lancé automatiquement par proxmox-create-lxc.sh. Idempotent : chaque outil n'est installé
# que s'il manque, le dépôt est mis à jour s'il est déjà cloné.
#
# Variables facultatives : REPO_URL (sinon git@github.com:polpod/guetteur.git, puis HTTPS),
# INSTALL_DIR (/opt/guetteur), SERVICE_USER (guetteur), WITH_WHISPER=1, WITH_NOTEBOOKLM=1,
# UPDATE_CLAUDE=1, WITH_VAULT=1 (clone du vault Obsidian dans data/vault),
# VAULT_REMOTE (git@github.com:polpod/vault-veille.git par défaut).
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

    # 2. known_hosts : les 3 clés publiques de github.com, épinglées ici (source :
    #    https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
    #    Vérifiées par empreinte SHA256 avant écriture — jamais de StrictHostKeyChecking=no.
    gh_kh="$(mktemp)"
    cat >"$gh_kh" <<'KH'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
KH
    expected_fps="SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s
SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM
SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
    got_fps="$(ssh-keygen -lf "$gh_kh" | awk '{ print $2 }')"
    while IFS= read -r fp; do
        printf '%s\n' "$got_fps" | grep -qxF "$fp" \
            || die "empreinte GitHub attendue absente : $fp (vérifier la source)"
    done <<<"$expected_fps"

    # Idempotent : on retire toute ancienne entrée github.com du known_hosts et on
    # rajoute les 3 lignes vérifiées.
    touch "$known_hosts"
    grep -v '^github\.com ' "$known_hosts" > "$known_hosts.tmp" || true
    cat "$gh_kh" >> "$known_hosts.tmp"
    mv "$known_hosts.tmp" "$known_hosts"
    chown "$SERVICE_USER:$SERVICE_USER" "$known_hosts"
    chmod 0600 "$known_hosts"
    rm -f "$gh_kh"
    ok "known_hosts GitHub vérifiés (3 empreintes SHA256)"

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
                    read -r _ < /dev/tty
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
[[ "$WITH_WHISPER" == "1" ]] && extra+=(--extra whisper)
[[ "$WITH_NOTEBOOKLM" == "1" ]] && extra+=(--extra notebooklm)
sudo -u "$SERVICE_USER" -H bash -c 'cd "$1" && shift && uv python install 3.12 \
    && uv sync --frozen --no-dev --python 3.12 "$@"' _ "$INSTALL_DIR" "${extra[@]}"
ok "environnement prêt ($INSTALL_DIR/.venv)"

if [[ "$WITH_NOTEBOOKLM" == "1" ]]; then
    log "notebooklm-py $NLM_VERSION (uv tool, wheel épinglé par hash, sans extra)"
    # Contraintes uv : version + hash sha256 audité. Wheel seulement (--no-sources
    # empêche toute source-dist qui exécuterait le hook de build).
    nlm_constraints="$(mktemp)"
    trap 'rm -f "$nlm_constraints"' EXIT
    cat >"$nlm_constraints" <<EOF
notebooklm-py==${NLM_VERSION} --hash=${NLM_HASH}
EOF
    # `uv tool install` crée /root/.local/share/uv/tools/notebooklm-py/ et un
    # entrypoint dans /root/.local/bin ; on symlink dans /usr/local/bin pour le
    # service systemd (User=guetteur n'a pas /root/.local/bin dans le PATH).
    if ! /usr/local/bin/uv tool install --force --constraints "$nlm_constraints" \
        "notebooklm-py==${NLM_VERSION}" >/dev/null; then
        die "installation de notebooklm-py $NLM_VERSION en échec (hash sha256 ?)."
    fi
    nlm_bin="$(/usr/local/bin/uv tool dir)/notebooklm-py/bin/notebooklm"
    [[ -x "$nlm_bin" ]] || die "notebooklm introuvable après uv tool install ($nlm_bin)"
    ln -sf "$nlm_bin" /usr/local/bin/notebooklm
    ok "$(/usr/local/bin/notebooklm --version 2>&1 | head -1)"
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
