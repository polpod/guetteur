# Audit de sécurité — notebooklm-py 0.8.3

**Date** : 2026-09-26
**Contexte** : intégration dans GUETTEUR, service qui tournera 24/7 avec un compte Google.
**Méthode** : lecture seule.
- Aucune installation globale.
- Aucun code audité exécuté ou importé.
- Aucun login.

Les grep ont été faits sur un export de l'arbre du tag `v0.8.3`. Les dépendances ont été résolues dans `./venv` avec `--no-install-project`, donc sans build ni installation du paquet audité.

## Synthèse

| Axe | Verdict |
|---|---|
| 1. Provenance (git ↔ sdist ↔ wheel) | **OK** |
| 2. Surface réseau | **OK** |
| 3. Exécution dynamique / accès système | **OK** (un mécanisme opt-in à neutraliser) |
| 4. Fichiers et identifiants | **À surveiller** (écarts mineurs, rien de bloquant) |
| 5. Dépendances | **OK** (graphe minimal ; extra `cookies` à exclure) |
| 6. Historique et gouvernance | **À surveiller** (mainteneur unique, publication non protégée) |

**Recommandation finale : intégrable avec conditions** (voir §8).

---

## 1. Provenance — OK

| Élément | Valeur |
|---|---|
| Dernière release | `v0.8.3` (tag annoté, non signé), publiée sur PyPI le 2026-09-26T03:46Z |
| Commit du tag | `53fc7c50acc22f94729e5b067075640d8ddf7496` |
| HEAD de `main` au clonage | `cf889298c1a2757b542fd54437f8e79e8f2cda0e` (4 commits après le tag, hors release) |
| Wheel | `notebooklm_py-0.8.3-py3-none-any.whl` — sha256 `7e3e02057b3acf354d3dbc337c08869d2a4954c9c324f3271e272236cfcc2bfc` |
| Sdist | `notebooklm_py-0.8.3.tar.gz` — sha256 `7d444c19193e405bae7277b37c29a365c936a8d278a1f62910f6b06d92aabf2a` |

**Écart de méthode (volontaire).** `pip download --no-binary :all:` exécute le backend de build pour préparer les métadonnées. Ici, cela lancerait `hatch_build.py`, qui est du code du projet audité. Les deux artefacts ont donc été téléchargés directement depuis les URL PyPI, et leur sha256 a été vérifié contre l'API JSON de PyPI.

Le hook `hatch_build.py` a été lu : il se contente de `git rev-parse --short=8 HEAD`, dont le résultat est écrit dans `_commit.py`. Il est bénin.

**Diff récursif :**

- **sdist ↔ tag** : seuls `PKG-INFO` et `src/notebooklm/_commit.py` (métadonnées de build) diffèrent.
- **wheel ↔ `src/notebooklm` du tag** : seuls `_commit.py` et `data/` diffèrent.
  - `data/SKILL.md` et `data/CODEX.md` sont identiques à `SKILL.md` et `AGENTS.md` du dépôt (via `force-include`).
- **`_commit.py`** vaut `COMMIT = "53fc7c50"`, ce qui correspond au commit du tag.
- **`RECORD` du wheel** : 564 fichiers vérifiés, 0 hash divergent.
- **Dépendances du wheel** : `Requires-Dist` sans extra = httpx, click, rich, filelock, conformes au `pyproject.toml`.
- **Attestations PEP 740** : présentes pour les deux artefacts.
  - Publisher : GitHub `teng-lin/notebooklm-py`, workflow `publish.yml`, environnement `release`.
  - Certificat Sigstore : `refs/tags/v0.8.3`, commit `53fc7c50`.

➡ **Aucune différence de code** entre le tag git et les artefacts PyPI.

## 2. Surface réseau — OK

Toutes les URL et tous les hôtes codés en dur dans `src/notebooklm` ont été extraits par grep, avec et sans schéma. **Seuls des domaines Google sont contactés.** Tous les autres hôtes apparaissent uniquement dans des commentaires, docstrings, exemples ou messages d'erreur. Exemples : `github.com/teng-lin/...` dans les messages « report a bug », `example.com`, `python.org`, `microsoft.com/edge` (lien d'aide), `cursor.com` et `docs.windsurf.com` (commentaires).

### Domaines effectivement contactés

