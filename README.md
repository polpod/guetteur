# GUETTEUR

GUETTEUR surveille des playlists YouTube. Pour chaque nouvelle vidéo, il récupère la
transcription, la fait résumer par Claude, puis envoie le résumé sur **Telegram** ou
**WhatsApp**. Le canal se choisit playlist par playlist.

Deux backends de résumé sont disponibles (`[summarize] provider` dans `config.toml`) :

- **`claude_code`** (par défaut) : passe par le binaire **Claude Code** (`claude -p`), déjà
  connecté à votre compte Claude. **Aucune clé API** n'est nécessaire.
- **`claude_api`** : passe par le SDK `anthropic` avec une clé `ANTHROPIC_API_KEY`.

Chaque résumé contient :

- un titre ;
- un TL;DR en 2 phrases ;
- 5 à 8 points clés, chacun avec un horodatage cliquable (`https://youtu.be/ID?t=SECONDES`) ;
- une phrase « pourquoi ça compte » ;
- une durée de lecture estimée.

---

## Sommaire

1. [Fonctionnement](#fonctionnement)
2. [Installation dans un LXC Proxmox sans Docker (Claude Code)](#installation-dans-un-lxc-proxmox-sans-docker-claude-code)
3. [Installation dans un LXC Proxmox avec Docker](#installation-dans-un-lxc-proxmox-debian-12)
4. [Obtenir les jetons Telegram](#obtenir-les-jetons-telegram)
5. [Obtenir les jetons WhatsApp (Meta Cloud API)](#obtenir-les-jetons-whatsapp-meta-cloud-api)
6. [Mettre une playlist en « non répertoriée »](#mettre-une-playlist-en--non-répertoriée-)
7. [Playlist privée : procédure OAuth](#playlist-privée--procédure-oauth)
8. [Commandes](#commandes)
9. [Configuration](#configuration)
10. [Développement](#développement)
11. [Limites connues](#limites-connues)

---

## Fonctionnement

```
playlists (RSS ou YouTube Data API)
        │  toutes les N secondes (300 par défaut)
        ▼
  SQLite data/guetteur.db ── new ─► transcribed ─► summarized ─► sent
        │                        │                       └─► failed (après 3 nouveaux essais)
        │                        └─► retry (pas de sous-titres, 3 cycles max)
        ▼
 sous-titres YouTube (fr → en → n'importe quelle langue)
   └─ secours optionnel : yt-dlp (audio m4a) + faster-whisper « small » sur CPU
        ▼
 Claude (défaut : claude-sonnet-4-6) via `claude -p` (claude_code) ou le SDK (claude_api)
        │  → JSON validé par schéma → MarkdownV2 (Telegram) / texte (WhatsApp)
```

- **Premier lancement** : les vidéos déjà présentes dans une playlist sont marquées `sent` sans
  être traitées. Seules les vidéos publiées ensuite sont résumées. Pour traiter l'existant,
  utilisez `guetteur backfill`.
- **Idempotence stricte** : juste avant l'envoi, la vidéo passe de `summarized` à `sent` dans
  une seule transaction SQLite atomique. Une vidéo ne peut donc être envoyée qu'une fois, même
  si le service redémarre. Si l'envoi échoue, elle repasse en `summarized` et un nouvel essai
  aura lieu au cycle suivant, sans rappeler Claude.
- **Pas de transcription** : sans sous-titres et avec whisper désactivé, la vidéo passe en
  `retry`. Elle est retentée pendant 3 cycles, puis abandonnée (`failed`) et un message
  « pas de transcription » est envoyé.
- **Débit** : au plus `max_videos_per_cycle` vidéos (3 par défaut) sont traitées par cycle,
  l'une après l'autre.
- **Découpage** : une transcription de plus de 150 000 caractères est découpée en morceaux.
  Chaque morceau est résumé, puis les résumés partiels sont fusionnés en un seul résumé.
- **Backend indisponible** : si le binaire `claude` est introuvable ou que sa session n'est
  pas connectée (ou si la clé API est refusée), le cycle s'arrête avec une erreur explicite
  dans les logs. Les vidéos en attente **ne consomment pas d'essai** : elles seront traitées
  dès que le problème sera réglé.
- **Logs** : une ligne JSON par événement sur la sortie standard (`journalctl -u guetteur`
  ou `docker compose logs`).

### Backend `claude_code` : comment le binaire est appelé

```text
claude -p "<consigne + métadonnées>" --output-format json --model <claude_model> \
       --system-prompt "<prompt système GUETTEUR>" --json-schema '<schéma du résumé>' \
       --tools "" --permission-mode plan --no-session-persistence \
       --strict-mcp-config --setting-sources ""
       < transcription sur stdin
```

- Le binaire est lancé sans shell (`asyncio.create_subprocess_exec`), dans un dossier
  temporaire vide.
- `--tools ""` et `--permission-mode plan` désactivent **tous** les outils : pas de
  commande, pas de fichier, pas de web.
- `--setting-sources ""` et `--strict-mcp-config` ignorent les réglages, hooks et serveurs
  MCP de l'utilisateur.
- `ANTHROPIC_API_KEY` est retirée de l'environnement du binaire : c'est bien la **session
  Claude Code** qui est utilisée, même si une clé traîne dans `.env`.
- Le champ `result` de la sortie JSON contient le résumé, validé par `--json-schema`. Le
  schéma est le même que pour le backend API.
- Délai maximal : `summarize.timeout_s` (180 s par défaut). Au-delà, le processus est tué
  et la vidéo est retentée au cycle suivant.

---

## Installation dans un LXC Proxmox sans Docker (Claude Code)

C'est le mode recommandé avec le backend `claude_code` : l'image Docker ne contient pas
Claude Code.

### 1. Créer le conteneur

Suivez l'étape 1 de la [section Docker](#1-créer-le-conteneur-lxc) (Debian 12, 2 cœurs,
1 Go de RAM). Les options `nesting`/`keyctl` sont inutiles sans Docker.

### 2. Lancer le script d'installation

Dans le LXC, en root :

```bash
apt update && apt install -y git
git clone <url-de-votre-dépôt> /root/guetteur-src
REPO_URL=<url-de-votre-dépôt> bash /root/guetteur-src/scripts/install-lxc.sh
# ou, sans dépôt git : copiez le projet (scp -r) puis lancez bash scripts/install-lxc.sh
```

`scripts/install-lxc.sh` :

1. installe `ffmpeg`, `git`, `curl` et `sqlite3` ;
2. installe **Node.js 22** (NodeSource), puis **Claude Code** avec
   `npm i -g @anthropic-ai/claude-code` ;
3. installe **uv** dans `/usr/local/bin`, puis **Python 3.12** avec `uv python install` ;
4. crée l'utilisateur système `guetteur`, avec un vrai `HOME` pour la session Claude ;
5. clone (ou copie) le projet dans `/opt/guetteur`, puis lance
   `uv sync --frozen --no-dev` (ajoutez `WITH_WHISPER=1` pour whisper) ;
6. crée `/opt/guetteur/.env` (mode 600) à partir de `.env.example` ;
7. installe et active `deploy/guetteur.service`, sans le démarrer.

Relancer le script met l'installation à jour (`git pull` puis `uv sync`).

### 3. Connecter Claude Code pour l'utilisateur `guetteur` (en SSH)

La session Claude Code appartient à l'utilisateur système qui lance le binaire. Il faut
donc se connecter **en tant que `guetteur`**. Depuis votre PC :

```bash
ssh root@<ip-du-lxc>
sudo -u guetteur -i claude auth login
```

(`claude login` n'existe pas comme sous-commande : utilisez `claude auth login`, ou lancez
`claude` en interactif puis tapez `/login`.)

1. Le binaire affiche une URL de connexion. Ouvrez-la dans le navigateur **de votre PC**
   et connectez-vous avec votre compte Claude (Pro/Max).
2. Copiez le code affiché par le navigateur et collez-le dans le terminal SSH.
3. Vérifiez la connexion :

   ```bash
   sudo -u guetteur -i claude auth status                  # "loggedIn": true
   cd /opt/guetteur && sudo -u guetteur -H uv run --no-sync guetteur doctor
   ```

La session est stockée dans `/home/guetteur/.claude/` et se renouvelle automatiquement.
Si `doctor` affiche `claude : session  KO  … not logged in`, relancez `claude auth login`.

> Alternative sans navigateur : sur une machine déjà connectée, `claude setup-token` génère
> un jeton longue durée. Mettez-le dans `/opt/guetteur/.env` sous
> `CLAUDE_CODE_OAUTH_TOKEN=…` : le binaire lancé par le service l'utilisera.

### 4. Configurer et démarrer

```bash
nano /opt/guetteur/.env          # jetons Telegram / WhatsApp (pas de clé Anthropic requise)
nano /opt/guetteur/config.toml   # playlists ; [summarize] provider = "claude_code"
cd /opt/guetteur && sudo -u guetteur -H uv run --no-sync guetteur doctor
sudo -u guetteur -H uv run --no-sync guetteur test-notify
systemctl start guetteur
journalctl -u guetteur -f
```

L'unité `deploy/guetteur.service` lance `uv run --no-sync guetteur run` sous l'utilisateur
`guetteur`, avec `WorkingDirectory=/opt/guetteur`,
`EnvironmentFile=/opt/guetteur/.env` et `Restart=always`.

---

## Installation dans un LXC Proxmox (Debian 12)

> Avec Docker, le backend `claude_code` n'est pas disponible (l'image n'embarque pas
> Claude Code) : mettez `provider = "claude_api"` dans `[summarize]` et renseignez
> `ANTHROPIC_API_KEY` dans `.env`. Sinon, `guetteur run` s'arrête au démarrage avec une
> erreur explicite.

### 1. Créer le conteneur LXC

Dans l'interface Proxmox, **Create CT** :

| Paramètre | Valeur conseillée |
|---|---|
| Template | `debian-12-standard` |
| Disque | 8 Go (12 Go avec whisper) |
| CPU | 2 cœurs (4 avec whisper) |
| Mémoire | 1 Go (3 Go avec whisper) |
| Réseau | DHCP ou IP fixe |
| Non privilégié | oui |

Avant de démarrer le conteneur, activez dans **Options → Features** : `nesting` et `keyctl`.
Docker en a besoin dans un LXC non privilégié.

La même chose en ligne de commande, sur l'hôte Proxmox :

```bash
pct create 120 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname guetteur --cores 2 --memory 1024 --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged 1 \
  --features nesting=1,keyctl=1 --onboot 1
pct start 120 && pct enter 120
```

### 2. Installer Docker et Docker Compose

Dans le LXC :

```bash
apt update && apt install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list
apt update && apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
docker run --rm hello-world
```

### 3. Déployer GUETTEUR

```bash
mkdir -p /opt && cd /opt
git clone <url-de-votre-dépôt> guetteur   # ou copiez le dossier (scp -r)
cd guetteur

cp .env.example .env && chmod 600 .env
nano .env            # ANTHROPIC_API_KEY, jetons Telegram et/ou WhatsApp
nano config.toml     # vos playlists

# Le conteneur tourne sous l'utilisateur uid 1000 : il doit pouvoir écrire dans ./data
mkdir -p data && chown -R 1000:1000 data

docker compose up -d --build
docker compose logs -f
```

Le service redémarre automatiquement (`restart: unless-stopped`). La base SQLite, le jeton
OAuth et le cache whisper se trouvent dans `./data`.

Mise à jour :

```bash
git pull && docker compose up -d --build
```

---

## Obtenir les jetons Telegram

1. Dans Telegram, ouvrez **@BotFather** et envoyez `/newbot`. Choisissez un nom, puis un
   identifiant qui se termine par `bot`.
2. BotFather répond avec un jeton de la forme `123456789:AA…`. Il va dans
   `TELEGRAM_BOT_TOKEN`.
3. Envoyez un message quelconque à votre bot (ou ajoutez-le à un groupe et écrivez dans le
   groupe).
4. Récupérez l'identifiant du chat :

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'
   ```

   Le champ `"id"` va dans `TELEGRAM_CHAT_ID`. Il est positif pour une conversation privée,
   négatif pour un groupe (par exemple `-100…` pour un supergroupe).
5. Vérifiez : `docker compose run --rm guetteur test-notify --channel telegram`.

---

## Obtenir les jetons WhatsApp (Meta Cloud API)

1. Sur <https://developers.facebook.com/apps>, cliquez sur **Créer une app** et choisissez le
   type **Business**. Rattachez l'app à un Business Manager (il est créé au besoin).
2. Dans l'app, ajoutez le produit **WhatsApp** puis ouvrez **WhatsApp → API Setup** :
   - **Phone number ID** → `WA_PHONE_ID` (numéro de test fourni par Meta, ou votre propre
     numéro une fois ajouté) ;
   - **To** : ajoutez votre numéro personnel à la liste des destinataires autorisés. Vous
     recevez un code de validation sur WhatsApp. Ce même numéro, au format international sans
     `+` ni espaces (par exemple `33612345678`), va dans `WA_TO`.
3. **Jeton permanent** : le jeton affiché sur la page API Setup expire au bout de 24 h. Pour
   un jeton durable :
   - dans **Business Settings → Utilisateurs → Utilisateurs système**, créez un utilisateur
     système avec le rôle *Admin* ;
   - **Ajouter des ressources** : sélectionnez l'app, avec le contrôle total ;
   - **Générer un jeton** : sélectionnez l'app, choisissez une expiration « Jamais » et les
     permissions `whatsapp_business_messaging` et `whatsapp_business_management` ;
   - le jeton obtenu va dans `WA_TOKEN`.
4. **Fenêtre de 24 h** : WhatsApp n'accepte un message texte libre que dans les 24 h qui
   suivent le **dernier message que vous avez envoyé** au numéro expéditeur. Envoyez
   régulièrement un message (« ok ») au numéro de l'app pour garder la fenêtre ouverte.
   Si la fenêtre est fermée, Meta refuse le texte (erreur `131047`). GUETTEUR envoie alors un
   **message modèle** (*template*). Pour que ce message contienne le titre et le lien :
   - dans **WhatsApp Manager → Modèles de message**, créez un modèle, par exemple
     `nouveau_resume`, catégorie *Utility*, langue *Français*, avec le corps :
     `Nouveau résumé GUETTEUR : {{1}}. Répondez « ok » pour recevoir le résumé complet.` ;
   - une fois le modèle approuvé, renseignez dans `config.toml` :

     ```toml
     [whatsapp]
     template_name = "nouveau_resume"
     template_language = "fr"
     template_body_param = true
     ```

   Le modèle `hello_world`, présent par défaut, fonctionne aussi mais ne contient aucune
   information sur la vidéo.
5. Vérifiez : `docker compose run --rm guetteur test-notify --channel whatsapp`.

---

## Mettre une playlist en « non répertoriée »

Le flux RSS (`https://www.youtube.com/feeds/videos.xml?playlist_id=ID`) fonctionne avec les
playlists **publiques** et **non répertoriées**. C'est le mode le plus simple, sans OAuth.

1. Ouvrez <https://studio.youtube.com> → **Contenu** → onglet **Playlists**.
2. Survolez la playlist → icône crayon (ou **Modifier sur YouTube**).
3. **Visibilité** → **Non répertoriée** → **Enregistrer**.

   Depuis youtube.com, vous pouvez aussi ouvrir la playlist, cliquer sur **⋯ → Paramètres
   de la playlist** (ou sur le cadenas sous le titre) et choisir **Non répertoriée**.
4. L'identifiant de la playlist est la valeur du paramètre `list=` de son URL. Par exemple
   `https://www.youtube.com/playlist?list=PLabc…` donne l'identifiant `PLabc…`.
5. Ajoutez-la dans `config.toml` avec `private = false`.

Astuce : pour suivre toutes les vidéos d'une chaîne, utilisez sa playlist « uploads ». Son
identifiant est celui de la chaîne dont le préfixe `UC…` est remplacé par `UU…`.

---

## Playlist privée : procédure OAuth

Une playlist **privée** n'a pas de flux RSS. GUETTEUR passe alors par la **YouTube Data
API v3** avec un jeton OAuth (lecture seule, portée `youtube.readonly`), stocké dans
`data/token.json`.

### 1. Créer les identifiants Google

1. Sur <https://console.cloud.google.com>, créez un projet, par exemple `guetteur`.
2. **API et services → Bibliothèque** : activez **YouTube Data API v3**.
3. **API et services → Écran de consentement OAuth** :
   - type d'utilisateur : **Externe** ;
   - renseignez le nom de l'app et votre adresse ;
   - **Utilisateurs test** : ajoutez le compte Google propriétaire de la playlist ;
   - ⚠️ En mode « Test », le jeton de rafraîchissement **expire après 7 jours**. Pour un
     service permanent, cliquez sur **Publier l'application** (passage en « Production »).
     Pour un usage personnel, aucune vérification Google n'est nécessaire : un avertissement
     « application non validée » s'affichera simplement lors du consentement.
4. **Identifiants → Créer des identifiants → ID client OAuth** → type **Application de
   bureau**. Téléchargez le JSON et copiez-le dans `data/client_secret.json` sur le LXC.

### 2. Autoriser l'accès (une seule fois)

**Option A : depuis le LXC, avec un tunnel SSH.** Sur votre PC, ouvrez un tunnel :

```bash
ssh -L 8765:localhost:8765 root@<ip-du-lxc>
```

Puis, dans cette session SSH, sur le LXC :

```bash
cd /opt/guetteur
docker compose run --rm -p 127.0.0.1:8765:8765 guetteur auth --bind 0.0.0.0
```

La commande affiche une URL Google. Ouvrez-la dans le navigateur **de votre PC** et
acceptez. Google redirige vers `http://localhost:8765/…`, qui passe par le tunnel. Le jeton
est alors écrit dans `data/token.json`.

**Option B : sur votre PC** (Python et uv installés) :

```bash
cp client_secret.json data/ && uv run guetteur auth
scp data/token.json root@<ip-du-lxc>:/opt/guetteur/data/
ssh root@<ip-du-lxc> chown 1000:1000 /opt/guetteur/data/token.json
```

### 3. Déclarer la playlist

```toml
[[playlists]]
id = "PLxxxxxxxx"
label = "Ma playlist privée"
language = "fr"
notify = "telegram"
private = true
```

Le jeton est rafraîchi automatiquement. S'il est révoqué, relancez `guetteur auth`.

---

## Commandes

Avec Docker (depuis le dossier du projet) :

```bash
docker compose up -d                                          # service (guetteur run)
docker compose run --rm guetteur doctor                       # vérifications OK/KO
docker compose run --rm guetteur test-notify                  # message de test sur les canaux utilisés
docker compose run --rm guetteur test-notify --channel whatsapp
docker compose run --rm guetteur once                         # un seul cycle puis sortie
docker compose run --rm guetteur backfill --playlist PLxxx --limit 5
docker compose logs -f guetteur
```

Sans Docker :

```bash
uv sync                      # ajoutez --extra whisper pour le secours whisper
uv run guetteur doctor
uv run guetteur test-notify
uv run guetteur once
uv run guetteur run
uv run guetteur backfill --playlist PLxxx --limit 5
```

| Commande | Rôle |
|---|---|
| `run` | Service : un cycle immédiatement, puis un cycle toutes les `poll_interval_seconds` secondes. |
| `once` | Un seul cycle. Code retour 1 si une vidéo est passée en `failed`. |
| `backfill --playlist ID --limit N` | Traite les N vidéos les plus récentes de la playlist, y compris celles ignorées au premier lancement. Une vidéo réellement envoyée ne l'est jamais une seconde fois. |
| `doctor` | Tableau OK/KO : binaire `claude` trouvé et connecté (`claude -p "ping" --output-format json` doit répondre), ffmpeg, base SQLite, jetons des canaux utilisés (et `ANTHROPIC_API_KEY` si `provider = "claude_api"`). Code retour 1 si une ligne est KO. |
| `test-notify [--channel …]` | Envoie un faux résumé (avec des caractères spéciaux) pour valider l'échappement et les liens. |
| `auth [--port 8765] [--bind …]` | Autorisation OAuth pour les playlists privées. |

Option globale : `--config chemin/config.toml` (ou la variable `GUETTEUR_CONFIG`).

---

## Configuration

`config.toml` contient les réglages, sans aucun secret. Les secrets vont dans `.env`.

| Clé | Défaut | Description |
|---|---|---|
| `general.poll_interval_seconds` | `300` | Intervalle entre deux cycles, en secondes. |
| `general.claude_model` | `claude-sonnet-4-6` | Modèle Claude. |
| `general.max_videos_per_cycle` | `3` | Nombre maximal de vidéos traitées par cycle. |
| `general.data_dir` | `data` | Emplacement de la base, du jeton OAuth et de `client_secret.json`. |
| `summarize.provider` | `claude_code` | `claude_code` (binaire Claude Code connecté, sans clé) ou `claude_api` (SDK, `ANTHROPIC_API_KEY` obligatoire, sinon erreur au démarrage). |
| `summarize.claude_code_bin` | `claude` | Nom du binaire dans le `PATH` ou chemin absolu. |
| `summarize.timeout_s` | `180` | Délai maximal d'un appel de résumé, en secondes. |
| `transcript.languages` | `["fr", "en"]` | Ordre de préférence des sous-titres. Si aucune de ces langues n'existe, n'importe quelle langue est prise. |
| `transcript.whisper_enabled` | `false` | Active le secours yt-dlp + faster-whisper. Nécessite de construire l'image avec `WITH_WHISPER: "1"` dans `docker-compose.yml`. |
| `transcript.whisper_model` | `small` | Modèle faster-whisper, exécuté sur CPU en int8. |
| `transcript.max_retries` | `3` | Nombre de nouveaux essais (un par cycle) avant abandon. |
| `whatsapp.*` | | Version de l'API Graph et modèle utilisé hors fenêtre de 24 h. |
| `[[playlists]]` | | `id`, `label`, `language` (langue du résumé), `notify` (`telegram` ou `whatsapp`), `private`. |

Variables de `.env` : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WA_TOKEN`, `WA_PHONE_ID`,
`WA_TO`, et `ANTHROPIC_API_KEY` uniquement avec `provider = "claude_api"`.

Une clé inconnue à la racine de `config.toml` est refusée. C'est typiquement un en-tête
`[[playlists]]` oublié ou commenté : sans lui, la playlist serait ignorée en silence.

---

## Développement

```bash
uv sync --extra whisper
uv run ruff check . && uv run ruff format --check .
uv run mypy            # mode strict, sur src/ et tests/
uv run pytest
```

Organisation du code :

```
src/guetteur/
  config.py            # config.toml + secrets depuis l'environnement
  main.py              # CLI et boucle schedule
  doctor.py            # guetteur doctor
  pipeline.py          # orchestration séquentielle (poll → transcription → résumé → envoi)
  store.py             # SQLite, machine à états, idempotence
  logs.py              # logs JSON
  sources/             # rss.py (feedparser), api.py (YouTube Data API v3 + OAuth)
  transcript/          # youtube.py (youtube_transcript_api), whisper.py (yt-dlp + faster-whisper)
  summarize/           # base.py (interface Summarizer, prompt, schéma, découpage)
                       # claude_code.py (binaire claude -p), claude_api.py (SDK anthropic)
                       # format.py (MarkdownV2 / texte)
  notify/              # base.py (Notifier), telegram.py, whatsapp_cloud.py
scripts/install-lxc.sh # installation sans Docker (Debian 12)
deploy/guetteur.service
tests/
  test_*.py            # RSS, découpage/échappement, store, backends (SDK et subprocess mockés),
                       # notifieurs, config, doctor
  e2e/scenarios.py     # scénarios communs aux deux backends
  e2e/test_pipeline.py              # backend claude_api (SDK mocké)
  e2e/test_pipeline_claude_code.py  # backend claude_code (subprocess mocké) + pannes du binaire
```

---

## Limites connues

- **Claude Code et quotas** : avec `claude_code`, les résumés consomment le quota de votre
  abonnement Claude, partagé avec votre usage interactif. Si la limite est atteinte, le
  binaire renvoie une erreur : la vidéo est retentée aux cycles suivants, dans la limite de
  `max_retries`. Baissez `max_videos_per_cycle` si nécessaire.
- **Options du binaire** : le backend s'appuie sur `--json-schema`, `--tools` et
  `--setting-sources` (Claude Code 2.1.x). Après une mise à jour majeure de Claude Code,
  relancez `guetteur doctor`.

- **RSS** : YouTube ne publie que les **15 dernières vidéos** d'une playlist. Au-delà de 15
  ajouts entre deux cycles, les plus anciennes sont manquées. Avec l'API (`private = true`),
  les 50 dernières sont lues.
- **Sous-titres** : depuis une IP de datacenter, YouTube peut bloquer la récupération des
  sous-titres. Une IP résidentielle (homelab) fonctionne en général. Les blocages sont traités
  comme des erreurs transitoires et retentés.
- **WhatsApp** : l'erreur « fenêtre de 24 h fermée » n'arrive pas toujours dans la réponse
  HTTP. Meta peut accepter le message (HTTP 200), puis signaler l'échec plus tard par webhook,
  que GUETTEUR n'écoute pas. Dans ce cas, le message est perdu en silence. Gardez la fenêtre
  ouverte en écrivant régulièrement au numéro.
- **Au plus une fois** : si le processus est tué pendant l'envoi, la vidéo reste `sent` et
  n'est pas renvoyée. C'est le prix de la garantie « jamais deux fois ».
- **Une vidéo présente dans deux playlists** n'est résumée et envoyée qu'une seule fois, pour
  la première playlist où elle a été vue.
- **Durée de lecture** : elle est calculée sur la base de 200 mots par minute, pas estimée par
  le modèle.
