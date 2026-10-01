# Quadringent — Contrat control plane v2 (« chantier 3 »)

Document de conception historique. S'appuie sur `docs/plans/2026-09-23-produit-fini-design.md` (§2, 3, 4), `docs/api.md`, `docs/architecture.md`, et le code de `src/quadringent_control_plane/`.

## Constat de départ (ce qui existe réellement aujourd'hui)

Avant de concevoir `/v2`, il faut être précis sur l'écart avec `/v1`, parce qu'il conditionne tout le plan de migration :

- **Pas de base de données.** `repository.py` (`ProjectionRepository`) est un projecteur **en mémoire**, rafraîchi par polling, qui lit des documents JSON depuis `file://`, `http(s)://` ou `s3://` (fonction `_read_document`, bornée à `MAX_DOCUMENT_BYTES = 2 MiB`) et calcule une empreinte SHA-256 (`_canonical_json`) pour incrémenter une révision. Aucune écriture de pipeline n'existe dans ce module : c'est strictement une lecture de documents produits ailleurs (le lecteur Java/Python, les runtimes de flotte).
- **Écritures = fichiers JSON atomiques mono-processus.** `fleet_runtime_store.AtomicJsonStateStore` fait un `write-tmp + fsync + os.replace` sur un seul fichier local (`fleet-prepare.json`, `fleet-history.json`, `fleet-pause.json`, `connections.json`, `fleet-run.json`, `fleet-catalog.json`…). Aucun verrou distribué : le commentaire dans `connections.py` est explicite — *« un seul process control plane écrit ce fichier »*. C'est un modèle **single-writer, single-site-per-process**.
- **Un seul site par processus.** `quadringent.site_config.current()` résout une configuration de site globale (contextvar) ; `fleet_composition.py` a même une exception de nommage codée en dur pour `LEGACY_SITE_ID = "example-corp"`, le seul déploiement en prod avant le multi-site. Il n'y a donc aujourd'hui ni notion de tenant/organisation, ni modèle multi-connexions actif dans le control plane (seulement une déclaration `connections.json` dormante, `lifecycle_state: declared_not_in_service`, jamais raccordée à un provisionnement réel).
- **Pas d'utilisateurs, pas de jetons.** `auth.py` ne connaît que des en-têtes de confiance posés par un proxy externe (`oauth2-proxy`, forward-auth) ; aucun compte, aucun stockage de rôle, aucun jeton d'agent. `authorize()` ne distingue que viewer/operator/admin par appartenance à un groupe IdP.
- **Actions = 5 verbes fermés** (`prepare`, `start`, `pause`, `resume`, `refresh`) sur un seul objet « pipeline », avec confirmation textuelle (`f"{ACTION} {SITE_ID} {ENV}"`) et exécuteur injecté (`FleetActionExecutor`) qui retourne un triptyque `intent/execution/observed_effect` (`actions.py::_normalize_executor_stages`). Pas de `dry_run`, pas d'`Idempotency-Key`, pas de confirmations différées/liftables par un tiers.
- **Audit = fichier journal append-only** (`audit.py::ActionAuditLog`), sans notion d'acteur agent/MCP, sans rétention interrogeable par API.
- **Pas de MCP, pas de CLI publique orientée agent** au sens du produit fini (le `cli.py` actuel est le lanceur du serveur HTTP, pas un client).
- **Coûts et télémétrie** (`costs.py`, `telemetry.py`, `infrastructure_costs.py`) sont déjà conformes en esprit au design (mesuré/estimé/absent, fraîcheur, jamais 0 par défaut) — ce sont des briques à **réutiliser**, pas à jeter.

Conséquence directe pour le chantier 3 : `/v2` n'est pas un simple renommage de `/v1`. C'est l'ajout d'un vrai state store transactionnel multi-tenant (Postgres embarqué), d'un modèle d'identité (users/roles/tokens), d'un modèle de confirmation asynchrone, et l'élargissement du modèle « un pipeline » vers « sources → destinations → tables → pipelines » — en gardant la discipline produit déjà en place : jamais de secret en clair, jamais de valeur inventée (`null` = non mesuré), fail-closed partout.

---

## 1. Modèle de ressources et machines à états

### 1.1 Ressources

```
Organisation (implicite v1 : une seule, celle du control plane)
 └─ Source (IBM i)            : host, account, secret_ref (chiffré), tls_fingerprint, detected_timezone, detected_version
 └─ Destination (Snowflake)    : account_id, key_pair_ref, setup_script, verification_state
 └─ Table (découverte source)  : schema, name, journal_status, key_strategy, discovered_row_count, discovered_size
 └─ Pipeline (Source×Table×Destination) : state, lag, throughput, rows_source, rows_snowflake, last_arrival
 └─ Action                     : verb, scope (table|source|destination|all), state, dry_run_plan, idempotency_key
 └─ Confirmation               : action_id, reason, threshold, expires_at, lifted_by
 └─ Alert / Webhook endpoint
 └─ User (admin|reader) / OIDC binding
 └─ AgentToken (scope, source restriction)
 └─ AuditRecord
```

Cette hiérarchie reprend `SourceDescriptor` (`model.py`) et `ConnectionRecord` (`connections.py`) en les faisant évoluer : `SourceDescriptor.id/evidence_kind/environment/origin` devient les colonnes d'identité de `sources`, et `ConnectionRecord` (déjà quasi une table candidate : `connection_id`, `site_id`, `ibmi_host`, `snowflake_account`, `tables`, `secret_ref_*`) devient la jonction `sources` + `destinations` + `pipelines`.

