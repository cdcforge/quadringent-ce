# Control plane v2 (`/v2`) — état après les chantiers 3 et 4

Ce document décrit ce qui existe à l'issue du chantier 3 (« control plane
v2 », fondation) et de la tâche 4 (découverte de tables IBM i). Il couvre
les tâches 1, 2, 3, 4, 5 et 6 du contrat
(`docs/plans/2026-09-23-control-plane-v2-contract.md`, §9.2/§2.3) : le
reste (confirmations, jetons d'agent, utilisateurs, audit v2, SSE étendu,
webhooks, coûts v2, migration v1→v2, proxy v1→v2, MCP, CLI) est hors
périmètre et n'existe pas encore.
v2 »). Il couvre les tâches 1, 2, 3, 5, 6 (fondation), 7, 8, 9, 11, 12 et 13
du contrat (`docs/plans/2026-09-23-control-plane-v2-contract.md`, §9.2) :
confirmations pour actions sensibles, jetons d'agent, utilisateurs/rôles,
audit interrogeable, SSE étendu et webhooks signés. Restent hors périmètre
et n'existent pas encore : découverte de tables (tâche 4), OIDC (tâche 10,
optionnel), coûts v2 (tâche 14), migration v1→v2 (tâche 15), proxy v1→v2
(tâche 16), OpenAPI contract-testing (tâche 17), serveur MCP (tâches 18-19),
CLI (tâche 20), bout-en-bout agent (tâche 21).

Le serveur `/v1` (`src/quadringent_control_plane/server.py`) reste
indépendant de l'application FastAPI `/v2`, mais sait désormais la
relayer : voir « Accès à travers le seul port v1 » ci-dessous.

## Accès à travers le seul port v1

Le Pod control plane contient deux conteneurs (`control-plane` v1, port
`controlPlane.port` = 8844, et `control-plane-v2`, port
`controlPlane.v2.port` = 8845), tous deux en écoute loopback. L'opérateur
n'ouvre qu'un tunnel vers le port v1
(`kubectl port-forward deployment/<release>-quadringent-control-plane
8844:8844`, ou son équivalent SSM/IAP en mode VM). Quand
`controlPlane.v2.enabled` est actif, la chart passe `--v2-upstream-port`
au conteneur v1, qui relaie alors toute requête `/v2/*` ou `/mcp` vers
`http://127.0.0.1:<port v2>` (corps, en-têtes utiles — dont cookies de
session et jetons — et flux SSE relayés tels quels ; v1 ne fait ni
authentification ni autorisation sur ces chemins, v2 porte la sienne). Le
wizard d'activation et l'UI (`ui/src/data/controlPlaneV2Client.ts`,
basePath `/v2`) fonctionnent ainsi à travers ce seul tunnel.

## Démarrer l'API

```bash
pip install -e ".[api]"   # ou : ajouter l'extra "api" du paquet quadringent

export QUADRINGENT_V2_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export QUADRINGENT_CONTROL_PLANE_V2_DSN="postgresql+psycopg://user:password@host:5432/quadringent"
```

Exemple de code pour construire et servir l'application (aucun script CLI
dédié n'existe encore — hors périmètre de ce chantier) :

```python
from sqlalchemy import insert
from quadringent_control_plane.v2 import db, schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox

dsn = "postgresql+psycopg://user:password@host:5432/quadringent"
db.run_migrations(dsn)                       # applique les migrations Alembic
engine = db.create_engine_for(dsn)
with engine.begin() as connection:
    connection.execute(insert(schema.organizations), {"id": "default", "name": "Client"})

app = create_v2_app(
    engine=engine,
    secret_box=SecretBox.from_environment(),
    org_id="default",
    token_pepper=b"...",  # QUADRINGENT_V2_TOKEN_PEPPER en production — voir "Secrets"
)
# uvicorn.run(app, host="0.0.0.0", port=8001)
```

## Migrations

Les migrations Alembic vivent sous
`src/quadringent_control_plane/v2/migrations/`. Deux révisions existent à
`src/quadringent_control_plane/v2/migrations/`. Révisions, dans l’ordre :