| Domaine | Fichier(s) | Usage | Quand |
|---|---|---|---|
| `notebook.google.com` (défaut), `notebooklm.google.com` | `_env.py:20-37`, `rpc/types.py:94-96` | RPC `batchexecute`, chat, upload | Toujours |
| `notebooklm.cloud.google.com` | `_env.py:22` | Variante Enterprise | Si `NOTEBOOKLM_BASE_URL` est défini |
| `accounts.google.com` | `_auth/mint_service.py:23,254,262` | `RotateCookies` (keepalive), `OAuthLogin`, `MergeSession` | À l'ouverture de session et en keepalive ; mint headless |
| `android.clients.google.com` | dépendance `gpsoauth` (`AUTH_URL`) | Échange ou usage du master token | Extra `headless` uniquement |
| `notebooklm-pa.googleapis.com:443` | `_android/session.py:62`, `_android/upload.py:64` | gRPC Android | Extra `android` uniquement |
| `www.googleapis.com` | `_android/drive_staging.py:105`, `_android/phenotype.py:51` | Upload Drive, config d'expériences | Extra `android` uniquement |
| `drive.usercontent.google.com`, `drive.google.com` | `_web/sources/drive_import.py:74-78` | Import de sources Drive | Sur demande |
| `*.google.com`, `*.googleusercontent.com`, `*.googleapis.com` (+ `*.googlevideo.com` côté Android) | `_artifact/_download_client.py:26`, `_android/assets.py:46` | Téléchargement d'artefacts (audio, vidéo, slides) | Sur demande |

### Contrôles de sécurité réseau constatés

- **`NOTEBOOKLM_BASE_URL`** est restreint à une allowlist d'hôtes Google : https uniquement, sans port, userinfo, chemin ni query (`_env.py:70-96`).
- **Téléchargements d'artefacts** : l'allowlist d'hôtes est revérifiée **à chaque redirection**, avec https imposé (`_artifact/_redirect_guard.py`). Les hôtes contenant `%`, `\` ou `/` sont rejetés, ce qui corrige l'issue #1521.
- **URL de sources ajoutées par l'utilisateur** : la bibliothèque les transmet à Google, qui les récupère lui-même ; elle ne les télécharge pas.

### Télémétrie

Aucune télémétrie, analytics, Sentry, PostHog, « phone home » ni vérification de mise à jour. Aucun appel à `pypi.org`.

- Les occurrences de « telemetry » concernent un callback **fourni par l'appelant** (`on_rpc_event`, `_client_metrics.py:131`), `None` par défaut.
- `_version_check.py` ne vérifie que la version locale de Python.
- `cclog` (`_android/auth.py:25`) est un scope OAuth demandé par l'extra Android. `clearcut_logger_header` (`_android/phenotype.py:143`) est un en-tête du protocole d'expériences envoyé à `www.googleapis.com`. Ces deux éléments ne concernent que l'extra `android`, et Google reste le seul destinataire.

## 3. Exécution dynamique et accès système — OK

Aucune occurrence de `eval(`, `exec(`, `pickle.load`, `marshal`, `os.system`, `os.popen`, `runpy`, ni de chaîne encodée longue (≥ 120 caractères base64 ou hex, hors stubs protobuf).