### 1.2 État d'une table/pipeline

Le design produit (§3) énumère : `copying, live, paused, attention, stopped`. Le code actuel a un état plus riche (`PipelineProjection.status` : `healthy, recovering, degraded, incident, planned_stop, awaiting_resume, unknown` — voir `ui/src/domain/operator.ts::stateCopy`) qui reste la **couche d'observation** (dérivée des preuves, jamais mutée directement). Le state store v2 introduit un état *déclaré* distinct de l'état *observé* :

```
declared_state (piloté par actions, persisté)        observed_state (dérivé des preuves, projeté)
  not_started                                           —
  copying          ← start                              healthy | recovering | degraded
  live             ← copie initiale terminée             healthy | recovering | degraded | incident
  paused           ← pause                               planned_stop
  attention        ← incident détecté (automatique)       incident | awaiting_resume
  stopped          ← remove / stop définitif              unknown
```

Transitions autorisées (`declared_state`) :

```
not_started --start--> copying --(bootstrap achevé, preuve reader)--> live
copying --pause--> paused
live --pause--> paused
paused --resume--> copying|live (selon position du checkpoint)
live|copying|paused --attention--> attention (transition automatique, jamais déclenchée par une action humaine)
attention --resume--> copying (reprise explicite, cf. `_reconcile_capture_recovery` dans repository.py qui sait déjà distinguer « incident » de « prêt à reprendre »)
copying|live|paused|attention --remove--> stopped (terminal, irréversible)
copying|live|paused|attention --restart_initial_copy--> copying (réinitialise la position, action sensible → confirmation)
```

`attention` correspond à l'observation `awaiting_resume`/`incident` déjà produite par `_reconcile_capture_recovery` — c'est la bonne base à réutiliser : ne pas réinventer la détection, mais la brancher sur la nouvelle state machine déclarative.

### 1.3 Cycle de vie d'une action (avec `pending_confirmation`)

```
requested → [dry_run? → planned (terminal, pas d'effet)]
requested → validated → (confirmation nécessaire ?)
  non → executing → succeeded | failed
  oui → pending_confirmation → (approuvé avant expiration ?)
          approuvé   → executing → succeeded | failed
          refusé     → rejected (terminal)
          expiré     → expired (terminal)
pending_confirmation --liftable_by_token--> executing   (jeton scope=admin avec pré-autorisation déclarée)
```

Actions déclenchant systématiquement `pending_confirmation` (§4 du design) : suppression d'une source, recopie complète (`restart_initial_copy` sur une table déjà `live`), changement de destination, toute action dont le `dry_run` estime un coût au-delà d'un seuil configurable par organisation.

`gate.try_begin/end` de `actions.py::PipelineActionGate` reste le bon mécanisme d'exclusion mutuelle par ressource ; il est étendu pour couvrir n'importe quelle portée (`table|source|destination|all`), pas seulement `pipeline_id`.

---

## 2. Endpoints `/v2` (OpenAPI 3.1)

Conventions transverses :

- Pagination : `?limit=50&cursor=<opaque>` sur toutes les listes ; réponse `{"items":[...],"next_cursor":string|null}`.
- Filtre : `?filter[state]=live&filter[source_id]=...` (style JSON:API léger, cohérent avec le typage strict déjà pratiqué dans `connections.py` — regex fermées, aucun champ non listé accepté).
- Toute écriture : header `Idempotency-Key` (requis pour POST/PATCH/DELETE, UUID ou chaîne ≤128, rejouable 24 h — table `idempotency_keys`) ; corps `dry_run: bool` optionnel, défaut `false`.
- Réponse d'écriture : `{"before": {...}, "after": {...}, "verify": {"method":"GET","path":"/v2/..."}, "dry_run": {...}|null}`.
- Erreurs : `{"error":{"code":"...", "message":"...", "next_action":"...", "retryable": bool}}`, catalogue stable (voir 2.6).
- Auth scope par route : `read` (GET), `operate` (POST/PATCH non sensibles), `admin` (users, tokens, destinations, suppression).

### 2.1 Sources

