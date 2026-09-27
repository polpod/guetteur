# Archivage NotebookLM (Lot 3)

Après chaque envoi réussi, GUETTEUR peut ajouter l'URL de la vidéo comme **source** dans un
notebook Google NotebookLM et y attacher une **note** contenant le résumé Markdown. L'échec
d'un archivage ne bloque jamais l'envoi (avertissement dans les logs, `archived_at` reste
NULL, à rattraper avec `guetteur archive --pending`).

## Sécurité — non négociable

L'intégration suit strictement les conditions de l'audit
[`docs/audit-notebooklm/RAPPORT.md`](audit-notebooklm/RAPPORT.md) §8 :

- **Version épinglée** : `notebooklm-py==0.8.3`, wheel uniquement, sha256 audit-vérifié dans
  `uv.lock`. Aucun autre extra de `notebooklm-py` n'est installé (jamais `cookies`,
  `browser`, `android`, `headless`, `mcp`, `server`, `impersonate`).
- **Variables interdites** dans l'environnement du service : toute variable
  `NOTEBOOKLM_REFRESH_CMD*`, `NOTEBOOKLM_AUTH_JSON`, `NOTEBOOKLM_HEADLESS_REAUTH*` ou
  `NOTEBOOKLM_TRANSPORT` fait échouer l'archivage immédiatement (défense en profondeur :
  elles sont aussi retirées avant tout appel à la bibliothèque). Voir la liste exacte dans
  `guetteur.archive.base.FORBIDDEN_ENV_VARS`.
- **`NOTEBOOKLM_HOME` dédié** : `/opt/guetteur/data/nlm` en LXC, `~/.guetteur-nlm` en dev.
  Dossier en 0700 et fichiers JSON en 0600 obligatoires ; le refus indique la commande
  `chmod` à passer. GUETTEUR ne modifie jamais les permissions d'un chemin qui n'est pas ce
  dossier.
- **Compte Google dédié** (jamais un compte personnel — audit §7.8). `guetteur doctor`
  marque KO si l'email renvoyé par l'API diffère de `archive.account`.
- **Logs redactés** : cookies, jetons Google et en-têtes sensibles sont masqués dans les
  messages d'erreur avant journalisation (`guetteur.archive.base.redact`).

## Configuration

Section `[archive]` dans `config.toml` :

| Clé | Défaut | Description |
|---|---|---|
| `enabled` | `false` | Active l'archivage NotebookLM (extra `notebooklm` obligatoire). |
| `notebook_name` | `"Veille YouTube"` | Nom du notebook cible. « (2) », « (3) »… ajoutés au-delà du quota. |
| `account` | *(vide)* | Compte Google dédié attendu ; `guetteur doctor` compare à l'email renvoyé par l'API. |
| `home` | `/opt/guetteur/data/nlm` (LXC) ou `~/.guetteur-nlm` (dev) | Emplacement du profil dédié, en 0700. |
| `max_sources_per_notebook` | `45` | Au-delà, création automatique du notebook suivant. |
| `pinned_version` | `"0.8.3"` | Version audit-fixée ; `guetteur doctor` marque KO si `pip show` renvoie autre chose. |

## Installation

Le login initial se fait **hors production** (poste WSL avec navigateur), puis on transfère
le profil vers le LXC — `guetteur-nlm-refresh.timer` prend le relais toutes les 6 h. La
procédure complète (commandes de login, transfert du profil, montée de version, filtrage
sortant nftables) est décrite dans le rapport d'audit §8.

Vérification après installation : `guetteur doctor` doit afficher 4 lignes NotebookLM OK
(version, permissions du home, session, compte). Le timer `guetteur-nlm-refresh.timer`
appelle `notebooklm auth refresh --quiet` sous l'utilisateur `guetteur`.

## Commandes

| Commande | Rôle |
|---|---|
| `guetteur archive --video-id X` | Archive une vidéo précise (doit être `sent`). |
| `guetteur archive --pending` | Archive toutes les vidéos `sent` sans `archived_at` (par `sent_at` asc). |
| `guetteur status` | Une colonne `archive` (oui / non / —) est ajoutée. |
| `guetteur doctor` | 4 lignes NotebookLM si `archive.enabled = true`. |

## Filtrage sortant (optionnel mais recommandé)

Une règle nftables restreint les sorties de l'utilisateur `guetteur` aux seuls domaines
utiles (audit §6.6 + GUETTEUR) : `notebook.google.com`, `notebooklm.google.com`,
`accounts.google.com`, `android.clients.google.com`, `*.googleusercontent.com`,
`*.google.com`, `*.googleapis.com`, `www.youtube.com`, `api.telegram.org`,
`graph.facebook.com`, `api.anthropic.com`, `deb.debian.org`, `github.com`.

## Montée de version

Avant chaque `uv lock` qui remonte `notebooklm-py` (procédure détaillée dans le rapport
d'audit §8.8) :

1. Attestation PEP 740 vérifiée.
2. Diff relu sur `src/notebooklm/_auth/`, `auth.py`, `_env.py`,
   `_artifact/_download_client.py` et `pyproject.toml` du nouveau tag.
3. Commits post-tag qui touchent l'auth revus.
4. `pip-audit` sur le nouveau lock.
5. `archive.pinned_version` et `install-lxc.sh:NLM_HASH` mis à jour.