| Occurrence | Chemin | Verdict |
|---|---|---|
| `re.compile(...)` (nombreuses) | partout | Légitime : expressions régulières, pas `compile()` |
| `importlib.import_module` | `client.py:126`, `raw.py:248`, `rpc/__init__.py:74`, `_android/session.py:217-226`, `cli/services/login/*_accounts.py` | Légitime : imports paresseux vers des noms **fixes** tirés de tables constantes (`_LAZY_EXPORTS`), ou `grpc` et `google.protobuf` |
| `subprocess.run(["git", "rev-parse", ...])` | `_version_info.py:44` | Légitime : lit le commit uniquement si un `.git` est présent |
| `subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])` | `cli/services/playwright_login.py:74-92` | Légitime : login CLI via Playwright, sortie masquée (redaction) |
| `subprocess.run(NOTEBOOKLM_REFRESH_CMD)` | `_auth/refresh.py:584` | **Opt-in, à neutraliser** (voir extrait ci-dessous) |
| `ctypes` (`CommandLineToArgvW`, kernel32) | `_auth/refresh.py:437`, `mcp/_hostupload.py:44` | Légitime : Windows uniquement (découpage d'argv, attributs de fichiers) |
| `base64.b64decode` | `_android/phenotype.py:133`, `mcp/_filelink.py:110`, `mcp/tools/_fileupload.py:89` | Légitime : décode des données **reçues** (jeton serveur, payload d'upload MCP), pas de code embarqué |
| `bytes.fromhex("0a020803")` | `_android/phenotype.py:62` | Légitime : 4 octets protobuf constants |
| `*_pb2.py` / `*_pb2_grpc.py` (25 fichiers) | `_android/proto/` | Légitime : sortie standard de `protoc` (`AddSerializedFile`), sources `.proto` livrées dans `proto_src/`, aucune instruction hors du gabarit généré |

Point d'attention, **exécution de commande pilotée par l'environnement** :

```python
# src/notebooklm/_auth/refresh.py:522 et 584
cmd = policy_env(NOTEBOOKLM_REFRESH_CMD_ENV)
...
use_shell = policy_env(NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV) == "1"
...
result = await asyncio.to_thread(subprocess.run, run_target, shell=run_shell, ...)
```

C'est un choix de conception, désactivé par défaut. Par défaut, le code utilise `shlex.split` et `shell=False`, et retire les secrets propres à la bibliothèque de l'environnement de l'enfant. La conséquence reste que **quiconque contrôle l'environnement du service peut exécuter du code**. Voir la condition n° 3.

## 4. Fichiers et identifiants — À surveiller

### Lectures et écritures

- **Racine** : `NOTEBOOKLM_HOME`, par défaut `~/.notebooklm` (`paths.py:127-130`). Les secrets sont stockés dans `profiles/<profil>/`.
- **Écritures confinées à `NOTEBOOKLM_HOME`** : `storage_state.json`, `master_token.json`, `context.json`, `config.json`, `.bak`, `*.lock`, `browser_profile/` et `oauth/`.
- **Écritures hors HOME, toutes sur action explicite** :
  - fichiers téléchargés vers la destination choisie par l'appelant ;
  - `notebooklm mcp install <client>`, qui modifie `~/.claude.json`, `~/.cursor/mcp.json`, la config Windsurf ou Claude Desktop (`_app/mcp_install.py:97-126`) ;
  - `notebooklm skill install`, qui écrit dans `~/.claude/skills/` ;
  - fichiers temporaires `mkdtemp` (0700) plus `O_EXCL` (0600).
- **À l'import de la bibliothèque, rien n'est écrit sur disque.** La migration de l'ancien layout n'est lancée que par le CLI (`notebooklm_cli.py:222`).

### Permissions annoncées dans SECURITY.md, vérifiées dans le code

| Fichier | Annoncé | Constaté |
|---|---|---|
| `storage_state.json` | 0600 | ✔ temporaire créé 0600, `os.fchmod`, fsync, `os.replace` (`_atomic_io.py:251-289`), sans fenêtre TOCTOU |
| `master_token.json` | 0600 | ✔ même chemin atomique ; dossier parent `chmod 0700` en best-effort (`_auth/master_token_file.py:35-41`) |
| `context.json`, `oauth/<slug>.json` | 0600 | ✔ `atomic_write_json` / `atomic_update_json`, mode 0600 par défaut |
| HOME, `profiles/<p>/`, `browser_profile/` | 0700 | ✔ `mkdir(mode=0o700)` puis `chmod(0o700)` explicite (`paths.py:140-143,253-254`, `_app/login_browser.py:166-169`) |
| `.storage_state.json.lock` | — | 0600 (`_auth/storage_lock.py:186`) |

**Écarts mineurs relevés :**

- **Migration de l'ancien layout** (`migration.py:57,176-182`) :
  - `shutil.copy2` puis `chmod` laisse une courte fenêtre où le fichier garde le mode d'origine (le dossier parent est en 0700, ce qui atténue le risque).
  - `context.json` n'est pas dans `_SECRET_LEGACY_FILES` : il **garde son ancien mode** au lieu du 0600 annoncé.
  - Même schéma copy2 + chmod pour le fichier `.bak` (`_auth/profile_store.py:464-467`).
- **`get_home_dir(create=True)` fait `chmod 0700` sur la valeur de `NOTEBOOKLM_HOME`.** Pointer cette variable vers `~` ou vers un dossier partagé en changerait les droits.
- **Fichiers `*.lock` de filelock** : en 0644. Ils sont vides et ne contiennent aucun secret.
- **`mkdir(mode=)`** est soumis à l'umask, et le dossier `oauth/` (serveur MCP, hors périmètre GUETTEUR) n'est pas re-chmodé s'il existe déjà.

### Master token (extra `headless`)

- **Lecture** : uniquement depuis `profiles/<p>/master_token.json`. **Aucune variable d'environnement** ne porte le master token.
- **Destinations** : il n'est transmis qu'à `gpsoauth.perform_oauth` (`_auth/mint_service.py:198`), donc à `android.clients.google.com/auth`. Le bearer `ya29` obtenu n'est envoyé qu'à `accounts.google.com/OAuthLogin`, puis `/MergeSession` (`mint_service.py:252-267`).
- **Aucun log, print ni repr qui l'expose** : `MasterToken` a un `__repr__` masqué, extrait ci-dessous (`_auth/master_token_types.py:22-32`).

  ```python
  @dataclass(frozen=True, repr=False)
  class MasterToken:
      email: str
      android_id: str
      secret: str = field(repr=False)
      def __repr__(self) -> str:
          return f"MasterToken(email={self.email!r}, android_id={self.android_id!r}, secret=<redacted>)"
  ```

- **Erreurs de mint** : l'exception d'origine est jetée (« discard dependency/transport exception + traceback », `mint_service.py:204`).
- **Filtre de redaction** : `configure_logging()` est appelé à l'import (`__init__.py:31`). Il installe un `RedactingFilter` sur les loggers `notebooklm`, `httpx` et `urllib3`, qui masque `aas_et/…`, `ya29.`, `Bearer`, `Cookie:`, `Set-Cookie:` et `SNlM0e`.
- **Petite asymétrie** : l'échange **initial** `oauth_token → master token` fait `raise _MintError(...) from exc` (`mint_service.py:166`). L'exception gpsoauth reste donc chaînée. Le risque est faible, car cela n'arrive qu'une fois, pendant le setup, et l'`oauth_token` est à usage unique.
- **Repr par défaut** : les dataclasses `HopCredentials` (`_hop_credentials.py:11`) et `AuthSnapshot` (`_web/transport/request_types.py:39`) gardent le repr généré. Aucun log de ces objets n'a été trouvé, mais un `logger.debug(obj)` ajouté côté GUETTEUR afficherait des cookies ou le CSRF.

### Extra `cookies` (rookie-cookies)

- **Optionnel** : il n'est ni dans les dépendances de base ni dans `all` (`pyproject.toml`).
- **Import paresseux** : `rookie_cookies` n'est importé que dans des fonctions (`cli/_chromium_profiles.py:338`, `cli/services/login/browser_accounts.py:309`), atteintes seulement via `login --browser-cookies`, `auth refresh --browser-cookies` ou `auth inspect --browser`.
- **Graphe statique de `import notebooklm`** (AST, 127 modules) : seules dépendances externes chargées, `httpx`, `filelock` et `typing_extensions`. **Aucun import** de playwright, rookie, curl_cffi, gpsoauth, grpc ni fastmcp.

### Réseau en arrière-plan

- **Pas de keepalive par défaut** dans la bibliothèque (`keepalive=None`).
- **Un `POST accounts.google.com/RotateCookies` par ouverture de session**, sauf si `storage_state.json` date de moins de 60 s.
- **Désactivable** avec `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`.

## 5. Dépendances — OK

Commandes : `uv sync --frozen --no-install-project --no-dev` dans `./venv` (13 paquets installés), puis `pip-audit --strict --require-hashes` sur `uv export` du lock (`requirements-locked*.txt`).
**Résultat : « No known vulnerabilities found »** pour le graphe minimal et pour le graphe `headless`.

| Paquet | Version verrouillée | Publiée le | 1ʳᵉ release | Mainteneurs / projet | Niveau |
|---|---|---|---|---|---|
| httpx | 0.28.1 | 2024-12-06 | 2019 | encode (lovelydinosaur, cagil) | direct |
| click | 8.4.2 | 2026-06-24 | 2014 | Pallets | direct |
| rich | 14.2.0 | 2025-10-09 | 2019 | Textualize | direct |
| filelock | 3.25.2 | 2026-03-11 | 2014 | tox-dev | direct |
| anyio | 4.14.2 | 2026-07-12 | 2018 | agronholm | transitif |
| httpcore | 1.0.9 | 2025-04-24 | 2019 | encode | transitif |
| h11 | 0.16.0 | 2025-04-24 | 2016 | python-hyper | transitif |
| certifi | 2026.1.4 | 2026-01-04 | 2011 | certifi | transitif |
| idna | 3.15 | 2026-05-12 | 2013 | kjd | transitif |
| markdown-it-py | 4.0.0 | 2025-08-11 | 2020 | executablebooks | transitif |
| mdurl | 0.1.2 | 2022-08-14 | 2021 | executablebooks | transitif |
| pygments | 2.20.0 | 2026-03-29 | 2006 | Pygments | transitif |
| typing-extensions | 4.15.0 | 2025-08-25 | 2017 | python (CPython core) | transitif (<3.13) |
| colorama / exceptiongroup | 0.4.6 / 1.3.1 | — | 2010 / 2020 | — | conditionnels (win32 / <3.11) |
| **Extra `headless`** : gpsoauth | 2.0.0 | 2025-07-04 | 2015 | simon-weber (quasi seul mainteneur, 141★) | direct |
| ↳ pycryptodomex | 3.23.0 | 2025-05-17 | 2016 | Legrandin | transitif |
| ↳ requests, urllib3, charset-normalizer | 2.34.2 / 2.7.0 / 3.4.7 | 2026 | 2011 / 2009 / 2019 | PSF / urllib3 / jawah | transitif |

La récupération des mainteneurs depuis les pages HTML PyPI a été bloquée par le challenge anti-bot. Les mainteneurs indiqués proviennent des dépôts sources. Seuls httpx et requests ont pu être confirmés sur PyPI.

**Dépendances à signaler :**

- **`rookie-cookies` (extra `cookies`)** : fork de `rookiepy`, **publié par le mainteneur de notebooklm-py lui-même** depuis le 2026-08-09 (10 releases en 7 semaines). Il contient du code natif (Rust) et lit les bases de cookies des navigateurs. **À ne pas installer sur le serveur.**
- **Extras `headless` et `curl_cffi`** : ajoutés le 2026-06-27. Extra `android` (grpcio et protobuf épinglés) : ajouté le 2026-08-30. Ces trois extras sont récents mais ne tirent que des paquets établis.
- **gpsoauth** : petit projet à mainteneur quasi unique, mais c'est lui qui voit passer le master token. Le code a été lu : il ne contacte que `https://android.clients.google.com/auth` et n'importe que requests, urllib3 et pycryptodomex.

## 6. Historique et gouvernance — À surveiller

**Mainteneurs :**
- Le dépôt appartient à un compte **personnel** (`teng-lin`), et non à une organisation. La liste des collaborateurs est inaccessible (403).
- Sur 12 mois, environ 2 194 commits viennent de Teng Lin, environ 150 de « Claude » (agents IA du mainteneur) et 43 du deuxième humain (audichuang).
- **Bus factor = 1** : une seule personne contrôle à la fois le code et la publication.

**Releases :**
- Première release : 0.1.1, le 2026-01-09.
- 32 tags, avec un rythme d'environ 2 releases par mois ces derniers mois : 0.8.0 (08-03), 0.8.1 (08-14), 0.8.2 (09-02), 0.8.3 (09-26).
- Aucune version retirée (yank).

**Publication sur PyPI :**
- ✔ **Trusted Publishing** (OIDC, sans token d'API) et **attestations PEP 740** présentes.
- ✔ `id-token: write` est limité au job de publication, et `pypa/gh-action-pypi-publish` est épinglé par SHA.
- ✘ **L'environnement GitHub `release` n'a aucune règle de protection** : pas de reviewer, pas de restriction de branche ou de tag.
- ✘ `main` n'a pas de protection de branche visible, et les tags ne sont **pas signés**.
- ✘ Le tag `v0.8.3` a été **déplacé 3 fois** après trois publications en échec (300cbc5a → e11c91b4 → 5cb86c21 → 53fc7c50). Vérification faite : aucun changement dans `src/` ni dans `pyproject.toml` entre ces commits, seulement du CI et des tests.
- ✘ Dans le job de build, les actions first-party sont épinglées par tag, pas par SHA.

**Sécurité :**
- `SECURITY.md` est présent et le Private Vulnerability Reporting de GitHub est activé.
- Aucune issue taguée `security` et aucun advisory publié.
- Issues ouvertes liées à la sécurité : #1983, #1984 et #1986, qui concernent le durcissement OAuth du serveur **MCP**, hors périmètre GUETTEUR.
- Plusieurs issues de sécurité ont été corrigées (#1517, #1768, #1869, #2253), signe d'une hygiène active.

**20 derniers commits touchant l'auth** (`auth.py`, `_auth/`) : rien de suspect.
- Pas de nouvelle destination réseau hors Google.
- Pas de validation assouplie : la comparaison d'hôtes reste exacte.
- Pas de nouveau log de secret.
- Pas de nouveau `subprocess`.
- a0712e3d (09-02) **durcit** les permissions : `mkdir 0700` + `chmod`.

**Commits postérieurs au tag, donc hors 0.8.3, à revoir à la prochaine montée de version :**
- cf889298 ajoute `notebook.cloud.google.com` aux allowlists (hôtes de base et domaines de cookies).
- 2c56ebbe reconnaît `notebook.google` comme redirection vers la page d'accueil.

Ils répondent à l'issue ouverte **#2441** : le passage à la marque `notebook.google` casserait les RPC Web. Un **0.8.4 est donc probable à court terme**, et la version 0.8.3 pourrait être impactée fonctionnellement.

**Signaux de santé :** environ 19 500 étoiles, 36 contributeurs, CI fournie (CodeQL, dependency-audit, auth-patch-audit), Dependabot actif. La part de code généré par IA est importante.

## 7. Points suspects — récapitulatif

Aucun code malveillant, aucune obfuscation, aucune exfiltration identifiée. Les points retenus sont des risques d'usage ou de gouvernance :

| # | Point | Chemin | Gravité |
|---|---|---|---|
| 1 | Exécution d'une commande tirée de `NOTEBOOKLM_REFRESH_CMD` (option `shell=True`) | `_auth/refresh.py:522-590` | Moyenne (dépend de l'environnement) |
| 2 | Publication PyPI sans approbation : environnement `release` non protégé, tags non signés, mainteneur unique | `.github/workflows/publish.yml` | Moyenne (supply chain des **futures** versions) |
| 3 | `context.json` legacy non passé en 0600 à la migration ; copy2 puis chmod | `migration.py:57,176-182` | Faible |
| 4 | `chmod 0700` appliqué à la valeur de `NOTEBOOKLM_HOME` | `paths.py:127-143` | Faible (configuration) |
| 5 | Repr par défaut de `HopCredentials` et `AuthSnapshot` (cookies, CSRF) | `_hop_credentials.py:11`, `_web/transport/request_types.py:39` | Faible |
| 6 | Exception gpsoauth chaînée lors de l'échange initial | `_auth/mint_service.py:166` | Faible |
| 7 | `rookie-cookies` : fork récent du même auteur, natif, qui lit les cookies des navigateurs | `pyproject.toml` (extra `cookies`) | À exclure |
| 8 | Portée du master token : accès large au compte Google (scopes Android, dont `drive`) | `_android/auth.py:24-33` | Inhérent au mode headless |

## 8. Recommandation finale — **intégrable avec conditions**

1. **Épingler exactement 0.8.3, wheel uniquement, avec vérification de hash** (commandes en §9). Ne pas installer depuis la sdist, ce qui évite d'exécuter le hook de build.
2. **Extras** : aucun, ou `headless` seul si le service s'authentifie par master token. Ne jamais installer `cookies`, `browser`, `impersonate`, `mcp`, `server` ni `android` sur l'hôte 24/7. Faire le login et la capture de l'`oauth_token` sur un poste d'administration, puis copier seulement `master_token.json` ou `storage_state.json`.
3. **Neutraliser les variables d'environnement à risque** : ne pas définir `NOTEBOOKLM_REFRESH_CMD*`, `NOTEBOOKLM_AUTH_JSON`, `NOTEBOOKLM_HEADLESS_REAUTH*` ni `NOTEBOOKLM_TRANSPORT`. De préférence, les supprimer explicitement de l'environnement au démarrage du service. Garder l'environnement de GUETTEUR sous contrôle strict, car il équivaut à de l'exécution de code.
4. **Utiliser un `NOTEBOOKLM_HOME` dédié**, par exemple `/var/lib/guetteur/notebooklm`, possédé par l'utilisateur du service. Ne jamais le faire pointer vers `~` ni vers un dossier partagé. Au démarrage, vérifier les modes 0700 pour le dossier et 0600 pour `*.json`, sinon refuser de démarrer. Ne pas réutiliser un ancien `~/.notebooklm` à migrer.
5. **Utiliser un compte Google dédié**, sans données personnelles. Le master token donne un accès large et durable au compte (Drive compris). Le révoquer immédiatement en cas de doute (myaccount.google.com → Sécurité).
6. **Filtrer les sorties réseau** si l'infrastructure le permet. Autoriser seulement :
   - `notebook.google.com` et `notebooklm.google.com` ;
   - `accounts.google.com` ;
   - `android.clients.google.com` (si extra `headless`) ;
   - `*.googleusercontent.com`, `*.google.com` et `*.googleapis.com` (téléchargements).
7. **Logs** : ne jamais journaliser d'objets internes de la bibliothèque (credentials, snapshots), et laisser le logger `notebooklm` avec son filtre de redaction.
8. **Politique de mise à jour** : avant chaque montée de version, en particulier le 0.8.4 attendu pour #2441 :
   - vérifier l'attestation PEP 740 ;
   - relire le diff de `src/notebooklm/_auth/`, `auth.py`, `_env.py`, `_artifact/_download_client.py` et `pyproject.toml` ;
   - relancer `pip-audit`.

## 9. Épinglage dans GUETTEUR

Version auditée : **`notebooklm-py==0.8.3`**
Hash du wheel : `sha256:7e3e02057b3acf354d3dbc337c08869d2a4954c9c324f3271e272236cfcc2bfc`

**Option A — projet géré par uv** (recommandé, le lock contient les hashes de tout le graphe) :

```bash
cd /chemin/vers/guetteur
uv add "notebooklm-py==0.8.3"            # ou: uv add "notebooklm-py[headless]==0.8.3"
uv lock
# Vérifier que le lock référence bien le wheel audité :
grep -A12 '^name = "notebooklm-py"' uv.lock | grep 7e3e02057b3acf354d3dbc337c08869d2a4954c9c324f3271e272236cfcc2bfc
# Installation en production, strictement depuis le lock, wheels uniquement :
uv sync --frozen --no-dev --no-build
```

**Option B — pip + requirements avec hashes** :

```bash
cd /chemin/vers/guetteur
cat > requirements.in <<'EOF'
notebooklm-py==0.8.3
# notebooklm-py[headless]==0.8.3   # si auth par master token
EOF
uv pip compile requirements.in --generate-hashes --only-binary :all: -o requirements.txt
# Contrôle : le hash du wheel audité doit figurer dans requirements.txt
grep -q 7e3e02057b3acf354d3dbc337c08869d2a4954c9c324f3271e272236cfcc2bfc requirements.txt && echo "hash OK"
pip install --require-hashes --only-binary :all: --no-deps -r requirements.txt
```

La ligne qui doit apparaître pour le paquet audité :

```
notebooklm-py==0.8.3 \
    --hash=sha256:7e3e02057b3acf354d3dbc337c08869d2a4954c9c324f3271e272236cfcc2bfc
```

**Vérification optionnelle de l'attestation PEP 740** avant déploiement :

```bash
uvx pypi-attestations verify pypi --repository https://github.com/teng-lin/notebooklm-py \
    pypi:notebooklm_py-0.8.3-py3-none-any.whl
```

---

### Annexes (dans `audit-notebooklm/`)

- `repo/` : clone git (HEAD `cf889298`) ; `tag/` : export de `v0.8.3`.
- `dist/` : sdist et wheel PyPI, plus leurs extractions (`sdist/`, `wheel/`).
- `venv/` : dépendances verrouillées sans extra (paquet audité non installé).
- `requirements-locked.txt`, `requirements-locked-headless.txt` : graphes exportés avec hashes, utilisés pour `pip-audit`.