| Méthode | Route | But | Scope | Idempotent | dry_run | confirmation |
|---|---|---|---|---|---|---|
| GET | `/v2/sources` | Lister | read | — | — | — |
| POST | `/v2/sources` | Créer (host, account, secret déclaré via ref ou valeur chiffrée à la volée) | operate | oui (clé) | oui (teste joignabilité sans persister) | non |
| GET | `/v2/sources/{id}` | Détail (tls_fingerprint, detected_timezone/version) | read | — | — | — |
| POST | `/v2/sources/{id}/test` | Re-teste réseau/TLS/auth/horloge (équivalent « Tester » du design §2.1) | operate | oui | non (c'est déjà un essai) | non |
| PATCH | `/v2/sources/{id}` | Modifier host/secret | operate | oui | oui | non sauf changement de secret admin |
| DELETE | `/v2/sources/{id}` | Supprimer (cascade tables/pipelines) | admin | oui | oui | **oui** |

Corps de création (sketch) :
```json
{"display_name":"...", "ibmi_host":"...", "ibmi_user":"...", "secret": {"kind":"inline"|"k8s_ref","value_or_ref":"..."}, "tls": {"trust":"system"|"pinned","fingerprint":"..."}}
```
Réutilise les regex de `connections.py` (`_IBMI_HOST`, `_IBMI_USER`, `_K8S_NAME`) et le principe « aucun mot de passe en clair persisté » (`ConnectionsStore.create` n'a structurellement pas de paramètre secret en clair) — en v2, le secret inline est chiffré immédiatement (KMS/`age`/Fernet clé rotée) avant tout `INSERT`, jamais loggé (`audit.py` filtre déjà les secrets, à conserver).

### 2.2 Destinations

| Méthode | Route | But | Scope | dry_run | confirmation |
|---|---|---|---|---|---|
| GET | `/v2/destinations` | Lister | read | — | — |
| POST | `/v2/destinations` | Créer : génère paire de clés + script SQL (`SETUP.sql` téléchargeable) | operate | oui (génère le script sans le committer) | non |
| GET | `/v2/destinations/{id}` | Détail + `verification_state` | read | — | — |
| POST | `/v2/destinations/{id}/verify` | Relance la vérification (rôle, warehouse, clé) | operate | non | non |
| PATCH | `/v2/destinations/{id}` | Changer compte Snowflake d'un pipeline | operate | oui | **oui** (« changement de destination » listé explicitement en §4) |
| DELETE | `/v2/destinations/{id}` | admin | oui | **oui** |

### 2.3 Tables

| Méthode | Route | But | Scope |
|---|---|---|---|
| GET | `/v2/sources/{id}/tables` | Catalogue découvert (journalisation, clé, CL fix commands) | read |
| POST | `/v2/sources/{id}/tables/refresh` | Relance la découverte catalogue | operate |
| GET | `/v2/tables/{id}` | Détail (clé choisie, RRN fallback) | read |
| PATCH | `/v2/tables/{id}` | Choix de clé (`unique_index`/`rrn`) | operate |
| POST | `/v2/tables/{id}/pipeline` | Démarre le pipeline pour cette table (équivaut à `start`) | operate — dry_run oui |

`journal_status` et `cl_fix_commands` reprennent le rôle de `onboarding.py::evaluate_onboarding` côté « journal » (blocages `"Le journal IBM i est obligatoire"`, avertissement RRN déjà rédigé — cf. §294 « sans clé métier : identifiées par leur position physique (RRN) »).

### 2.4 Pipelines & Actions

| Méthode | Route | But | dry_run | confirmation |
|---|---|---|---|---|
| GET | `/v2/pipelines` | Liste (remplace `/v1/pipelines`) | — | — |
| GET | `/v2/pipelines/{id}` | Détail | — | — |
| GET | `/v2/pipelines/{id}/metrics?window=1h\|24h` | Séries retard/débit — reprend `LagSeriesProjection` | — | — |
| GET | `/v2/pipelines/{id}/logs?since=&level=&correlate_incident=` | Journaux filtrés, jamais de donnée de ligne | — | — |
| POST | `/v2/pipelines/{id}/actions/pause` | oui | non |
| POST | `/v2/pipelines/{id}/actions/resume` | oui | non |
| POST | `/v2/pipelines/{id}/actions/restart_initial_copy` | oui | **oui** |
| POST | `/v2/pipelines/{id}/actions/replay?from_sequence=&to_sequence=` | oui | oui si coût > seuil |
| DELETE | `/v2/pipelines/{id}` (retirer la table) | oui | **oui** |
| POST | `/v2/sources/{id}/actions/pause` \| `/resume` (toutes les tables d'une source) | oui | non pour pause, oui pour un pause massif au-delà d'un seuil configurable |
| POST | `/v2/destinations/{id}/actions/pause` \| `/resume` | oui | idem |
| POST | `/v2/actions/pause_all` \| `/v2/actions/resume_all` | oui | non |

Corps action (remplace le triplet `fleet_id/environment/confirmation` de `/v1`, désormais implicite via l'auth + scope de ressource) :
```json
{"dry_run": false, "confirmation_token": "cf_..."?}
```
Réponse reprend le triptyque déjà présent (`intent/execution/observed_effect`, `actions.py::_normalize_executor_stages`) — conservé tel quel, il est solide et déjà testé (`tests/test_control_plane_actions.py`) ; il vient simplement peupler `after` dans l'enveloppe générique before/after.

### 2.5 Confirmations, coûts, alertes, users, tokens

| Méthode | Route | But |
|---|---|---|
| GET | `/v2/confirmations?state=pending_confirmation` | Liste des confirmations en attente (cockpit + lien direct) |
| POST | `/v2/confirmations/{id}/approve` \| `/reject` | Approbation humaine (admin ou operator selon action), ou via lien signé non authentifié à durée limitée |
| GET | `/v2/costs?scope=connection\|table&window=` | Mesuré/estimé/absent + provenance + fraîcheur — reprend `costs.py`/`infrastructure_costs.py` tel quel |
| GET/POST | `/v2/alerts`, `/v2/webhooks` | CRUD webhooks signés |
| GET/POST | `/v2/users`, `POST /v2/users/{id}/activate` | admin/reader, lien d'activation premier admin |
| GET/POST/DELETE | `/v2/agent-tokens` | scopes read/operate/admin, restriction par source |
| GET | `/v2/audit?actor_kind=human\|agent&...` | Journal interrogeable (remplace le fichier append-only brut) |

### 2.6 Catalogue d'erreurs initial

| Code | HTTP | Sens | Action suivante |
|---|---|---|---|
| `invalid_request` | 400 | corps hors schéma | corriger le corps, revoir OpenAPI |
| `idempotency_key_conflict` | 409 | même clé, corps différent | changer de clé ou renvoyer le corps identique |
| `wrong_confirmation` | 403 | jeton de confirmation absent/expiré | relancer avec `dry_run` puis confirmer |
| `pending_confirmation_required` | 409 | action sensible sans confirmation | consulter `/v2/confirmations` |
| `capability_unavailable` | 409 | ressource pas prête pour cette action | relire l'état de la ressource |
| `action_in_progress` | 409 | verrou déjà pris (`PipelineActionGate`) | réessayer après |
| `insufficient_role` | 403 | rôle/scope insuffisant | demander l'élévation à un admin |
| `wrong_environment` | 403 | ressource hors périmètre du jeton | vérifier la restriction de source du jeton |
| `not_found` | 404 | ressource absente | vérifier l'identifiant |
| `store_unavailable` | 503 | Postgres indisponible | réessayer, alerter l'admin |
| `executor_unavailable` | 503 | pas d'exécuteur Kubernetes branché | vérifier le déploiement du control plane |
| `internal_error` | 500 | résultat d'exécuteur hors contrat (`MalformedExecutor`) | signaler, ne jamais rejouer aveuglément |

---

## 3. SSE et webhooks

### 3.1 SSE

Garde le contrat `/v1/events` (`stream.cursor`, `projection.updated`, `projection.reset`, `Last-Event-ID` reprenant un entier de révision — `ProjectionRepository.events_after`) et l'étend avec des types nommés pour ne plus forcer un rechargement complet à chaque petit changement :

```
event: pipeline.state_changed      data: {"pipeline_id":"...","from":"copying","to":"live","revision":N}
event: action.pending_confirmation data: {"action_id":"...","resource":"...","reason":"...","expires_at":"..."}
event: action.completed            data: {"action_id":"...","state":"succeeded","revision":N}
event: alert.fired | alert.resolved data: {"alert_id":"...","severity":"..."}
event: stream.cursor | projection.updated | projection.reset   (conservés tels quels pour compat UI)
```

Le client recharge la ressource concernée par HTTP (comme aujourd'hui : *« le flux ne transporte pas les pipelines »*) — principe conservé pour ne jamais faire du SSE une source de vérité.

### 3.2 Webhooks signés

- Enregistrement : `POST /v2/webhooks {"url":"https://...", "events":["pipeline.state_changed", "alert.fired"], "secret": <généré côté serveur, affiché une seule fois>}`.
- Signature HMAC-SHA256 sur `timestamp + "." + body`, header `X-Quadringent-Signature: t=<unix>,v1=<hex>`, tolérance 5 minutes.
- Anti-rejeu : `event_id` (UUID) + `delivered_at`, table `webhook_deliveries(event_id, endpoint_id)` avec contrainte unique — un événement déjà livré à un endpoint n'est jamais rejoué automatiquement (rejouable manuellement via `POST /v2/webhooks/{id}/redeliver/{event_id}`, action `operate`).
- Retry : backoff exponentiel borné (5 tentatives), passage `disabled` après échecs consécutifs, visible dans `/v2/webhooks/{id}`.

---

## 4. Serveur MCP

Le serveur MCP est un **client interne de `/v2`** (aucune logique dupliquée), embarqué dans le même processus control plane, exposé sur un endpoint séparé (stdio pour usage local, HTTP/SSE pour agents distants avec jeton).

### 4.1 Outils (sketch)

| Nom | Description agent | Entrée | Scope |
|---|---|---|---|
| `list_pipelines` | « Liste les pipelines de réplication IBM i → Snowflake avec leur état, retard et débit. Utilise `state` pour filtrer. » | `{state?, source_id?}` | read |
| `get_pipeline` | « Détail d'un pipeline : métriques, dernières lignes de log, preuves. » | `{pipeline_id}` | read |
| `pause_pipeline` / `resume_pipeline` | « Met en pause / relance la réplication d'une table. Toujours appeler avec `dry_run:true` d'abord pour voir l'effet annoncé. » | `{pipeline_id, dry_run}` | operate |
| `restart_initial_copy` | « Relance une copie complète d'une table. Coûteux et potentiellement destructif pour le miroir : nécessite une confirmation humaine que cet outil ne peut pas lever seul. » | `{pipeline_id, dry_run}` | operate |
| `replay_journal_range` | « Rejoue une plage de séquences journal. » | `{pipeline_id, from_sequence, to_sequence, dry_run}` | operate |
| `remove_table` | « Retire définitivement une table du périmètre répliqué. Action destructive, confirmation humaine obligatoire. » | `{pipeline_id, dry_run}` | admin |
| `create_source` / `test_source` | onboarding assisté par agent | … | operate/admin |
| `list_pending_confirmations` | « Liste les actions en attente d'approbation humaine, avec le lien à transmettre. » | `{}` | read |
| `get_costs` | « Coûts mesurés/estimés par connexion ou table, avec leur fraîcheur. » | `{scope, id, window}` | read |

Chaque outil qui correspond à une action sensible **ne peut pas la faire aboutir seul** : le résultat renvoyé quand une confirmation est requise contient `{"status":"pending_confirmation","confirmation_id":"...","approve_url":"https://.../confirmations/{id}"}` — l'agent doit soit attendre (poller `list_pending_confirmations`), soit transmettre le lien à un humain. Le jeton d'agent peut porter une pré-autorisation explicite (« peut lever ses propres confirmations jusqu'à seuil X ») déclarée à la création du jeton, jamais implicite.

### 4.2 Ressources MCP

- `quadringent://docs/api` (le document OpenAPI lui-même)
- `quadringent://docs/errors` (catalogue §2.6)
- `quadringent://state/overview` (équivalent `/v2/pipelines` en lecture, sans passer par un tool call)
- `quadringent://docs/llms.txt`

### 4.3 `llms.txt` (plan)

```
# Quadringent control plane
> API de pilotage d'une réplication IBM i → Snowflake.

## Concepts
- Source, Destination, Table, Pipeline, Action, Confirmation
- Toute écriture sensible passe par pending_confirmation

## Docs
- /v2/openapi.json : contrat complet
- /llms-full.txt : version longue avec exemples

## Règles pour un agent
- Toujours dry_run avant une action non triviale
- Ne jamais fournir de secret en clair
- Vérifier /v2/confirmations avant de conclure qu'une action a échoué
```

---

## 5. Arborescence CLI

```
quadringent sources list|show|create|test|delete
quadringent destinations list|show|create|verify|delete
quadringent tables list --source <id>|show|set-key
quadringent pipelines list|show
quadringent pipelines pause|resume|restart-initial-copy|replay|remove <id> [--dry-run] [--confirm <token>]
quadringent metrics <pipeline-id> --window 1h|24h
quadringent logs <pipeline-id> [--since] [--incident]
quadringent costs [--scope connection|table] [--id]
quadringent confirmations list|approve|reject
quadringent alerts list|create|delete
quadringent webhooks list|create|delete|redeliver
quadringent users list|create|activate
quadringent tokens create|list|revoke --scope read|operate|admin [--source <id>]
quadringent audit tail [--actor-kind human|agent]
quadringent events   # streaming SSE en JSON lines
```

Sortie JSON systématique (`--output json` implicite/seul mode, cohérent avec le nom `quadringent` déjà utilisé pour `quadringent-control-plane`/`quadringent-cost-collect` dans `pyproject.toml`/`.venv/bin`). Chaque sous-commande d'écriture porte `--dry-run` et `--idempotency-key` (générée automatiquement si omise, affichée pour permettre un rejeu volontaire).

---

## 6. AuthN/Z

### 6.1 Utilisateurs et rôles

Table `users` : `id, email, role(admin|reader), oidc_subject NULL, created_at, activated_at NULL`. Premier admin : `POST /v2/setup/first-admin` génère un lien à usage unique (`activation_tokens`, TTL 24h, hashé en base comme les jetons d'agent — voir 6.3) affiché en sortie CLI d'installation, cohérent avec *« affiche l'URL et le lien d'activation admin »* du design §1.

### 6.2 OIDC

Optionnel, configuré par organisation (`oidc_config: issuer, client_id, client_secret_ref`). Le flux standard (Authorization Code + PKCE) alimente `users.oidc_subject` ; en son absence, le mode `auth.py` actuel (en-têtes de proxy de confiance) reste utilisable en secours pour les déploiements qui préfèrent leur propre IdP en frontal — c'est déjà l'esprit du commentaire *« Ce mode reste optionnel »* dans `auth.py`.

### 6.3 Jetons d'agent

- Format : `qdt_<scope_prefix>_<32 octets aléatoires base62>`, ex. `qdt_op_...` / `qdt_rd_...` / `qdt_ad_...` (préfixe utile pour le scanning de fuite, comme les jetons GitHub/Stripe).
- Stockage : seul le hash (`argon2id`, ou `sha256` avec pepper serveur si argon2 indisponible) est persisté, jamais la valeur en clair (`agent_tokens(id, name, scope, hash, source_restriction[], created_by, expires_at, revoked_at, last_used_at)`).
- Rotation : `POST /v2/agent-tokens/{id}/rotate` invalide l'ancien hash et retourne une nouvelle valeur en clair une seule fois (jamais relisible ensuite — même discipline que `secret_ref` dans `connections.py`).
- Expiration : `expires_at` optionnelle mais recommandée ; un jeton sans expiration doit être explicitement marqué `never_expires: true` pour être créé (fail-closed par défaut).
- Restriction par source : `source_restriction` vide = toutes les sources ; sinon liste d'IDs, vérifiée à chaque appel (même logique que le contrôle d'environnement déjà fait par `actions.py::_normalized_environment`).

### 6.4 Flux de confirmation

1. Un `POST` d'action sensible sans `confirmation_token` retourne `409 pending_confirmation_required` + crée une ligne `confirmations(id, action_ref, requested_by(actor), reason, risk_estimate, expires_at, state)`.
2. Un humain (cockpit ou lien signé `/confirmations/{id}?token=...` envoyé par email/webhook) `approve`/`reject`.
3. Un jeton pré-autorisé peut approuver via `POST /v2/confirmations/{id}/approve` s'il porte le scope requis et que l'action est dans sa liste pré-autorisée.
4. L'audit distingue explicitement qui a demandé et qui a approuvé.

### 6.5 Audit

Table `audit_records` :
```
id, at, actor_kind(human|agent), actor_id, actor_display (nom du token / email),
mcp_client (nom déclaré par le client MCP, nullable), action, resource_type, resource_id,
request_id, idempotency_key, dry_run(bool), confirmation_id NULL, status, before JSONB, after JSONB
```
Remplace `audit.py::ActionAuditLog` (fichier append-only local) par une table interrogeable, tout en gardant le principe *« aucun secret, corps HTTP ou détail d'exception brut »* — validation stricte des champs avant insertion, comme le fait déjà `_append` avec un chemin `0o600`.

---

## 7. Schéma Postgres et migration

### 7.1 Schéma (vue d'ensemble, migrations Alembic ou équivalent SQL versionné)

```sql
organizations(id, name, created_at)                          -- v1 = organisation implicite unique
users(id, org_id, email, role, oidc_subject, created_at, activated_at)
activation_tokens(id, user_id, hash, expires_at, used_at)
agent_tokens(id, org_id, name, scope, hash, source_restriction jsonb, created_by, expires_at, revoked_at, last_used_at)

sources(id, org_id, display_name, ibmi_host, ibmi_user, secret_ciphertext, tls_fingerprint,
        detected_timezone, detected_version, created_at, updated_at)
destinations(id, org_id, snowflake_account, key_pair_ciphertext, setup_script, verification_state, created_at)
tables(id, source_id, schema_name, table_name, journal_status, key_strategy, discovered_row_count,
       discovered_size_bytes, discovered_at)
pipelines(id, table_id, destination_id, declared_state, created_at, updated_at)
pipeline_events(id, pipeline_id, at, kind, payload jsonb)      -- alimente SSE + /metrics + /logs sans relire k8s

actions(id, pipeline_id NULL, source_id NULL, destination_id NULL, scope, verb, requested_by,
        idempotency_key, dry_run bool, state, created_at, completed_at)
confirmations(id, action_id, reason, risk_estimate jsonb, expires_at, state, approved_by, approved_at)
idempotency_keys(key, actor_id, request_hash, response jsonb, created_at)

audit_records(...)                                              -- §6.5
webhooks(id, org_id, url, secret_hash, events jsonb, state, created_at)
webhook_deliveries(event_id, endpoint_id, delivered_at, status, attempt_count)
alerts(id, org_id, kind, condition jsonb, state, created_at)

costs_snapshots(id, scope_type, scope_id, window_start, window_end, status, amount, currency,
                 basis, collected_at)                            -- reprend costs.py/infrastructure_costs.py
```

Index recommandés : `pipelines(declared_state)`, `pipeline_events(pipeline_id, at)`, `audit_records(actor_kind, at)`, `idempotency_keys(key)` unique.

### 7.2 Stratégie de migration depuis le stockage v1

Le stockage v1 n'est pas une base de données à migrer schéma-par-schéma : c'est un ensemble de petits documents JSON locaux. La migration est donc un **import ponctuel** au premier démarrage v2, pas une migration continue :

1. `connections.json` (`connections.py::ConnectionsStore`) → une ligne `sources` + `destinations` + N lignes `tables`/`pipelines` par `ConnectionRecord`, `lifecycle_state: declared_not_in_service` devenant `declared_state: not_started`.
2. `fleet-prepare.json`/`fleet-history.json`/`fleet-pause.json`/`fleet-run.json` restent lus **tels quels par les runtimes existants** (`fleet_prepare_runtime.py`, `fleet_history_runtime.py`, `fleet_pause_runtime.py`) — ce sont des documents opérationnels produits par les Jobs Kubernetes, pas des déclarations utilisateur. Le control plane v2 les **projette** vers `pipeline_events`/`declared_state` en tâche de fond (adaptateur, pas migration ponctuelle), exactement comme `repository.py::_project_source` le fait déjà en mémoire aujourd'hui.
3. `audit.log` (fichier `ActionAuditLog`) est importé une fois en `audit_records` (lecture ligne à ligne, un acteur `human` par défaut faute de meilleure information historique), puis le fichier continue d'exister en parallèle un temps (double-écriture) le temps de valider Postgres en production, avant suppression.

### 7.3 Compatibilité `/v1`

- **Garder** : `GET /v1/*` en proxy translucide vers les équivalents `/v2` le temps d'une dépréciation annoncée (au moins une version majeure), pour ne pas casser l'UI existante pendant la bascule progressive.
- **Retirer** dès que l'UI/CLI v2 est livrée : `POST /v1/pipelines/{id}/actions/{action}` (remplacé par le modèle d'action générique v2, incompatible côté confirmation/dry_run).
- **Supprimer** immédiatement (jamais porté en v2) : rien d'identifié comme obsolète métier — tout le contrat v1 a un équivalent v2 plus riche.

---

## 8. Mapping v1 → v2

| Module v1 | Composant v2 | Traitement |
|---|---|---|
| `model.py` (dataclasses de projection) | Modèle de lecture `/v2/pipelines` | **Réutiliser** tel quel comme couche de projection (observed_state), alimentée désormais par Postgres plutôt que par des fichiers |
| `repository.py::ProjectionRepository` | Service de projection + cache SSE | **Refactorer** : remplacer `_read_document`/polling fichier par des requêtes Postgres + `LISTEN/NOTIFY` (ou polling léger sur `pipeline_events`) ; garder `events_after`/`wait_after` (sémantique de curseur déjà correcte) |
| `connections.py::ConnectionsStore` | `sources`/`destinations`/`pipelines` (tables Postgres) | **Remplacer** le stockage fichier par Postgres ; garder les regex de validation (`_IBMI_HOST`, `_SNOWFLAKE_IDENTIFIER`, etc.) et le principe « aucun secret en clair dans la signature » |
| `fleet_runtime_store.AtomicJsonStateStore` | — | **Conserver** pour l'état opérationnel produit par les Jobs (checkpoints, preuves) — ce n'est pas un state store de contrôle, ne pas le migrer vers Postgres sans nécessité |
| `actions.py` (triptyque intent/execution/observed_effect, `PipelineActionGate`) | Exécution d'action `/v2` | **Réutiliser** le cœur (gate, normalisation d'exécuteur) ; **étendre** l'enveloppe avec dry_run/idempotency/confirmation |
| `fleet_action_executor.py`, `fleet_pause_runtime.py`, `fleet_prepare_runtime.py`, `fleet_history_runtime.py`, `fleet_job_launcher.py`, `k8s_jobs.py` | Exécuteurs Kubernetes v2 | **Réutiliser** sans changement de fond — ce sont déjà les bons adaptateurs vers Kubernetes, indépendants du contrat HTTP |
| `audit.py::ActionAuditLog` | `audit_records` Postgres | **Remplacer**, migration ponctuelle décrite en 7.2 |
| `auth.py` | `users`/`oidc`/`agent_tokens` + mode proxy en secours | **Étendre** : garder le mode proxy-header pour compat, ajouter comptes/OIDC/jetons |
| `onboarding.py` | `/v2/sources`, `/v2/destinations`, `/v2/tables` (wizard) | **Refactorer** : la logique de validation par étape reste utile pour le CLI/MCP guidé, mais le modèle single-site (`_current_site()`) doit céder la place à des ressources par organisation |
| `costs.py`, `infrastructure_costs.py` | `/v2/costs`, `costs_snapshots` | **Réutiliser** intégralement, juste persister les instantanés au lieu de les projeter à la volée |
| `telemetry.py` | inchangé | **Conserver**, hors périmètre de l'API produit |
| `server.py` (routage stdlib `http.server`) | Routeur `/v2` | **Refactorer** vers un framework ASGI (ex. Starlette/FastAPI) pour porter OpenAPI 3.1 nativement, SSE, et le montage MCP — le routage manuel actuel (`do_GET`/`do_POST`, comparaisons de chaînes) ne scale pas à la surface `/v2` |
| `cli.py` | Serveur uniquement | Le vrai CLI utilisateur (`quadringent ...`) est un **nouveau module**, client HTTP de `/v2` |
| `window_chain.py`, `fleet_evidence.py`, `fleet_progression.py`, `fleet_plan.py`, `fleet_catalog_refresh.py`, `fleet_providers.py`, `projection.py` | Moteur de preuve (inchangé) | **Réutiliser** sans modification — logique métier de preuve de réplication, orthogonale au contrat API |

---

## 9. Risques, questions ouvertes, découpage TDD

### 9.1 Risques et questions ouvertes

1. **Multi-tenant vs single-site.** Toute la base actuelle (`site_config.current()`, contextvars, `LEGACY_SITE_ID`) suppose un site par processus. Passer à plusieurs sources/organisations dans un seul control plane est le changement le plus risqué : il touche transversalement `onboarding.py`, `fleet_composition.py`, `k8s_jobs.py` (namespace par site ?). Question ouverte : un control plane v2 sert-il une seule installation (un client) avec plusieurs sources, ou reste-t-il single-tenant avec plusieurs sources mais toujours *une* organisation ? Le design (§1) ne parle que d'« une connexion IBM i » au pluriel implicite mais ne tranche pas le multi-org. Recommandation : rester single-org pour chantier 3, garder `organizations` en base pour ne pas fermer la porte.
2. **Postgres embarqué : cycle de vie.** « Embarqué » (le design dit *« Postgres embarqué sur volume »*) implique un processus Postgres géré par le control plane lui-même (ex. via `pg_ctl` intégré à l'image, ou un sidecar). Risque opérationnel : sauvegarde/restauration, migration de schéma sans interruption des Jobs qui tournent. À trancher avant la première tâche d'implémentation.
3. **Double écriture pendant la transition.** Le state store fichier (`fleet-*.json`) continue d'exister pour les runtimes ; il faut garantir qu'aucune incohérence ne s'installe entre `pipeline_events` (Postgres) et les fichiers sources de vérité pendant la période de coexistence.
4. **Confirmations liftables par un jeton distant.** Le lien d'approbation non authentifié (`/confirmations/{id}?token=...`) doit être signé et à usage unique pour ne pas devenir une faille (cf. discipline zéro-secret déjà en place ailleurs) — à spécifier précisément avant implémentation (TTL court, un seul clic).
5. **MCP embarqué vs séparé.** Le design dit « MCP intégré au control plane » — clarifier si c'est un process séparé partageant la base, ou un module in-process du même serveur ASGI (recommandé pour la cohérence transactionnelle de `pending_confirmation`).
6. **Framework HTTP.** Le remplacement de `http.server` (stdlib, aucune dépendance) par un framework ASGI est un changement de dépendance notable dans un projet qui semble valoriser des surfaces minimales (`k8s_jobs.py` : « Ce module n'expose que deux opérations, volontairement »). Ce choix doit être validé explicitement, pas supposé.

### 9.2 Découpage TDD (ordonné, chantier 3)

Chaque tâche : test d'abord, rouge, puis code minimal, vert. Fichiers de test indiqués sous `tests/`.

1. **Schéma Postgres et connexion** — `tests/test_control_plane_store_postgres.py` : migrations appliquées proprement sur une base éphémère (`testcontainers` ou Postgres local CI), round-trip `sources`/`destinations`/`tables`/`pipelines`.
2. **Modèle `Source` v2 (CRUD read/create/test)** — `tests/test_v2_sources.py` : création avec secret jamais renvoyé en clair, régression sur les regex reprises de `connections.py`.
3. **Modèle `Destination` v2** — `tests/test_v2_destinations.py` : génération de paire de clés + script SQL, jamais de secret admin transmis (reprend l'esprit de `docs/plans/...§2.2`).
4. **Découverte de tables** — `tests/test_v2_tables_discovery.py` : statut de journalisation, commandes CL générées, choix de clé unique/RRN (portage des scénarios `onboarding.py::parse_table_selection`).
5. **Machine à états `declared_state`** — `tests/test_v2_pipeline_state_machine.py` : chaque transition listée en §1.2/1.3, y compris les transitions interdites (ex. `stopped` ne redevient jamais `copying`).
6. **Enveloppe d'action générique (`dry_run`, `Idempotency-Key`, before/after)** — `tests/test_v2_actions_envelope.py` : reprend/étend `tests/test_control_plane_actions.py` existant ; vérifie rejeu idempotent identique, conflit sur clé réutilisée avec corps différent.
7. **Pending confirmation** — `tests/test_v2_confirmations.py` : action sensible sans confirmation → 409 ; approbation humaine ; expiration ; levée par jeton pré-autorisé.
8. **Agent tokens (format, hash, scope, restriction source)** — `tests/test_v2_agent_tokens.py` : rotation invalide l'ancien hash, restriction de source appliquée, scope insuffisant refusé.
9. **Users/roles/activation** — `tests/test_v2_users.py` : premier admin par lien à usage unique, `reader` ne peut pas écrire.
10. **OIDC (optionnel)** — `tests/test_v2_oidc.py` : callback valide crée/relie un `user`, callback invalide échoue fermé.
11. **Audit distinguant humain/agent** — `tests/test_v2_audit.py` : `actor_kind`, `mcp_client` correctement peuplés selon la voie d'entrée (UI/CLI/MCP).
12. **SSE étendu** — `tests/test_v2_events.py` : nouveaux types d'événements, `Last-Event-ID`, reprend les invariants de `tests/test_control_plane_server.py` sur la reprise de curseur.
13. **Webhooks signés + anti-rejeu** — `tests/test_v2_webhooks.py` : signature HMAC valide/invalide, `event_id` déjà livré non rejoué, retry/backoff.
14. **Coûts v2 (persistance des instantanés)** — `tests/test_v2_costs.py` : portage direct des invariants de `tests/test_costs.py`/`tests/test_infrastructure_costs.py` sur le nouveau stockage.
15. **Migration import v1 → v2** — `tests/test_v2_migration_import.py` : `connections.json` fixture → lignes Postgres attendues, idempotence du script d'import (rejouable sans doublon).
16. **Proxy `/v1` → `/v2` de compatibilité** — `tests/test_v1_v2_compat_proxy.py` : chaque route `/v1` documentée dans `docs/api.md` reste fonctionnelle via le proxy.
17. **OpenAPI 3.1 généré et validé** — `tests/test_v2_openapi_contract.py` : le document généré valide un jeu de requêtes/réponses de référence (contract testing), utilisé aussi pour générer le client MCP et le CLI.
18. **Serveur MCP : outils en lecture** — `tests/test_v2_mcp_read_tools.py` : `list_pipelines`, `get_pipeline`, `get_costs` retournent exactement ce que `/v2` retourne.
19. **Serveur MCP : outils d'écriture + confirmation** — `tests/test_v2_mcp_write_tools.py` : `pause_pipeline` respecte `dry_run`, `remove_table` renvoie `pending_confirmation` sans jamais l'exécuter seul.
20. **CLI `quadringent` (sous-commandes lecture puis écriture)** — `tests/test_v2_cli.py` : sortie JSON stable, `--dry-run` par défaut documenté, codes de sortie non-zéro sur erreur avec code stable.
21. **Bout-en-bout : wizard d'onboarding piloté par agent via MCP** — `tests/test_v2_onboarding_agent_e2e.py`, en écho au critère de qualification continue déjà énoncé (§6 du design : « assistant piloté par un agent via MCP »).

---

### Critical Files for Implementation

- `src/quadringent_control_plane/repository.py`
- `src/quadringent_control_plane/connections.py`
- `src/quadringent_control_plane/actions.py`
- `src/quadringent_control_plane/model.py`
- `src/quadringent_control_plane/auth.py`
- `src/quadringent_control_plane/audit.py`
- `src/quadringent_control_plane/server.py`
- `docs/plans/2026-09-23-produit-fini-design.md`

## Décisions du 23 septembre 2026

| Question ouverte | Décision |
|---|---|
| Tenancy | Une organisation par installation (le client), plusieurs sources et destinations ; table `organizations` conservée sans multi-tenant dans l’UI. |
| État du control plane | Postgres déployé par la chart (StatefulSet, volume, sauvegarde planifiée vers le stockage objet) ; le mode expert peut pointer vers un Postgres géré. Migrations versionnées au démarrage. |
| Framework HTTP | FastAPI (OpenAPI 3.1 depuis les modèles Pydantic, SSE, montage MCP). |
| MCP | Dans le control plane, endpoint `/mcp` (HTTP streamable) protégé par jeton d’agent, même couche de service que l’API ; mode stdio via la CLI. |