| Révision | Contenu |
|---|---|
| `0001_initial_schema` | `organizations`, `sources`, `destinations`, `tables` (colonnes minimales), `pipelines`, `idempotency_keys` |
| `0002_identity_conf_audit` | utilisateurs, jetons d’agent, confirmations, audit, événements, webhooks |
| `0003_webhooks_secret_ciphertext` | secret de webhook chiffré (réversible, nécessaire à la signature) |
| `0004_tables_discovery` | ajoute à `tables` : `readiness`, `key_columns`, `journal_library`, `journal_name`, `images`, `cl_fix_commands` (JSON) |
| `0005_pipeline_last_observed` | ajoute à `pipelines` : `last_observed_state`, `last_observed_at` (mémoire du rafraîchissement d'observation, chantier « observabilité v2 » — voir plus bas) |
| `0009_scheduler_locks` | table `scheduler_locks` (bail `holder`/`expires_at`) — verrou multi-réplicas de `ObservationRefreshScheduler`, voir « Ordonnancement » plus bas |
| `0010_unify_leases` | table `leases` (bail unifié `holder`/`generation`/`expires_at`, `services/lease.py`) remplaçant `reconciler_leases` (0007) et `scheduler_locks` (0009) — même mécanisme, une seule implémentation partagée par la boucle de réconciliation et `ObservationRefreshScheduler`, avec un compteur de génération pour le fencing |
| `0001_initial_schema` | `organizations`, `sources`, `destinations`, `tables` (colonnes minimales, pour la clé étrangère de `pipelines` — la découverte de tables elle-même est hors périmètre), `pipelines`, `idempotency_keys` |
| `0002_identity_conf_audit` | `users`, `activation_tokens`, `agent_tokens`, `confirmations`, `audit_records`, `events` (flux SSE persisté), `webhooks`, `webhook_deliveries` |
| `0003_webhooks_secret_ciphertext` | remplace `webhooks.secret_hash` par `webhooks.secret_ciphertext` (le secret de signature doit rester déchiffrable, pas seulement vérifiable) |

Les ids de révision Alembic sont contraints à 32 caractères par la colonne
`alembic_version.version_num` sur Postgres (pas de contrainte sur SQLite,
d'où l'importance des tests marqués `@pytest.mark.postgres` pour détecter
ce genre de régression).

Appliquer les migrations sur une base cible (idempotent, s'arrête à
`head`) :

```python
from quadringent_control_plane.v2 import db
db.run_migrations("postgresql+psycopg://user:password@host:5432/quadringent")
```

## Modèle de données (périmètre de ce chantier)

- **`sources`** — une connexion IBM i déclarée : `display_name`,
  `ibmi_host`, `ibmi_user`, `secret_ciphertext` (mot de passe chiffré
  Fernet, jamais exposé — l'API ne renvoie que `secret_set: true`).
- **`destinations`** — une cible Snowflake déclarée : `snowflake_account`,
  `key_pair_ciphertext` (clé privée RSA 2048 chiffrée), `setup_script`
  (script SQL de mise en service, régénérable via `GET`), `verification_state`.
- **`tables`** — une table découverte sur une source : `schema_name`
  (bibliothèque IBM i), `table_name`, `journal_status`, `readiness`
  (`ready`, `not_journaled`, `images_incomplete`, `no_key` — voir
  « Découverte de tables » ci-dessous), `key_strategy`
  (`primary`/`unique_index`/`rrn`), `key_columns`, `discovered_row_count`,
  `discovered_size_bytes`, `journal_library`, `journal_name`, `images`,
  `cl_fix_commands` (JSON), `discovered_at`.
- **`pipelines`** — `declared_state` (voir machine à états ci-dessous).
- **`idempotency_keys`** — une ligne par organisation et en-tête
  `Idempotency-Key`, liée à l'acteur, avec l'empreinte de la requête et une
  réponse rejouable sans secrets à émission unique (TTL 24 h).

## Machine à états déclarée du pipeline

`quadringent_control_plane.v2.services.state_machine` implémente
exactement les transitions du contrat §1.2/§1.3 :

```
not_started --start--> copying --bootstrap_completed--> live
copying|live --pause--> paused
paused --resume_copying|resume_live--> copying|live
copying|live|paused --attention--> attention
attention --resume--> copying
copying|live|paused|attention --remove--> stopped        (terminal)
copying|live|paused|attention --restart_initial_copy--> copying
```

`stopped` est terminal : aucun événement n'en sort. La fonction
`transition(current, event)` lève `ForbiddenTransitionError` pour toute
paire non listée, `UnknownStateError` si `current` n'est pas un état connu.

## Identité (tâches 8, 9)

Trois voies d'authentification, résolues dans cet ordre par
`v2/auth.py::resolve_identity` :

1. **Jeton d'agent** — en-tête `Authorization: Bearer qdt_<rd|op|ad>_<b62>`.
   Seul le hash (sha256 + pepper serveur) est persisté ; scope `read`,
   `operate` ou `admin` porté par le préfixe, restriction de source
   optionnelle (`source_restriction`), expiration obligatoire sauf
   `never_expires: true` déclaré explicitement, rotation (`POST
   /v2/agent-tokens/{id}/rotate`) et révocation (`DELETE`).
2. **Cookie de session utilisateur** — posé par `POST /v2/auth/login`
   (email + mot de passe, haché scrypt) : `HttpOnly`, `SameSite=Strict`,
   `Secure` configurable (`session_cookie_secure`). Rôles `admin`/`reader` ;
   un `reader` ne dépasse jamais le scope `read`. Premier admin activé via
   `POST /v2/setup/first-admin` (lien à usage unique, TTL 24 h, refusé si un
   admin actif existe déjà) ; admins suivants invités via `POST /v2/users`.
3. **Proxy de confiance v1** — en-têtes posés par un reverse-proxy
   d'authentification (mode `AuthConfig` existant), conservé pour
   compatibilité.

Sans aucune des trois configurées, l'identité est un administrateur
implicite (mode développement/loopback, inchangé) — **sauf en mode
« authentification exigée »** (tâche « auth-login »).

### Mode « authentification exigée » (production)

`entrypoint.build_app` construit désormais l'application avec
`create_v2_app(..., require_authentication=True)` : le secours
anonyme-admin ci-dessus ne s'applique plus à aucune route qui résout une
identité (`Depends(require_scope(...))`/`require_source_scope(...)`) — une
requête sans jeton d'agent, sans session valide et sans proxy de confiance
déclaré reçoit `401 invalid_request` (« authentification requise »), au lieu
d'un accès admin implicite.

Restent accessibles **sans aucune identité**, en tout état de cause (elles
ne résolvent aucune identité) :

- `GET /v2/healthz`, `GET /v2/openapi.json` (et `/v2/docs`, `/v2/redoc`) ;
- `POST /v2/setup/first-admin` ;
- `POST /v2/users/activate`, `POST /v2/users/{id}/activate` ;
- `POST /v2/auth/login`, `POST /v2/auth/logout`.

`GET /v2/auth/me` (nouveau, tâche « auth-login ») renvoie l'identité
courante (`subject`, `role`, `actor_kind`, `email` — `null` pour un acteur
non humain) résolue par jeton d'agent/session/proxy ; `401` sans identité
résolue. C'est ce que l'UI interroge pour savoir si elle est connectée.

`require_authentication` vaut `False` par défaut dans `create_v2_app`
(mode développement/loopback historique, inchangé — c'est ce que les tests
existants et les appels directs à `create_v2_app` continuent d'utiliser
sans le déclarer).

**Limite connue (cookie `Secure`)** : le cookie de session posé par
`POST /v2/auth/login` porte toujours l'attribut `Secure`
(`session_cookie_secure=True` dans `entrypoint.build_app`), y compris à
travers le tunnel `http://127.0.0.1:8844`. Chrome et Firefox acceptent les
cookies `Secure` sur `http://localhost`/`http://127.0.0.1` (exception du
spec pour les origines de boucle locale) ; Safari ne le fait pas de façon
fiable. Voir `docs/product/install-default.md`, section « Accès à l'UI
après installation ».

## Confirmations (tâche 7)

`remove`, `restart_initial_copy` et `replay` sur
`/v2/pipelines/{id}/actions/{action}` sont des actions sensibles : sans
`confirmation_token` valide dans le corps, elles créent une ligne
`confirmations` (`pending`, TTL 24 h) et renvoient
`409 pending_confirmation_required` plutôt que de s'exécuter. Trois façons
de lever une confirmation :

- Un humain authentifié (scope `operate` suffisant) appelle
  `POST /v2/confirmations/{id}/approve` sans corps.
- Un lien signé à usage unique (`POST .../approve {"token": "..."}`, HMAC
  sha256+pepper, jamais authentifié) — utile pour un email/webhook.
- Un jeton d'agent pré-autorisé (`pre_authorized_actions` déclarée à la
  création du jeton) approuve directement s'il porte le scope requis et
  que l'`action_ref` figure dans sa liste.

Une confirmation approuvée est consommée (`state: used`) après exécution de
l'action liée — impossible à rejouer avec le même `confirmation_token`.
`replay` est toujours confirmé dans ce chantier (pas de seuil de coût
configurable — §2.4 du contrat dit « oui si coût > seuil », décision
conservatrice en son absence).

## Audit (tâche 11)

`audit_records` remplace le fichier append-only `audit.py::ActionAuditLog`
(v1) par une table interrogeable (`GET /v2/audit`, filtres `actor_kind`,
`action`, `resource_type`, `resource_id`). Chaque route d'écriture qui
déclare un `AuditContext` à `idempotent_write` (`http.py`) est tracée
automatiquement — succès comme échec, jamais deux fois pour un rejeu
idempotent. `mcp_client` est peuplé depuis l'en-tête `X-MCP-Client`. Câblé
à ce stade sur les actions de pipeline, les jetons d'agent, les
utilisateurs, les confirmations et les webhooks.
Les champs `private_key_pem`, `token`, `activation_token` et `secret`
sont retirés récursivement de `before`/`after` avant stockage et lecture,
y compris pour une ancienne ligne restaurée. Les lectures sont limitées
à l'organisation du site.

## SSE étendu (tâche 12)

`GET /v2/events` reprend le contrat `Last-Event-ID` de `/v1/events`
(entier opaque, table `events` auto-incrémentée) et ajoute des types
nommés : `pipeline.state_changed`, `action.pending_confirmation`,
`action.completed`, `alert.fired`, `alert.resolved`. Sondage léger (pas de
`LISTEN/NOTIFY` dans ce chantier — décision documentée au contrat §8) ; le
flux ne transporte jamais l'état complet d'une ressource, un client relit
par HTTP.

## Webhooks signés (tâche 13)

`POST /v2/webhooks {"url": "https://...", "events": [...]}` génère un
secret affiché une seule fois (chiffré ensuite, jamais rejoué en clair).
Signature `X-Quadringent-Signature: t=<unix>,v1=<hex>` sur
`f"{timestamp}.{body}"`, tolérance 5 minutes. Anti-rejeu par contrainte
unique `(webhook_id, event_id)` — un événement déjà livré n'est jamais
rejoué automatiquement, seul `POST /v2/webhooks/{id}/redeliver/{event_id}`
(idempotent) force une nouvelle tentative. Backoff exponentiel borné (5
tentatives), désactivation automatique (`state: disabled`) après échecs
consécutifs, réinitialisés par une livraison réussie. La livraison
effective (`WebhookDeliveryWorker`, client HTTP injecté) tourne hors
requête HTTP — pas encore câblée en tâche de fond dans ce chantier.

## Enveloppe d'action générique

Toute écriture (`POST`/`PATCH`/`DELETE`) exige l'en-tête `Idempotency-Key`
(absent → `400 invalid_request`). Dans une organisation, la même clé
rejouée par le même acteur avec un corps identique renvoie le statut et les
métadonnées déjà produits, sans créer de doublon. Les secrets à émission
unique sont présents dans la première réponse uniquement : `private_key_pem`,
`token`, `activation_token` et `secret` ne sont ni persistés dans la réponse
d'idempotence ni renvoyés lors du rejeu. La copie expurgée ne modifie pas la
première réponse. Sans secret, le rejeu reste identique à celle-ci.
Un autre acteur ou un corps différent renvoie `409 idempotency_key_conflict`.
Les clés sont indépendantes entre organisations. Une clé expire après 24 h ;
la réutilisation par son acteur remplace uniquement sa ligne expirée.

**Réponse initiale perdue.** Le rejeu permet de vérifier l'identifiant et
l'effet de l'action, pas de récupérer le secret : enregistrer la première
émission dans un stockage privé. Une clé RSA destination n'est pas relisible
via `GET` ; le script de mise en service reste disponible car il contient
seulement la clé publique. Pour un jeton d'agent perdu, un administrateur
peut demander une rotation explicite avec une nouvelle clé d'idempotence.
Un lien d'activation initial perdu n'est pas récupérable par le rejeu :
aucune nouvelle activation ou remise à zéro n'est déclenchée automatiquement.
Ce contrat vaut aussi pour les appels de l'UI et de la CLI.

**Réémission explicite du premier admin.** Un opérateur authentifié avec le
scope `admin` peut appeler `POST /v2/users/{id}/activation/reissue` ou
`quadringent users reissue-activation <user_id> --idempotency-key <nouvelle-cle>`.
L'utilisateur doit être un admin non activé de l'organisation du site et
aucun admin de cette organisation ne doit être actif. La transaction garde
le même utilisateur, invalide ses anciens liens et stocke uniquement le hash
du nouveau jeton, valable 24 h. Celui-ci apparaît une fois dans
`after.activation_token` (201) ; le même rejeu conserve l'identifiant sans
jeton et ne réémet rien. Un ancien lien, même lu par une activation en cours
avant la réémission, ne peut plus être consommé après son invalidation.
L'action est auditée sans secret et ne change aucun mot de passe.

Sans session humaine admin, l'opérateur disposant déjà de `kubectl exec`
peut émettre un jeton natif borné via la commande existante
`quadringent agent-token --name <site> --namespace <namespace> --label bootstrap-recovery
--scope admin --days 1 --url <url-privee> --write-config <fichier-prive-0600>`.
Cette commande écrit le credential dans le fichier et ne l'affiche pas.
Utiliser `quadringent users --config <fichier-prive-0600> list` pour retrouver
l'identifiant, puis `quadringent users --config <fichier-prive-0600>
reissue-activation <user_id> --idempotency-key <nouvelle-cle>`. Ce fichier
explicite est prioritaire sur les variables de configuration ambiantes ;
la configuration habituelle des autres sites reste intacte.
La sortie JSON de cette dernière contient le lien à émission unique : la
conserver dans un fichier privé 0600 et activer l'utilisateur avec le jeton.
Une installation relancée ne promet pas de réafficher le lien initial.

**Bases anciennes.** La migration `0015_one_time_secrets`, exécutée au
démarrage/mise à niveau natifs, expurge les mêmes champs des JSON historiques
de `idempotency_keys.response` et `audit_records.before`/`after`. Les clés
Fernet et RSA chiffrées, hashes d'authentification, identifiants et métadonnées
d'actions sont conservés. Si la base possède exactement une organisation,
les anciennes clés y sont rattachées. Sinon leur attribution reste inconnue
et leur rejeu renvoie `409`, même après expiration : inspecter l'état de
l'action avant toute nouvelle demande, aucun effet n'est recréé par le rejeu.
La migration ne purge pas les anciennes sauvegardes et ne permet pas un
downgrade réintroduisant le magasin vulnérable. Lors d'une restauration
ancienne, appliquer les migrations avant de servir l'API et conserver les
sauvegardes sensibles sous contrôle d'accès.
Arrêter les anciens processus du control plane avant cette mise à niveau :
un ancien binaire pourrait réécrire des réponses non expurgées pendant une
cohabitation de versions. Le verrou de migration sérialise les migrations,
pas les écritures applicatives des anciens processus.

Réponse standard :

```json
{"before": null, "after": {"...": "..."}, "verify": {"method": "GET", "path": "/v2/sources/..."}, "dry_run": null}
```

Avec `"dry_run": true` dans le corps : `after` reste `null`, `dry_run`
contient le plan (aucun effet de bord persisté).

Catalogue d'erreurs implémenté à ce stade (`quadringent_control_plane.v2.errors`) :
`invalid_request` (400), `idempotency_key_conflict` (409), `not_found` (404),
`store_unavailable` (503), `insufficient_role` (403), `capability_unavailable`
(409), `action_in_progress` (409), `pending_confirmation_required` (409),
`wrong_confirmation` (403), `wrong_environment` (403). Le catalogue complet
est au §2.6 du contrat (`executor_unavailable`, `internal_error` restent à
implémenter précisément selon leurs cas d'usage).

## Observabilité v2 (routes de lecture)

Ce chantier ajoute les routes de lecture qui manquaient encore au cockpit
(`ui/src/data/controlPlaneV2Client.ts`, sections marquées « CONTRAT SEUL »
avant ce chantier) : liste des pipelines, métriques, journaux et coûts.
Chacune délègue à un fournisseur injectable sur `create_v2_app(...)`, par
défaut absent (échec fermé, jamais de valeur inventée), exactement comme
`pipeline_executor`/`table_discovery_client` déjà en place :

| Paramètre `create_v2_app` | Protocole | Défaut sans câblage |
|---|---|---|
| `pipeline_observation_provider` | `PipelineObservationProviderProtocol` (`v2/services/observation.py`) | `NullObservationProvider` — tous les champs observés `None`, avec raison |
| `log_source` | `LogSourceProtocol` (`v2/services/logs.py`) | `NullLogSource` — liste vide |
| `costs_provider` | `CostsProviderProtocol` (`v2/services/costs.py`) | `NullCostsProvider` — `status: "absent"` |

### Adaptateurs réels (chantier « observabilité v2 » suite)

Une deuxième tranche câble des adaptateurs réels derrière ces mêmes
protocoles — toujours en repli sur Null\* si la configuration ne les
active pas (voir « Câblage par défaut », plus bas) :

- **`v2/services/observation_projection.py::ProjectionRepositoryObservationAdapter`**
  — `observed_state`/`lag_seconds`/`rows_source`/`rows_destination`,
  lus depuis un document console/projection v1 réel
  (`repository.ProjectionRepository`/`model.PipelineProjection`, inchangés).
  `rows_source`/`rows_destination` viennent de deux clés de
  `projection.PUBLIC_COUNTERS` (liste fermée v1 ; défauts
  `events_published`/`events_in_target`, surchageables). Un résolveur
  injecté (`pipeline_source_spec`) traduit l'id de pipeline v2 vers une
  spécification de source v1 (`"evidence_kind:source_id:origin"`) ; sans
  résolution déclarée pour un pipeline, l'observation reste absente.
  `metrics()` projette `PipelineProjection.lag_series` en série de points
  (ancrée sur `observed_at`, seule référence temporelle portée par le
  document) — le débit par point reste absent (v1 ne le porte pas).
- **`v2/services/observation_storage.py::StorageBackendObservationAdapter`**
  — `throughput_rows_per_second`/`last_arrival_at`, calculés à partir de
  deux relevés successifs du `JournalPosition` (receveur + séquence) lu
  via `quadringent.storage_backend.StorageBackend.checkpoint_store` (AWS
  DynamoDB ou GCS selon le backend configuré). Débit = delta de séquence /
  delta de temps entre deux appels — jamais une estimation sur un seul
  point ; une rotation de receveur ou un recul de séquence laisse le champ
  absent avec sa raison plutôt que d'inventer une valeur.
  `rows_source`/`rows_destination`/`observed_state` restent hors périmètre
  (un curseur n'est pas un dénombrement de lignes absolu).
- **`v2/services/observation_composite.py::CompositeObservationProvider`**
  — combine les deux ci-dessus, champ par champ (état/retard/lignes de la
  projection, débit/arrivée du stockage) ; `metrics()` délègue uniquement à
  l'adaptateur de projection (seul à porter un historique).
- **`v2/services/logs_kubernetes.py::KubernetesLogSource`** — sélectionne
  les pods par le label `quadringent.io/pipeline-id` posé par l'exécuteur
  (`v2/executor/manifests.py::LABEL_PIPELINE`) via un client Kubernetes
  injecté (`k8s_pods.KubernetesPodsClient`, lecture seule, bornée en
  octets et en lignes). Ne couvre que les Jobs de copie initiale/rejeu
  (seuls objets labellisés par pipeline) — le Deployment lecteur, partagé
  par plusieurs pipelines sur un même journal, n'a pas de sélection non
  ambiguë avec le modèle de labels actuel (décision documentée, pas un
  oubli). Chaque ligne est demandée avec `timestamps=true` (horodatage
  serveur Kubernetes réel) ; niveau détecté par une heuristique textuelle
  (`error`/`warn` dans le message) documentée dans le module. La rédaction
  reste centrale, dans `services/logs.py` (§ ci-dessus), quelle que soit
  la source.
- **`v2/services/loader_telemetry.py::KubernetesLoaderTelemetry`** —
  câblé par `v2/entrypoint.py` quand l'application tourne dans Kubernetes.
  La base v2 associe le pipeline à une table et une destination ; la source
  lit seulement les lignes du pod chargeur portant le nom exact de cette
  table. Les autres lignes sont écartées avant l'API. Les journaux montrent
  les lots livrés et l'âge de la dernière mutation. La série `metrics`
  contient uniquement les livraisons réellement suivies d'un MERGE :
  `lag_seconds` y est le délai IBM i → miroir de la dernière mutation du
  lot, et le débit exige deux livraisons successives. La lecture est bornée
  aux 2 000 dernières lignes du pod ; une fenêtre de 24 h peut donc être
  incomplète. Sans pod, droit Kubernetes ou livraison dans la période,
  les mesures restent absentes avec une raison. `history_lag_seconds` et
  `mirror_lag_seconds` sont l'**âge** de la dernière mutation visible dans
  Snowflake à l'instant du relevé, réalisé après un lot de journal traité
  ou un instantané de copie initiale réellement appliqué, y compris vide.
  Un instantané seul ne produit pas de point de livraison de mutation de
  journal. Au repos, le chargeur ne sonde pas Snowflake : ces derniers âges
  mesurés restent datés par `collected_at` et disparaissent de l'observation
  après une heure sans relevé. Ils ne prouvent pas la fraîcheur actuelle du
  flux source ; `lag_seconds` reste absent au repos.
- **`v2/services/costs_projection.py::CostsV1ProjectionAdapter`** —
  réutilise `costs.project_costs` v1 tel quel (portée `connection`
  uniquement — un warehouse, jamais une table ; `scope=table` reste
  explicitement absent, aucun collecteur v1 n'existe à cette granularité).
- **`v2/services/costs_snowflake.py::SnowflakeWarehouseCostsAdapter`** —
  crédits d'un warehouse Snowflake mesurés en direct via une requête
  injectée (`SnowflakeCreditsQueryProtocol`, jamais un client Snowflake
  réel câblé ici) ; **aucun montant sans prix par crédit déclaré**
  (`price_per_credit=None` -> `absent`). Un échantillon provisoire
  (facturation Snowflake non finalisée) donne un statut `estimated`, un
  échantillon finalisé `measured`.
- **`v2/services/costs_composite.py::FallbackCostsProvider`** — essaie
  plusieurs fournisseurs de coûts dans l'ordre, retient le premier non
  `absent` (typiquement : document déjà publié en préférence, requête
  Snowflake en direct en repli).

### Ordonnancement (rafraîchissement périodique)

`v2/services/scheduler.py::ObservationRefreshScheduler` appelle
`ObservationRefreshService.refresh_all` à intervalle régulier
(`run_once()` est la primitive testable ; `start()`/`stop()` pilotent un
thread démon). Protégé par `v2/services/scheduler_lock.py::SchedulerLock`
— un bail (`holder`/`expires_at`, table `leases`, migration
`0010_unify_leases`) portable SQLite/Postgres : un seul réplica rafraîchit
à la fois même en StatefulSet/Deployment à plusieurs réplicas, et un
réplica mort libère son verrou de lui-même à l'expiration du bail.

`SchedulerLock` est désormais une fine enveloppe autour de
`v2/services/lease.py::Lease` — la même implémentation de bail que la
boucle de réconciliation (`services/reconciler.py::acquire_lease`, voir
`docs/orchestration.md` §7), unifiée par la migration `0010_unify_leases`
(table `leases` unique, remplaçant `reconciler_leases` et
`scheduler_locks`) avec un compteur de génération pour le fencing : un
titulaire qui a perdu le bail peut le détecter (`Lease.is_current()`)
avant d'agir, même sans avoir observé l'expiration lui-même.

### Câblage par défaut

`create_v2_app(...)` câble ces adaptateurs automatiquement quand la
configuration le permet — toujours en repli sur Null\* sinon, et un
fournisseur explicite (`pipeline_observation_provider`/`log_source`/
`costs_provider`) prime toujours sur ce câblage automatique :

| Paramètre | Effet |
|---|---|
| `pipeline_source_spec_resolver` | câble `ProjectionRepositoryObservationAdapter` (seul, ou combiné si `capture_storage_backend`/`pipeline_stream_key_resolver` sont aussi fournis) |
| `pipeline_stream_key_resolver` + `capture_storage_backend` | ajoute `StorageBackendObservationAdapter`, combiné via `CompositeObservationProvider` |
| `kubernetes_pods_client` | câble `KubernetesLogSource` |
| `connection_source_spec_resolver` | câble `CostsV1ProjectionAdapter` |
| `snowflake_credits_query` + `snowflake_warehouse` | ajoute `SnowflakeWarehouseCostsAdapter` (prix/devise lus depuis `quadringent.site_config.current()` — silencieusement ignoré si le site n'est pas configuré, jamais un échec de démarrage) ; combiné via `FallbackCostsProvider` si `connection_source_spec_resolver` est aussi fourni |
| `enable_observation_scheduler=True` | démarre `ObservationRefreshScheduler` (arrêté proprement au `shutdown` FastAPI) — **désactivé par défaut partout**, y compris en présence d'un résolveur |

### `GET /v2/pipelines`

Liste paginée (`?limit=&cursor=` -> `{"items":[...],"next_cursor":...}`),
filtrable par `state` (une des valeurs de
`state_machine.DECLARED_STATES`), `source_id` (jointure `pipelines.table_id
-> tables.source_id`) et `destination_id`. Chaque élément combine l'état
*déclaré* (base v2, `declared_state`) et les figures en direct de
l'observation injectée : `observed_state`, `lag_seconds`,
`throughput_rows_per_second`, `rows_source`, `rows_destination`,
`last_arrival_at`, `collected_at`, `absent_reasons` (raison par champ
absent). Un filtre `state` hors catalogue échoue fermé (`400
invalid_request`), jamais silencieusement ignoré.

Fournisseur réel : `ProjectionRepositoryObservationAdapter`/
`CompositeObservationProvider`, voir « Adaptateurs réels » plus bas.

### `GET /v2/pipelines/{id}/metrics?window=1h|24h`

Série retard/débit (`{"window","points":[{"at","lag_seconds",
"throughput_rows_per_second"}],"provenance","freshness","collected_at"}`),
dans l'esprit de `model.LagSeriesProjection` sans en dépendre directement
(clé de pipeline v2 incompatible avec la clé `fleet_id`/`environment` v1).
`window` hors `{1h, 24h}` -> `400 invalid_request`. Pipeline inconnu ->
`404 not_found`.

### `GET /v2/pipelines/{id}/logs?since=&level=&correlate_incident=`

Journaux filtrés (`level` parmi `info`/`warning`/`error`,
`correlate_incident=true` ne garde que les entrées portant un
`incident_id`). **Règle de rédaction** (appliquée à toute source, jamais
laissée à la discrétion du fournisseur injecté — voir l'en-tête de
`v2/services/logs.py`) :

1. toute paire `clé=valeur`/`clé: valeur` dont la clé ressemble à un secret
   (`password`, `secret`, `token`, `api_key`, `authorization`,
   `credential`) est remplacée par `clé=[SECRET_MASQUE]` ;
2. tout bloc `{...}` contenant au moins deux paires `"clé": valeur` (donc
   ressemblant à une ligne de donnée source sérialisée, pas à un simple
   identifiant entre accolades) est remplacé en bloc par
   `{"redacted": "donnee_de_ligne_masquee"}`.

Testé dans `tests/test_v2_pipeline_logs.py` (faux positifs évités : un bloc
à un seul champ n'est pas masqué). Source réelle : `KubernetesLogSource`,
voir « Adaptateurs réels » plus bas.

### `GET /v2/costs?scope=connection|table&id=&window=`

Reprend les invariants de `costs.py`/`infrastructure_costs.py` v1
(mesuré/estimé/absent, devise et base toujours explicites, jamais de
montant inventé) sans réutiliser leurs types — incompatibles avec un
`scope`/`id` opaque v2 (voir la justification en tête de
`v2/services/costs.py`). `scope` hors `{connection, table}` ou `id` absent
-> `400 invalid_request`. Fournisseurs réels : `CostsV1ProjectionAdapter`
(réutilise `costs.project_costs` v1) et `SnowflakeWarehouseCostsAdapter`
(crédits mesurés en direct) — voir « Adaptateurs réels » plus bas.

### Émission SSE sur changement d'état observé

`ObservationRefreshService` (`v2/services/observation_refresh.py`) compare
l'`observed_state` renvoyé par le fournisseur d'observation au dernier état
observé connu, persisté dans `pipelines.last_observed_state` (migration
`0005_pipeline_last_observed` — jamais l'état *déclaré*, inchangé). Sur un
vrai changement, il publie `pipeline.state_changed` via `EventsService`
(réutilisé tel quel, tâche 12) ; une entrée/sortie de l'état `incident`
publie en plus `alert.fired`/`alert.resolved` (`alert_id` conventionnel
`f"pipeline:{pipeline_id}"`, faute d'un modèle d'alertes v2 dédié dans ce
chantier). `refresh_all` est invoqué périodiquement par
`ObservationRefreshScheduler` — voir « Ordonnancement » plus bas
(désactivé par défaut, `enable_observation_scheduler=True` pour l'activer).

## Endpoints implémentés

| Méthode | Route | Scope | Notes |
|---|---|---|---|
| GET | `/v2/sources` | read | pagination non implémentée (`next_cursor` toujours `null`) |
| POST | `/v2/sources` | operate | `dry_run` teste la validation sans persister |
| GET | `/v2/sources/{id}` | read | |
| POST | `/v2/sources/{id}/test` | operate | vérifie que le secret déchiffre ; ne sonde aucun réseau réel dans ce chantier |
| GET | `/v2/destinations` | read | |
| POST | `/v2/destinations` | operate | génère la paire de clés RSA + le script SQL ; la clé privée n'est renvoyée qu'une fois |
| GET | `/v2/destinations/{id}` | read | |
| GET | `/v2/destinations/{id}/setup-script` | read | script SQL de mise en service, relisible (clé publique seulement) |
| POST | `/v2/destinations/{id}/verify` | operate | vérifie connexion/rôle/warehouse/base/schémas et droits de chargement via un vérificateur injecté (`app.state.destination_verifier`, `None` par défaut → `verified: "unknown"`) ; transitionne `verification_state` vers `verified`/`failed` |
| GET | `/v2/pipelines` | read | liste paginée, filtres `state`/`source_id`/`destination_id` — voir « Observabilité v2 » |
| GET | `/v2/pipelines/{id}` | read | |
| GET | `/v2/pipelines/{id}/metrics` | read | `?window=1h\|24h` |
| GET | `/v2/pipelines/{id}/logs` | read | `?since=&level=&correlate_incident=`, rédaction systématique |
| GET | `/v2/costs` | read | `?scope=connection\|table&id=&window=` |
| POST | `/v2/pipelines/{id}/actions/{pause,resume,remove}` | operate | applique une transition via un exécuteur injecté (aucun câblage Kubernetes) ; `dry_run` retourne le plan de transition |
| GET | `/v2/sources/{id}/tables` | read | filtres `search`, `library`, `readiness` |
| POST | `/v2/sources/{id}/tables/refresh` | operate | relance la découverte via un client injecté (`app.state.table_discovery_client`, `None` par défaut → `503 discovery_unavailable`) ; `dry_run` ne sonde rien |
| PATCH | `/v2/tables/{id}` | operate | choix de clé (`primary`/`unique_index`/`rrn` ; `rrn` exige `acknowledge_rrn: true`) |
| POST | `/v2/pipelines/{id}/actions/{pause,resume,remove,restart_initial_copy,replay}` | operate | `remove`/`restart_initial_copy`/`replay` exigent une confirmation (409 sans `confirmation_token` approuvé) ; exécuteur injecté, aucun câblage Kubernetes ; `dry_run` retourne le plan |
| GET | `/v2/confirmations` | read | filtre `?state=` (défaut `pending`) |
| GET | `/v2/confirmations/{id}` | read | |
| POST | `/v2/confirmations/{id}/approve` | operate\* | `{"token": "..."}` = lien signé, non authentifié ; sinon identité (scope `operate`) |
| POST | `/v2/confirmations/{id}/reject` | operate | |
| GET/POST | `/v2/agent-tokens` | admin | `POST` retourne le jeton en clair une seule fois (`after.token`) |
| POST | `/v2/agent-tokens/{id}/rotate` | admin | invalide l'ancien jeton, retourne le nouveau une seule fois |
| DELETE | `/v2/agent-tokens/{id}` | admin | révocation (`revoked_at`) |
| POST | `/v2/setup/first-admin` | — | échoue (409) si un admin actif existe déjà |
| GET/POST | `/v2/users` | admin | `POST` (invite) retourne un lien d'activation à usage unique |
| GET | `/v2/users/{id}` | admin | |
| POST | `/v2/users/activate` | — | `{"token", "password"}` : forme du lien `/activate?token=…` remis par l'installeur |
| POST | `/v2/users/{id}/activate` | — | `{"activation_token", "password"}` |
| POST | `/v2/users/{id}/activation/reissue` | admin | réémission explicite du premier admin non activé ; aucune réinitialisation d'un admin actif |
| POST | `/v2/auth/login` \| `/logout` | — | pose/efface le cookie de session |
| GET | `/v2/auth/me` | read | identité courante (`email`, `role`) ; `401` sans identité résolue — voir « Mode "authentification exigée" » |
| GET | `/v2/audit` | admin | filtres `actor_kind`, `action`, `resource_type`, `resource_id` |
| GET | `/v2/events` | read | SSE, `Last-Event-ID` ou `?last_event_id=` |
| GET/POST | `/v2/webhooks` | read/admin | `POST` retourne le secret de signature une seule fois |
| DELETE | `/v2/webhooks/{id}` | admin | |
| POST | `/v2/webhooks/{id}/redeliver/{event_id}` | operate | idempotent (anti-rejeu par `(webhook_id, event_id)`) |
| GET | `/v2/openapi.json` | — | document OpenAPI 3.1 |

\* `POST /v2/confirmations/{id}/approve` n'exige `operate` que sans jeton
d'approbation dans le corps — avec `{"token": "..."}`, aucune
authentification n'est requise (lien signé à usage unique).
L'identité et les droits courants sont résolus avant le rejeu d'idempotence.
Un en-tête `X-Request-Actor` ne fournit aucune identité. Le lien signé est
vérifié (organisation, empreinte et expiration) avant le cache ; il possède
une identité stable distincte d'une session ou d'un agent. Le même lien et
la même clé peuvent relire le résultat sans approuver une seconde fois ;
un lien expiré ou invalide n'obtient pas de réponse mise en cache.

Scopes : `read` (lecture), `operate` (écritures non sensibles), `admin`
(gestion des jetons d'agent, utilisateurs, audit, webhooks). Trois voies
d'authentification (voir « Identité » ci-dessus) ; sans aucune configurée,
l'identité est un administrateur implicite (mode développement/loopback).

## Découverte de tables (tâche 4)

`POST /v2/sources/{id}/tables/refresh` délègue à un
`TableDiscoveryClientProtocol` injecté (`app.state.table_discovery_client`
sur `create_v2_app(...)`) ; le vrai câblage vers
`PersistentJavaWorker.discover` (commande worker `discover`, catalogue IBM i
seulement — `QSYS2.SYSTABLES`/`SYSTABLESTAT`/`JOURNALED_OBJECTS`/`SYSKEYCST`)
est hors périmètre de ce module (voir `src/quadringent/table_discovery.py`
et `java/src/main/java/io/quadringent/as400/TableDiscovery.java`). Sans
client configuré, la route échoue fermé : `503 discovery_unavailable`.

Câblage production (chantier « prod-wiring », 24 septembre 2026) : en
cluster, `entrypoint.py::build_diagnostic_adapters` injecte
`KubernetesJobTableDiscoveryClient` (`v2/executor/diagnostic_jobs.py`) —
la découverte tourne dans un Job Kubernetes éphémère (image de capture,
Java/JTOpen), pas dans le Pod control plane. Même mécanique pour
`POST /v2/sources/{id}/test` avec `KubernetesJobSourceProbe`. Détail du
mécanisme (retour du résultat par les journaux du pod, nettoyage
systématique, RBAC) : `docs/orchestration.md` §10.

Chaque table découverte est classée par
`quadringent.table_discovery.classify_table` en un `readiness` :

| `readiness` | Condition | Commandes CL proposées |
|---|---|---|
| `ready` | journalisée, images `*BOTH`, clé (primaire ou index unique) trouvée | aucune |
| `not_journaled` | pas de journal actif sur le fichier | `STRJRNPF` (journal déjà présent dans la bibliothèque), ou `CRTJRNRCV` + `CRTJRN` + `STRJRNPF` (aucun journal utilisable) |
| `images_incomplete` | journalisée mais en images `*AFTER` seules | `CHGJRNOBJ ... ATR(*IMAGES) IMAGES(*BOTH)` |
| `no_key` | journalisée en `*BOTH`, mais aucune clé primaire ni index unique | aucune — bascule proposée sur RRN, avec l'avertissement de resynchronisation en cas de `RGZPFM`/`CLRPFM` |

Sur une *sélection* de plusieurs tables (pas par table), un
mésappariement de journal (`journal_mismatch`, tables journalisées dans des
journaux distincts) est signalé séparément par
`quadringent.table_discovery.classify_selection` : chaque journal exige son
propre lecteur, ce qui n'est pas bloquant mais doit être expliqué avant
l'activation du pipeline.

`PATCH /v2/tables/{id}` fixe `key_strategy` (`primary`, `unique_index` ou
`rrn`) et `key_columns` ; `key_strategy=rrn` exige `acknowledge_rrn: true`
dans le corps, sinon `400 invalid_request` — jamais de bascule RRN
silencieuse.

## Tests

```bash
# Suite complète (unitaire, toujours actif — SQLite)
python -m pytest -q tests/test_control_plane_store_postgres.py tests/test_v2_sources.py \
  tests/test_v2_destinations.py tests/test_v2_pipeline_state_machine.py \
  tests/test_v2_actions_envelope.py tests/test_v2_app_skeleton.py tests/test_v2_tables.py \
  tests/test_table_discovery.py \
  tests/test_v2_observation.py tests/test_v2_pipelines_list.py tests/test_v2_pipeline_metrics.py \
  tests/test_v2_pipeline_logs.py tests/test_v2_costs.py tests/test_v2_observation_refresh.py \
  tests/test_v2_observation_projection.py tests/test_v2_observation_storage.py \
  tests/test_v2_observation_composite.py tests/test_k8s_pods.py tests/test_v2_logs_kubernetes.py \
  tests/test_v2_costs_projection.py tests/test_v2_costs_snowflake.py tests/test_v2_costs_composite.py \
  tests/test_v2_scheduler_lock.py tests/test_v2_scheduler.py tests/test_v2_app_default_wiring.py
python -m pytest -q tests/test_v2_*.py tests/test_control_plane_store_postgres.py tests/test_k8s_pods.py

# Tests d'intégration Postgres réels (marqués @pytest.mark.postgres)
# Démarrent un Postgres 16 via Docker (port aléatoire), l'arrêtent en fin de session.
python -m pytest -q -m postgres
```

Les tests marqués `@pytest.mark.postgres` exigent Docker par défaut (pas de
skip silencieux). Sur un poste sans Docker, positionner
`QUADRINGENT_TEST_ALLOW_SKIP_POSTGRES=1` pour un skip explicite et visible.

## Secrets

- `QUADRINGENT_V2_SECRET_KEY` (ou `QUADRINGENT_V2_SECRET_KEY_FILE`) : clé
  Fernet chiffrant le mot de passe IBM i (`sources.secret_ciphertext`), la
  clé privée RSA Snowflake (`destinations.key_pair_ciphertext`) et le
  secret de signature des webhooks (`webhooks.secret_ciphertext`). Absente →
  échec fermé (`SecretKeyUnavailableError`), jamais de clé éphémère générée
  en production.
- `QUADRINGENT_V2_TOKEN_PEPPER` (ou `QUADRINGENT_V2_TOKEN_PEPPER_FILE`) :
  pepper serveur pour le hash des jetons d'agent, des liens d'activation et
  la signature des cookies de session/liens de confirmation (HMAC-sha256).
  Sans valeur déclarée, `create_v2_app` génère un pepper éphémère par
  processus (mode développement uniquement — invalide toute session/jeton
  au redémarrage) ; une production réelle **doit** déclarer cette variable.
  Choix documenté (pas d'argon2id) : un jeton d'agent est une valeur
  aléatoire à haute entropie (32 octets), un hachage lent n'apporte rien
  ici et évite une dépendance native supplémentaire (voir `v2/crypto.py`).
- Mots de passe utilisateurs : hachés avec scrypt (`hashlib` standard, pas
  de dépendance supplémentaire) — voir `crypto.hash_password`.
- Aucune route ne renvoie de secret en clair après sa première émission :
  les sources exposent `secret_set: true`/`false`, les destinations
  n'exposent la clé privée RSA qu'une seule fois (`private_key_pem` dans
  `after`), les jetons d'agent/liens d'activation/secrets de webhook de
  même (`after.token`/`after.activation_token`/`after.secret`).

## Orchestration (chantier 4 — démarrage automatique)

`POST /v2/tables/{id}/pipeline` crée le pipeline (résout `destination_id`
explicite ou l'unique destination de l'organisation, jamais un choix
implicite silencieux si plusieurs existent) et déclenche l'évènement
`start`. Les actions de
`POST /v2/pipelines/{id}/actions/{pause|resume|remove|restart_initial_copy}`
(routées via `PipelinesService.apply_action`, tâches 5/6) sont exécutées
par `KubernetesPipelineExecutor`
(`src/quadringent_control_plane/v2/executor/kubernetes.py`) quand il est
injecté comme `pipeline_executor` de `create_v2_app` — un `Deployment` par
`(source, journal)`, mis à jour de façon idempotente ; un `Job` de copie
initiale par table pour `start`/`restart_initial_copy`. `replay` (rejeu
borné) est implémenté côté exécuteur (`replay_range`) mais **pas encore
relié** à la route `POST /v2/pipelines/{id}/actions/replay`. La transition
`copying -> live` (évènement `bootstrap_completed`) est déclenchée par la
boucle de réconciliation de fond (`services/reconciler.py`, optionnelle —
`reconciliation_executor`/`reconciliation_interval_seconds` de
`create_v2_app`), qui bascule aussi un pipeline en `attention` si le Job de
copie échoue. Protocole de bascule journal, objets Kubernetes produits,
provisionnement des Secrets référencés, RBAC minimal requis et liste
complète des limites de ce chantier : voir `docs/orchestration.md`.

`POST /v2/sources/{id}/test` accepte une sonde IBM i réelle injectable
(`source_probe` de `create_v2_app`, `SourceProbeProtocol`) ; sans elle, le
comportement précédent est conservé (`reachable: "unknown"`). Voir
`docs/orchestration.md` §8 pour la table de correspondance `QTIMZON` →
IANA et §9 pour le câblage réel de `POST /v2/sources/{id}/tables/refresh`.

`POST /v2/destinations/{id}/verify` accepte de même un vérificateur
Snowflake réel injectable (`destination_verifier` de `create_v2_app`,
`DestinationVerifierProtocol`,
`src/quadringent_control_plane/v2/services/destination_verifier.py`) ; sans
lui, réponse honnête `verified: "unknown"` (même discipline que
`sources.test` sans sonde). Le vérificateur par défaut
(`SnowflakeKeyPairVerifier`, câblé dans `v2/entrypoint.py::build_app`) ouvre
une session par paire de clés (JWT, même connexion que
`scripts/quadringent_destination_loader.py::_connect_snowflake`), confirme
le rôle/warehouse de service courants, l'accès au périmètre déclaré et
persisté à la création, puis prouve `CREATE TABLE` sur ses schémas par une création de table réelle aussitôt détruite — couvrant
l'écriture des tables historique/miroir et le `MERGE` du chargeur sans
GRANT distinct (le rôle est propriétaire de ses tables, voir
`services/destinations.py::_build_setup_script`). Chaque étape (connexion,
rôle, warehouse, base, schéma, droits de chargement) est rapportée
séparément (`ok`/`detail`), jamais de secret dans la réponse.

### Périmètre Snowflake déclaré à la création

`POST /v2/destinations` accepte `destination_database` (défaut
`QUADRINGENT`) et `destination_schema` (optionnel). Sans schema explicite,
le script et la vérification conservent `RAW` et `CURATED` dans la base
choisie, y compris si celle-ci est personnalisée. Avec un schema explicite,
l'historique et le miroir utilisent uniquement celui-ci : le rôle de service
reçoit `USAGE, CREATE TABLE` et le rôle lecteur reçoit `SELECT` sur ce scope.
Les identifiants suivent le contrat existant de configuration du site
(lettre ou `_` initial, puis lettres, chiffres, `_` ou `$`, 63 caractères
maximum), sont normalisés en majuscules et validés avant génération RSA.

Les deux champs sont persistés et relisibles par `GET`. La migration
`0016_destination_scope` conserve les scripts, clés et états existants,
avec `QUADRINGENT` et schema nul pour les destinations historiques.
La création garde `verification_state=declared_not_verified`. Seul le
vérificateur peut faire évoluer cet état ; un corps de requête `/verify`
ne peut pas remplacer le scope enregistré ni déclarer un résultat réussi.
Le script crée un utilisateur `TYPE=SERVICE`, avec authentification RSA,
sans modifier les comptes humains ni leur politique MFA.

Le chargeur v2 relit le périmètre de la destination liée au pipeline : les
valeurs globales du site ne remplacent jamais `destination_database` ou
`destination_schema`. L'assistant permet de saisir ces deux champs et conserve
le schéma nul quand le champ facultatif reste vide. La déclaration API
n'applique pas de SQL et ne modifie pas la configuration du site. La
vérification réussie atteste ce scope déclaré ; elle ne prouve pas encore une
ingestion CDC.

Le Deployment transmet la base et le schéma historique résolus dans
`QUADRINGENT_DESTINATION_DATABASE` et `QUADRINGENT_DESTINATION_SCHEMA`, et le
schéma miroir dans `QUADRINGENT_MIRROR_SCHEMA`. Pour une destination historique,
ils valent respectivement la base persistée, `RAW` et `CURATED`. DDL, chargement
SQL/Snowpipe, MERGE et mesures utilisent ces périmètres. Les anciens appelants
et manifestes sans variable miroir conservent leur schéma unique. Avant upgrade,
arrêter les anciens chargeurs et inventorier leurs tables et checkpoints : si
un chargeur a écrit sous les anciennes valeurs globales, ne pas reprendre sur
des tables cibles vides avec ses checkpoints existants. Restaurer les données
dans le scope déclaré et effectuer une nouvelle copie initiale contrôlée,
puis vérifier les lignes avant reprise ; aucun déplacement automatique.
Le réconciliateur arrête un chargeur observé dont la base, le schéma historique
ou le schéma miroir diffère, sans remplacer son périmètre, et refuse la reprise.
Après préparation de la nouvelle copie, retirer ce Deployment arrêté avant
recréation. Le chargeur lie aussi chaque checkpoint de table/run à ses tables
HISTORY/MIRROR dans un marqueur immuable `loader-scopes/*.json` du stockage brut,
avant toute connexion Snowflake. Un autre périmètre, un marqueur illisible ou
un checkpoint existant sans marqueur bloque le démarrage, même après pause,
suppression du Deployment ou restauration. Une nouvelle copie utilise un
autre checkpoint et ne remet jamais l'ancien curseur à zéro. Sauvegarder et
restaurer les marqueurs (compte et tables cibles) avec le brut et les checkpoints ;
une sauvegarde ancienne sans cette provenance nécessite une nouvelle copie initiale.

## Actions de pause au niveau source/destination/organisation

Les actions de pipeline (§2.4) portent sur un pipeline précis. Trois
niveaux supplémentaires (chantier MCP/CLI, migration
`0006_source_destination_pause`, complétés chantier 4) :

- `POST /v2/sources/{id}/actions/{pause,resume}` — pose l'intention
  opérateur (`sources.paused_at`) **et** pause/reprend réellement chaque
  pipeline `copying`/`live` de la source via l'exécuteur injecté
  (`PipelinesService.apply_scope_action`). Une table pausée
  individuellement (`POST /v2/pipelines/{id}/actions/pause`) n'est jamais
  relancée par la reprise de la source
  (`pipelines.paused_by_scope_action`, migration
  `0008_pipeline_scope_pause_marker` — distingue une pause de portée d'une
  pause individuelle). `after.pipelines` détaille `{applied, skipped}` par
  pipeline ; un évènement SSE `pipeline.state_changed` est émis pour
  chaque pipeline affecté.
- `POST /v2/destinations/{id}/actions/{pause,resume}` — même principe côté
  destination.
- `POST /v2/actions/{pause_all,resume_all}` — portée organisation entière
  (toutes les sources, destinations et pipelines déclarés) ; **toujours
  confirmée** (même mécanisme `pending_confirmation_required` que les
  actions sensibles de pipeline, §2.4) sauf `confirmation_token` valide
  fourni.

Sans `pipeline_executor` injecté sur `create_v2_app`, une portée qui
contiendrait au moins un pipeline à piloter renvoie `503
executor_unavailable` (jamais un pilotage partiel silencieux) ; une portée
sans aucun pipeline (déploiement encore vide) reste un no-op sûr.

Les trois supportent `dry_run` (enveloppe standard, §2.5, avec
`dry_run.pipelines` détaillant `{would_transition, skipped}`) et
`Idempotency-Key`.

## OIDC (tâche 10, optionnel)

Contrat §6.2. **Désactivé par défaut** : sans `oidc_config` déclarée sur
`create_v2_app(...)`, aucune route `/v2/auth/oidc/*` n'est montée — la
connexion par mot de passe (`services/users.py`) reste le seul mode
d'identité humaine, inchangé. Quand elle est déclarée, OIDC n'est qu'une
autre façon d'obtenir un `UserRecord` authentifié : le cookie de session
posé après un callback réussi est **exactement** celui de la connexion par
mot de passe (même nom, même TTL, même signature).

Flux Authorization Code + PKCE (S256), sans table « flux en cours » —
l'état (`state`, `nonce`, `code_verifier`) est porté par un cookie
`HttpOnly` signé (HMAC, même pepper que les sessions), posé à
`GET /v2/auth/oidc/login` et vérifié à `GET /v2/auth/oidc/callback` :

```python
from quadringent_control_plane.v2.oidc import OidcConfig

app = create_v2_app(
    engine=engine,
    secret_box=secret_box,
    oidc_config=OidcConfig(
        issuer="https://idp.example.test",
        client_id="...",
        client_secret="...",
        redirect_uri="https://control-plane.example.test/v2/auth/oidc/callback",
    ),
    oidc_http_client=my_http_client,  # voir OidcHttpClient — get_json/post_form injectés
)
```

`oidc_http_client` (protocole `OidcHttpClient`, `v2/oidc.py`) est injecté
— jamais de réseau réel non contrôlé, même discipline que
`WebhookHttpClient`/`TableDiscoveryClientProtocol`. Le callback :

1. Vérifie le cookie d'état (signature, expiration, `state` == paramètre
   de requête).
2. Échange le code contre un `id_token` (`token_endpoint`).
3. Vérifie l'`id_token` (signature RS256 via JWKS, `iss`, `aud`, `nonce`,
   champs requis) — `PyJWT`, fail-closed à chaque étape
   (`OidcError` → `400 invalid_request`).
4. Résout l'utilisateur (`UsersService.link_or_create_oidc_user`) :
   utilisateur déjà lié à ce `sub` → c'est lui ; sinon utilisateur existant
   avec cet email (créé par invitation) → lié, rôle **jamais écrasé** ;
   sinon nouvel utilisateur `reader` créé et activé immédiatement (l'IdP a
   déjà vérifié l'identité). **OIDC ne crée jamais d'admin** — le premier
   admin s'active toujours par mot de passe
   (`POST /v2/setup/first-admin`).

Tests (fournisseur d'identité factice signant avec une clé RSA de test,
aucun réseau réel) : `tests/test_v2_oidc.py`.

## Serveur MCP (`/mcp`)

Contrat §4. Un serveur [MCP](https://modelcontextprotocol.io) (SDK Python
officiel, paquet `mcp` 2.x — `mcp.server.mcpserver.MCPServer`, l'ancien nom
`FastMCP`) est monté **in-process** sous `/mcp` (streamable HTTP) dans la
même application FastAPI que `/v2` — voir
`src/quadringent_control_plane/v2/mcp_server.py`. Authentification :
jeton d'agent Bearer (même mécanisme que `/v2`, §6.2/§8) — l'en-tête
`Authorization` du client MCP est retransmis tel quel à la route REST
équivalente. Conséquence directe : en mode « authentification exigée »
(voir « Identité » ci-dessus), un appel d'outil MCP sans jeton d'agent
Bearer valide échoue avec la même erreur `401 invalid_request` que
l'appel REST équivalent — aucune route de garde séparée n'est nécessaire
sur le montage `/mcp` lui-même.

**Aucune logique métier n'est dupliquée entre `/v2` et `/mcp`.** Chaque
outil MCP construit un client `httpx` interne (transport ASGI, zéro
réseau) et appelle la route `/v2` correspondante — `dry_run`,
`Idempotency-Key` (auto-générée par appel), confirmations et
enregistrement d'audit passent tous par le même code
(`http.py::idempotent_write`) que pour un appel REST humain. L'en-tête
`X-MCP-Client` (dérivé des métadonnées `clientInfo` du protocole MCP,
présentes quel que soit le transport) est transmis à chaque appel et
enregistré dans `audit_records.mcp_client` (voir §11 — audit).

### Outils

| Outil | Route `/v2` appelée | Sensible (confirmation) |
|---|---|---|
| `list_sources` | `GET /v2/sources` | — |
| `test_source` | `POST /v2/sources/{id}/test` | — |
| `pause_source` / `resume_source` | `POST /v2/sources/{id}/actions/{pause,resume}` | non |
| `pause_destination` / `resume_destination` | `POST /v2/destinations/{id}/actions/{pause,resume}` | non |
| `verify_destination` | `POST /v2/destinations/{id}/verify` | — |
| `list_tables` | `GET /v2/sources/{id}/tables` | — |
| `refresh_tables` | `POST /v2/sources/{id}/tables/refresh` | non |
| `choose_table_key` | `PATCH /v2/tables/{id}` | non |
| `list_pipelines` | `GET /v2/pipelines` | — |
| `get_pipeline` | `GET /v2/pipelines/{id}` | — |
| `pause_pipeline` / `resume_pipeline` | `POST /v2/pipelines/{id}/actions/{pause,resume}` | non |
| `restart_initial_copy` | `POST /v2/pipelines/{id}/actions/restart_initial_copy` | **oui** |
| `replay_journal_range` | `POST /v2/pipelines/{id}/actions/replay` | **oui** |
| `remove_table` | `POST /v2/pipelines/{id}/actions/remove` | **oui** |
| `pause_all` / `resume_all` | `POST /v2/actions/{pause_all,resume_all}` | **oui** |
| `list_pending_confirmations` | `GET /v2/confirmations?state=pending` | — |
| `get_costs` | — (renvoie `{"status": "absent"}` : aucun service de coûts `/v2` câblé dans ce chantier) | — |
| `get_audit` | `GET /v2/audit` | — |

Tous les outils d'écriture acceptent `dry_run: bool` (défaut `false`).
Les outils sensibles acceptent en plus `confirmation_token: str | None` ;
sans confirmation approuvée, ils renvoient
`{"status": "pending_confirmation", "confirmation_id", "approve_url", "reason"}`
au lieu de s'exécuter — un jeton d'agent ne peut jamais approuver sa
propre action sensible, sauf s'il porte l'action dans ses
`pre_authorized_actions` déclarées à sa création (§6.2/§8, inchangé pour
MCP).

### Ressources

| URI | Contenu |
|---|---|
| `quadringent://docs/api` | Document OpenAPI 3.1 complet de `/v2` (JSON) |
| `quadringent://docs/errors` | Catalogue des codes d'erreur `/v2` (§2.6) |
| `quadringent://docs/llms.txt` | Point d'entrée court pour un agent découvrant ce control plane — aussi servi en HTTP brut sur `GET /llms.txt` |
| `quadringent://state/overview` | Vue d'ensemble courante : sources, destinations, pipelines, confirmations en attente |

### Bout-en-bout : test d'onboarding scripté

`tests/test_v2_mcp_e2e_onboarding.py` fait dérouler à un agent scripté un
onboarding complet via un vrai client MCP streamable HTTP (SDK officiel —
pas de raccourci `MCPServer.call_tool` sans transport) : liste des tables,
choix de clé, pause d'une source, tentative de retrait d'un pipeline
(confirmation en attente), approbation humaine en REST, rejeu de l'action
par l'agent. Il vérifie que `audit_records` distingue
`actor_kind='agent'` (avec `mcp_client` peuplé) de `actor_kind='human'`.

## CLI `quadringent` (client `/v2`)

Contrat §5. La CLI `quadringent` (déjà installée par le paquet, voir
`docs/product/install-default.md` pour `install`/`uninstall`/`status`) est
étendue de sous-commandes clientes de `/v2` — un seul point d'entrée, pas
de second binaire (`src/quadringent/installer/v2_commands.py`,
`api_client.py`, `mcp_bridge.py`). Toute sortie est **du JSON uniquement**,
sur stdout, sous la forme `{"result": <corps /v2>, "idempotency_key": "..."}`
— jamais de texte libre, pour rester scriptable par un humain ou un agent.

### Configuration

Dans cet ordre :

1. `QUADRINGENT_URL` (+ `QUADRINGENT_TOKEN` optionnel).
2. `~/.quadringent/cli.json` — `{"url": "...", "token": "..."}`,
   permissions **`0600` exigées** (refusé sinon, fail-closed) :
   ```bash
   umask 077
   cat > ~/.quadringent/cli.json <<'JSON'
   {"url": "https://control-plane.example.test", "token": "qdt_op_..."}
   JSON
   ```

### Commandes

```
quadringent sources       list | get <id> | test <id> | pause <id> | resume <id>
quadringent destinations  list | get <id> | pause <id> | resume <id>
quadringent tables        list <source_id> | refresh <source_id> | choose-key <table_id>
quadringent pipelines     list | get <id> | pause <id> | resume <id> |
                           restart-initial-copy <id> | replay <id> | remove <id>
quadringent actions       pause-all | resume-all
quadringent confirmations list [--state pending] | approve <id> | reject <id>
quadringent tokens        list | create --name ... --scope ... | rotate <id> | revoke <id>
quadringent users         list | invite --email ... --role ...
quadringent audit         tail [--actor-kind human|agent] [--resource-type ...] [--limit N]
quadringent events        stream [--last-event-id N]
quadringent webhooks      list | create --url ... --event ... | delete <id>
quadringent mcp --stdio
```

Toute commande d'écriture accepte `--dry-run` (prévisualise l'effet sans
l'appliquer, enveloppe `dry_run` renvoyée telle quelle) et
`--idempotency-key` (sinon générée — `cli-<uuid4>` — et toujours affichée
dans la sortie, pour être réutilisée si la commande doit être rejouée à
l'identique). Les actions sensibles de pipeline et `actions pause-all`/
`resume-all` acceptent `--confirmation-token <id>` (le flux
`pending_confirmation` → `confirmations approve` → rejeu avec le jeton est
identique à MCP, voir ci-dessus).

Exemple :

```bash
$ quadringent pipelines remove pipe_abc123
{
  "result": {"error": {"code": "pending_confirmation_required", ...}},
  "idempotency_key": "cli-3f2a..."
}
$ echo $?
6
$ quadringent confirmations list --state pending
$ quadringent confirmations approve conf_xyz789
$ quadringent pipelines remove pipe_abc123 --confirmation-token conf_xyz789
```

### Codes de sortie

Dérivés du catalogue d'erreurs `/v2` (§2.6) — un script appelant peut
brancher sur le code exact sans parser le JSON :

| Code | Signification |
|---|---|
| `0` | succès |
| `2` | `invalid_request` |
| `3` | `not_found` |
| `4` | `insufficient_role` / `wrong_confirmation` / `wrong_environment` |
| `5` | `idempotency_key_conflict` |
| `6` | `pending_confirmation_required` |
| `7` | `capability_unavailable` / `action_in_progress` |
| `8` | `store_unavailable` / `executor_unavailable` |
| `9` | erreur réseau (connexion, timeout) |
| `10` | erreur de configuration (URL/jeton introuvables ou fichier `cli.json` mal protégé) |
| `1` | erreur inconnue/non catégorisée |

### `quadringent mcp --stdio`

Pont stdio ↔ `/mcp` distant, pour un client MCP local qui ne parle pas
streamable HTTP nativement (ou pour l'exécuter derrière un lanceur de
process, à la manière d'un serveur MCP classique). Construit exactement le
même `MCPServer` que le montage in-process (mêmes outils, même
documentation), avec un seul changement : les appels partent en HTTP
réseau vers `QUADRINGENT_URL` (jeton `QUADRINGENT_TOKEN`, ou
`~/.quadringent/cli.json`) au lieu d'un transport ASGI local — voir
`docs/agents.md` pour la configuration côté client (Claude Code, Claude
Desktop, Codex...).

`httpx`/`mcp` sont importés à la demande dans `api_client.py`/
`mcp_bridge.py`, jamais au niveau module : `quadringent install`/
`uninstall`/`status` (paquet de base, sans l'extra `api`) continuent de
fonctionner sans ces dépendances installées.
