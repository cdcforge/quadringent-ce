# Changelog

Format [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).
Voir la [politique de livraison](docs/releasing.md).

## [Unreleased]

Réservé aux changements postérieurs à la préversion 0.2.4.

## [0.2.4] — préversion DEV en préparation

### Sécurité et installation

- Les exemples de comptes Snowflake et les descriptions de tests utilisent
  des identifiants synthétiques, sans référence à un environnement réel.
- L'émission d'un jeton d'agent vérifie le chemin Python sûr depuis un
  répertoire contenant le shim du control plane, avec authentification et audit.
- Le guide EKS précise les prérequis de stockage persistant, les vérifications
  du pilote EBS CSI et le partage de propriété de son compte de service.

## [0.2.3] — préversion DEV en préparation

### Sécurité

- Les images control plane et vérificateur verrouillent `urllib3` en 2.8.0,
  avec les empreintes des distributions. Cette version corrige les alertes
  HIGH CVE-2026-97687 et CVE-2026-97689 qui ont bloqué la tentative de release
  0.2.2. Aucun artefact de cette tentative n'a été promu ou annoncé.
  [Correctifs de l'éditeur](https://github.com/urllib3/urllib3/releases/tag/2.8.0).
- Le vérificateur verrouille `PyJWT` en 2.14.0. Le scan actualisé avait
  détecté six alertes HIGH/CRITICAL corrigées dans cette version ; elles
  sont traitées avant une nouvelle tentative de release.
  [Correctifs de l'éditeur](https://github.com/jpadilla/pyjwt/releases/tag/2.14.0).
- Un scan Trivy des deux verrous Python précède les constructions OCI.
  Une vulnérabilité corrigée HIGH/CRITICAL ou une analyse incomplète bloque
  la release avant les builds coûteux ; les six scans d'images restent requis.

### Réplication et installation

- La destination conserve sa base et son schéma déclarés. Le script SQL et
  la vérification contrôlent ce même périmètre ; un schéma isolé ne réclame
  plus de droits sur les schémas historiques RAW/CURATED. Les valeurs par
  défaut restent compatibles avec les destinations existantes.
- Le chargeur v2 utilise le périmètre persisté de sa destination, même si
  les valeurs globales du site diffèrent. Sans schéma explicite, HISTORY
  utilise RAW et MIRROR utilise CURATED ; les DDL, MERGE, mesures et profils
  Snowpipe suivent ce contrat. L'assistant transmet la base et le schéma.
- L'upgrade arrête un chargeur observé dont le périmètre diffère et refuse
  de réutiliser silencieusement ses checkpoints sur de nouvelles tables.
  Une liaison immuable scope/checkpoint protège aussi la pause/reprise et
  la restauration sans ancien Deployment. Les anciens checkpoints sans
  provenance exigent une nouvelle copie initiale contrôlée, sans remise
  à zéro des curseurs existants.
- Les utilisateurs Snowflake générés sont des comptes `SERVICE` avec clé
  RSA, sans modification du compte humain.
- Le golden Helm suit la version 0.2.3 ; les tests de packaging vérifient les
  scans de chaque image séparément du nouveau scan précoce.

## [0.2.2] — tentative de release bloquée par le scan de sécurité

### Ajouté

- La commande installable `quadringent qualification native` collecte les
  preuves des pods du produit sur GKE, EKS et VM : admission des images,
  identités cloud, oracle IBM i indépendant, rapprochement HISTORY/MIRROR,
  pause/reprise et observations de latence. Les actions DEV sont explicites
  et bornées ; les preuves restent privées. Une réussite locale du harnais
  ne constitue pas une qualification de ces cibles.
- Le chargeur Streaming mesure les étapes de découverte, ouverture de canal,
  append, flush, commit, MERGE et checkpoint des cycles actifs. Le cockpit
  rattache ces journaux à la table concernée ; aucun sondage Snowflake
  supplémentaire n'est ajouté au repos. Ces durées ne remplacent pas la
  mesure de latence entre l'écriture sur IBM i et sa visibilité dans le miroir.

### Corrigé

- Les réponses idempotentes et l'audit n'enregistrent plus les secrets remis
  une seule fois. La migration `0015_one_time_secrets` expurge les copies
  historiques et rattache les rejeux à leur organisation et leur acteur.
- L'authentification et les droits sont vérifiés avant le rejeu. Une activation
  sur le mauvais identifiant d'utilisateur ne consomme plus le lien et ne
  modifie aucun compte. La réémission explicite du lien du premier admin
  encore non activé invalide les anciens liens.
- La CLI écrit les configurations de jetons dans un fichier privé temporaire,
  puis les remplace atomiquement ; un échec préserve le fichier existant.
  L'assistant explique qu'une clé déjà remise ne peut plus être téléchargée.
- Le workflow de release libère le cache du builder éphémère après les
  trois exports OCI, avant l'extraction et les scans. Les exports à scanner
  sont conservés. Les diagnostics de disque et d'inodes permettent de
  distinguer un manque d'espace d'un résultat de sécurité.
- Le reçu des empreintes OCI scannées est conservé un jour dans un artefact
  CI séparé des fichiers de release, pour rapprocher les images installées
  des contrôles exécutés sur le même commit.
- Un second artefact temporaire conserve les 21 fichiers de métadonnées OCI
  originaux liés à ce reçu, après un scan de secrets, sans couches d'images.
  Les scans complets et la promotion par digest restent obligatoires.
- La release réutilise la CI déjà réussie sur le commit exact de son tag,
  avec ses sept contrôles obligatoires, au lieu de les réexécuter.
- La publication du site réutilise également cette preuve CI du commit exact.
  Les runs obsolètes de la même branche sont annulés ; les dépendances et
  couches d'images disposent de caches bornés et les jobs ont des délais maximum.
- Le guide VM GCP explicite l'accès Internet sortant nécessaire à
  l'installation de k3s et aux téléchargements d'images, ainsi que les
  variables Terraform pour une VM de test sans NAT existant.

### Limites de qualification

- Ces changements ne valident pas la chaîne CDC sur EKS ou VM ni la cible
  de latence maximale. La nouvelle construction distante et les essais
  personnels restent nécessaires avant publication.

## [0.2.1] — préversion DEV en préparation

### Ajouté

- L'installateur prend en charge une VM GCP dédiée sans IP publique. Il
  crée k3s, transmet l'identité GCP de la VM aux pods et ouvre un tunnel SSH
  sur IAP pour l'installation et l'exploitation. Une installation DEV réelle
  a vérifié l'API, l'UI, PostgreSQL et l'accès au bucket GCS ; la recopie
  IBM i sur cette cible reste à qualifier.
- L'outil de qualification planifiée sait composer ses adaptateurs réels de
  capture et de fraîcheur. Un scénario complet sur source IBM i et Snowflake
  réels n'a pas encore été exécuté avec cet outil.

### Corrigé

- Le chargeur ne sonde plus Snowflake toutes les 30 s au repos, ce qui
  pouvait maintenir son warehouse actif. Les délais de livraison restent
  mesurés après chaque lot traité et visibles dans le cockpit.
- Près de la queue du journal, le lecteur réutilise le lot produit par sa
  lecture `DISPLAY_JOURNAL` au lieu de relire la même fenêtre par
  RetrieveJournal. Le décodage SQL applique les indicateurs de nullité
  distincts de l'image brute : une colonne nulle ne devient plus une chaîne
  vide. Une comparaison en lecture seule sur la source de qualification
  retrouve les mêmes événements par les deux chemins ; la cible de latence
  maximale de 10 s reste non démontrée.
- L'assistant reprend l'hôte et le compte d'une source IBM i chargée après
  le premier rendu. Il laisse le mot de passe vide et peut retester la
  source avec le secret déjà enregistré. Un nouveau mot de passe crée une
  nouvelle source, et l'écran indique explicitement ce comportement.
- Le module Terraform de VM GCP précise le projet du sous-réseau quand
  l'installateur reçoit son nom court. Une installation réelle a détecté
  l'erreur initiale et confirmé la reprise sans recréer le socle.

### Sécurité de la release

- La construction privée ajoute un scan explicite des variantes arm64 pour
  les vulnérabilités HIGH/CRITICAL corrigibles, contrôle les secrets de
  toutes sévérités sur amd64 et arm64 et joint un SBOM par image et par
  architecture. La preuve d'exécution sur les images de cette version reste
  à établir avant publication.

### Limites de qualification

- Le miroir IBM i → Snowflake est vérifié sur GKE DEV avec des tables
  synthétiques. Le maximum de 10 s n'est pas démontré : des essais à froid
  et certaines mesures ont dépassé cette valeur. EKS et VM sont qualifiés
  pour l'installation et le control plane, pas pour une chaîne CDC complète.

## [0.2.0] — préversion DEV, 29 septembre 2026

### Corrigé

- Les confirmations v2 sont isolées par organisation, y compris lecture,
  émission de jeton et décision. Le parcours d'activation du premier compte
  n'appelle plus une route protégée avant la connexion. Le ServiceAccount
  de capture transmet le secret de registre privé aux pods lecteur,
  chargeur et Jobs gérés.
- La CI installe les dépendances de l'API nécessaires aux tests et compare
  le bundle UI complet, avis de licences compris, à une reconstruction isolée.
- La mesure Snowflake du délai IBM i → miroir conserve désormais les
  millisecondes et le cockpit affiche au plus deux décimales. Un délai
  de 10,05 s n'est plus présenté comme 10 s ; les débits faibles restent
  lisibles. Le scénario de nouvelle copie après un run déjà actif vérifie
  le remplacement de la clé de preuve du chargeur et son redéploiement.

### Ajouté

- Cockpit v2 : journaux et métriques du chargeur Snowflake branchés en
  cluster, filtrés par la table exacte du pipeline. Le chargeur publie une
  mesure de délai IBM i → miroir après chaque MERGE de lot ; la série et
  sa limite de conservation sont indiquées dans l'UI. Au repos, l'âge de
  la dernière mutation reste distinct du délai de livraison. Installation
  et désinstallation du mode VM AWS vérifiées sur une instance DEV temporaire
  depuis un wheel et trois images du même commit.

- Chargeur de destination (historique Snowpipe Streaming + MERGE miroir,
  suite) : un processus par destination
  (`scripts/quadringent_destination_loader.py`), un Deployment K8s par
  destination (`v2/executor/manifests.py::build_loader_deployment`, réplica
  unique via `replicas` 0/1 + stratégie `Recreate` — même garantie que le
  lecteur de journal, sans bail applicatif distinct), réconcilié par
  l'exécuteur v2 (`_reconcile_loader_for_destination`, appelé aux mêmes
  points que le lecteur). Découvre les lots bruts publiés via les reçus de
  fenêtre (`object_store.list_receipt_keys`) avec un repli sans reçus
  (`discover_new_batches_from_manifest_keys`) pour les sites qui publient
  sans fenêtres de preuve. `destinations.service_user`/`service_role`
  (migration `0011_dest_service_identity`) et
  `tables.discovered_columns` (migration `0012_table_columns`,
  `TablesService.set_discovered_columns`, route `PUT /v2/tables/{id}/
  discovered-columns`) complètent le catalogue nécessaire à la création des
  tables. Image control-plane étendue (snowflake-connector-python,
  snowpipe-streaming épinglé 1.8.0, google-cloud-storage) ; build Docker
  réel vérifié. Vérification en direct sur un compte de qualification
  (`QUALIFICATION_DB.DEST_CHECK`) : 110 lignes miroir, 0 écart contre
  l'oracle ; deux défauts réels corrigés à cette occasion
  (`authorization_type=JWT` du profil Snowpipe Streaming,
  `INGESTED_AT DEFAULT CURRENT_TIMESTAMP()` sur la table historique).

- Control plane v2 : bail (lease) applicatif unifié (`v2/services/lease.py`,
  table `leases`, migration `0010_unify_leases`) remplaçant les deux
  mécanismes jumeaux introduits indépendamment par la boucle de
  réconciliation (`reconciler_leases` — `0007_reconciliation`) et
  l'ordonnanceur de rafraîchissement d'observation (`scheduler_locks` —
  `0009_scheduler_locks`) — un seul module, portable SQLite/Postgres, avec
  un compteur de génération (fencing) : un titulaire qui a perdu le bail
  peut désormais le détecter (`Lease.is_current()`) avant d'agir sur la
  ressource protégée, même sans avoir observé l'expiration lui-même.
  `services/reconciler.py::acquire_lease` et
  `services/scheduler_lock.py::SchedulerLock` deviennent de fines
  enveloppes de compatibilité autour de cette abstraction — comportement
  observable inchangé pour les deux boucles. Tests de concurrence
  (acquisition, renouvellement, expiration, reprise, rejet d'un titulaire
  périmé) sur SQLite et sur un vrai Postgres (`@pytest.mark.postgres`).
  Voir `docs/orchestration.md` §7 et `docs/api-v2.md` (« Ordonnancement »).
- Destination Snowflake historique + miroir (`docs/decisions/2026-09-23-miroir-snowflake.md`,
  `docs/product/snowflake-destination.md`) : DDL généré depuis les types IBM i
  découverts (`quadringent.snowflake_destination`, table de correspondance
  CHAR/VARCHAR/GRAPHIC, DECIMAL/NUMERIC, entiers, DATE/TIME/TIMESTAMP(p),
  BINARY, CLOB/BLOB avec rejet explicite au-delà des limites Snowflake) ;
  chargeur Snowpipe Streaming pour l'historique + `MERGE` miroir dédupliqué
  par `event_id` (`quadringent.snowflake_streaming_loader`, canal stable par
  flux, reprise au dernier jeton d'offset, client injectable + faux client de
  test) ; bascule `QUADRINGENT_DESTINATION_MODE=streaming`
  (`copy_merge`/`streaming`, chart `site.destinationMode` +
  `streaming.profileSecret`) sans changer le comportement par défaut ; script
  de mise en service Destination v2 étendu (`CREATE TABLE` sur
  `QUADRINGENT.CURATED`, `EXECUTE TASK` seulement pour l'option A) ; retard
  historique/miroir mesuré et exposé à `PipelineObservation`
  (`history_lag_seconds`/`mirror_lag_seconds`, jamais une valeur inventée).

- Control plane v2 (observabilité, suite) : les fournisseurs injectables des
  routes de lecture précédentes sont désormais adossés à des adaptateurs
  réels, toujours en repli sur `Null*` sans configuration (`docs/api-v2.md`,
  section « Adaptateurs réels ») :
  - `ProjectionRepositoryObservationAdapter`/`StorageBackendObservationAdapter`/
    `CompositeObservationProvider` (`v2/services/observation_projection.py`,
    `observation_storage.py`, `observation_composite.py`) : état/retard/
    lignes depuis un document console/projection v1 réel
    (`repository.ProjectionRepository`, inchangé) ; débit/dernière arrivée
    depuis deux relevés successifs du curseur de capture
    (`quadringent.storage_backend.StorageBackend.checkpoint_store`, AWS ou
    GCS) — jamais une estimation sur un seul point, jamais un dénombrement
    de lignes déduit d'un simple curseur.
  - `KubernetesLogSource` (`v2/services/logs_kubernetes.py`) + nouveau
    client `k8s_pods.py` (lecture seule bornée, symétrique de
    `k8s_jobs.py`/`k8s_deployments.py`) : journaux des pods sélectionnés
    par le label `quadringent.io/pipeline-id` posé par l'exécuteur
    (`v2/executor/manifests.py`), horodatage Kubernetes réel
    (`timestamps=true`), rédaction toujours centrale
    (`services/logs.py::redact`).
  - `CostsV1ProjectionAdapter` (réutilise `costs.project_costs` v1 tel
    quel, portée connexion uniquement) et `SnowflakeWarehouseCostsAdapter`
    (crédits d'un warehouse mesurés en direct via une requête injectable,
    jamais de montant sans prix par crédit déclaré, statut `estimated`
    tant que la facturation Snowflake n'est pas finalisée) ; combinables
    via `FallbackCostsProvider` (`v2/services/costs_composite.py`).
  - `ObservationRefreshScheduler` (`v2/services/scheduler.py`) : rafraîchit
    `ObservationRefreshService.refresh_all` à intervalle régulier, protégé
    par un verrou à bail portable SQLite/Postgres
    (`SchedulerLock`/`scheduler_locks`, migration `0006_scheduler_locks`)
    — sûr en plusieurs réplicas, un réplica mort libère son bail de
    lui-même. Désactivé par défaut ; démarré/arrêté proprement par
    `create_v2_app(enable_observation_scheduler=True, ...)`.
  - `create_v2_app(...)` câble automatiquement ces adaptateurs dès que la
    configuration le permet (résolveurs id v2 -> source v1, client
    Kubernetes, requête de crédits Snowflake) — un fournisseur explicite
    prime toujours ; sans configuration, comportement inchangé (Null\*).
  Tests : services + intégration `create_v2_app` (SQLite) et un round-trip
  Postgres réel pour le verrou à bail.

- Chantier 4 (« démarrage automatique ») — exécuteur Kubernetes v2 réel,
  `src/quadringent_control_plane/v2/executor/` (`boundary.py`,
  `manifests.py`, `evidence.py`, `reconcile.py`, `kubernetes.py`) et
  `src/quadringent_control_plane/k8s_deployments.py` (client Deployments,
  symétrique de `k8s_jobs.py`). `KubernetesPipelineExecutor` implémente
  `PipelineExecutorProtocol` (`v2/services/pipelines.py`) : un `Deployment`
  de capture continue par `(source, journal)` — un seul lecteur, jamais un
  par table —, un `Job` de copie initiale par table (`run_id` UUID, jamais
  réutilisé), un `Job` de rejeu borné par plage de séquences ; pause/reprise
  par mise à jour idempotente du jeu de tables du `Deployment` (jamais de
  perte de checkpoint) ; transition `copying -> live` autorisée par une
  preuve durable écrite par le Job lui-même (jamais par le control plane).
  Protocole de bascule journal documenté et justifié dans
  `docs/orchestration.md` (position lue avant la copie, jamais reculée,
  risque résiduel pendant la copie couvert par le MERGE ordonné par
  position du miroir). Réconciliation désiré/observé idempotente par
  empreinte de `spec` (annotation `quadringent.io/spec-sha256`) — sûre au
  redémarrage du control plane. 45 tests (`tests/test_v2_executor_*.py`),
  entièrement hors ligne (clients Kubernetes et sonde IBM i factices).
- `POST /v2/sources/{id}/test` : sonde IBM i réelle injectable
  (`SourceProbeProtocol`, `v2/services/source_probe.py`) — réseau, TLS,
  authentification, version, fuseau détecté depuis `QTIMZON` (table de
  correspondance vers IANA ; valeur inconnue jamais devinée,
  `timezone_ambiguous: true`). Sans sonde branchée, comportement historique
  conservé (`reachable: "unknown"`). Champs détectés persistés uniquement
  si la source est effectivement joignable.
- Découverte de tables (`POST /v2/sources/{id}/tables/refresh`) :
  adaptateur réel `PersistentJavaWorkerTableDiscoveryClient`
  (`v2/services/table_discovery_client.py`) câblant
  `quadringent.java_worker.PersistentJavaWorker.discover()` au contrat
  `TableDiscoveryClientProtocol`, via `parse_discover_output` (même
  parsing que le CLI/worker historique).

- Control plane v2 : routes de lecture qui manquaient au cockpit
  (`ui/src/data/controlPlaneV2Client.ts`, sections « CONTRAT SEUL ») —
  `GET /v2/pipelines` (liste paginée, filtres `state`/`source_id`/
  `destination_id`, état déclaré combiné aux figures en direct d'un
  fournisseur d'observation injectable), `GET /v2/pipelines/{id}/metrics
  ?window=1h|24h` (série retard/débit avec provenance et fraîcheur),
  `GET /v2/pipelines/{id}/logs?since=&level=&correlate_incident=` (source de
  journaux injectable, rédaction systématique des secrets et des blocs
  ressemblant à de la donnée de ligne — voir `docs/api-v2.md`, section
  « Observabilité v2 »), `GET /v2/costs?scope=connection|table&id=&window=`
  (mesuré/estimé/absent, jamais de montant inventé). Chaque fournisseur
  (`pipeline_observation_provider`, `log_source`, `costs_provider`) est
  injectable sur `create_v2_app(...)`, par défaut absent (échec fermé,
  aucune valeur inventée) — même discipline que `pipeline_executor`/
  `table_discovery_client` déjà en place. `ObservationRefreshService`
  publie `pipeline.state_changed`/`alert.fired`/`alert.resolved` via
  `EventsService` (SSE, tâche 12) sur un vrai changement d'état observé,
  persisté dans `pipelines.last_observed_state`/`last_observed_at`
  (migration `0005_pipeline_last_observed`). Tests : services + routes
  (SQLite) et un round-trip Postgres réel pour la liste des pipelines
  (`@pytest.mark.postgres`).

- Pause/reprise de source, destination et organisation (`/v2/sources/{id}/actions/*`,
  `/v2/destinations/{id}/actions/*`, `/v2/actions/{pause_all,resume_all}`,
  chantier MCP/CLI) **agissent réellement sur les flux** : au-delà du
  marqueur d'intention (`paused_at`), chaque pipeline `copying`/`live` de la
  portée est pausé/repris via l'exécuteur, sans jamais relancer une table
  que l'utilisateur avait mise en pause individuellement
  (`pipelines.paused_by_scope_action`, migration `0007_pipeline_scope_pause_marker`).
  Évènements SSE `pipeline.state_changed` émis pour chaque pipeline
  affecté, audit via l'enveloppe générique existante.
- Control plane v2 : serveur MCP in-process monté sous `/mcp` (streamable
  HTTP, SDK officiel `mcp` 2.x), protégé par les mêmes jetons d'agent que
  `/v2` — chaque outil appelle la route REST équivalente (`dry_run`,
  `Idempotency-Key`, confirmations, audit passent tous par le même code
  que les humains, aucune logique dupliquée). Outils : `list_sources`,
  `test_source`, `pause_source`/`resume_source`,
  `pause_destination`/`resume_destination`, `list_tables`,
  `refresh_tables`, `choose_table_key`, `list_pipelines`, `get_pipeline`,
  `pause_pipeline`/`resume_pipeline`, `restart_initial_copy`,
  `replay_journal_range`, `remove_table`, `pause_all`/`resume_all`,
  `list_pending_confirmations`, `get_costs` (« absent » — aucun service de
  coûts `/v2` dans ce chantier), `get_audit`. Ressources :
  `quadringent://docs/{api,errors,llms.txt}`,
  `quadringent://state/overview` ; `/llms.txt` servi aussi en HTTP brut.
  Nouvelles routes REST associées (migration 0006_source_destination_pause) :
  `/v2/sources/{id}/actions/{pause,resume}`,
  `/v2/destinations/{id}/actions/{pause,resume}`,
  `/v2/actions/{pause_all,resume_all}` (toujours confirmées),
  `GET /v2/pipelines`.
- Control plane v2 : OIDC (Authorization Code + PKCE), **optionnel et
  désactivé par défaut** — actif seulement si `oidc_config` est déclarée
  sur `create_v2_app`. `GET /v2/auth/oidc/login|callback` ; le callback
  vérifie le `id_token` (RS256/JWKS via `PyJWT`), lie ou crée
  `users.oidc_subject`, puis pose exactement le même cookie de session que
  la connexion par mot de passe. OIDC ne crée jamais d'admin. Tests avec
  un fournisseur d'identité factice (clé RSA de test), aucun réseau réel.
- CLI `quadringent` : sous-commandes `/v2` (sortie JSON uniquement) —
  `sources`, `destinations`, `tables`, `pipelines` (dont
  `restart-initial-copy`/`replay`/`remove` avec `--dry-run` et
  `--idempotency-key` auto-générée et affichée), `actions`
  (`pause-all`/`resume-all`), `confirmations` (`list`/`approve`/`reject`),
  `tokens` (`create`/`list`/`rotate`/`revoke`), `users`, `audit tail`,
  `events stream` (Server-Sent Events → une ligne JSON par évènement),
  `webhooks`. Configuration via `QUADRINGENT_URL`/`QUADRINGENT_TOKEN` ou
  `~/.quadringent/cli.json` (`0600` exigé, fail-closed sinon). Codes de
  sortie stables, dérivés du catalogue d'erreurs `/v2` (§2.6). Ajoute
  `quadringent mcp --stdio` : pont stdio ↔ `/mcp` distant, pour un client
  MCP local (Claude Code, Claude Desktop, Codex...).
- UI : assistant de connexion v2 (`#/wizard/activate|source|snowflake|tables`),
  premier lancement en quatre écrans autonomes (sans la coquille de navigation
  du cockpit) :
  - Activation admin par lien à usage unique (jeton dans l'URL).
  - Source IBM i : hôte/compte/mot de passe avec validation en direct, Tester
    avec une ligne par contrôle (réseau, certificat — confiance explicite et
    empreinte pour une autorité privée —, authentification, version IBM i,
    fuseau détecté), ports avancés repliés.
  - Snowflake : identifiant de compte seul (jamais de secret admin), script
    SQL généré copiable/téléchargeable, Vérifier avec un résultat par
    contrôle (rôle/utilisateur/entrepôt/base).
  - Tables : liste recherchable (papier listing), état par table (« prête /
    non journalisée / images incomplètes / sans clé », `ui/src/domain/wizardTables.ts`,
    traduit de `journal_status`/`key_status` du contrat v2 §2.3 sans jamais
    exposer un code brut), panneau de commandes CL copiables + Revérifier,
    choix de clé avec acquittement explicite pour la RRN, Démarrer (avec
    confirmation) puis renvoi vers la vue en direct du pipeline.
  Client typé `/v2` (`ui/src/data/controlPlaneV2Client.ts` : test de source,
  création/vérification de destination, catalogue de tables, choix de clé,
  démarrage de pipeline, activation admin — en-tête `Idempotency-Key` généré
  par écriture, enveloppe d'erreur `{code,message,next_action,retryable}`
  typée). Mode démo sans backend (`ui/src/data/fixtures/wizardDemo.ts`),
  chargé dynamiquement uniquement en développement (`ui/src/data/wizardClient.ts`) :
  aucune fixture n'est expédiée dans le build de production.
  L'ancien `Setup.tsx` (v1, `#/setup`) reste intact et accessible ; le nouvel
  assistant n'y est pas encore lié depuis la navigation (accessible
  directement via l'URL) — choix fait pour ne prendre aucun risque de
  régression sur `Setup.test.ts`, à trancher par le product owner.

- UI : première tranche du cockpit v2 (docs/plans/
  2026-09-23-produit-fini-design.md §3), non branchée à la navigation —
  fondations client + panneau de contrôles seulement, aucun écran des trois
  niveaux (accueil, connexion, table) livré dans cette tranche :
  - `ui/src/data/controlPlaneV2Client.ts` étendu : pipeline get/liste,
    actions pipeline (`pause`/`resume`/`remove`/`restart_initial_copy`/
    `replay`) avec `dry_run` + `confirmation_token`, actions source/
    destination/flotte (`pause`/`resume`/`pause_all`/`resume_all`),
    métriques retard/débit, journaux filtrés (jamais de donnée de ligne),
    coûts mesuré/estimé/absent, confirmations (liste/approve/reject),
    audit interrogeable, flux `/v2/events` (SSE) avec reprise
    `Last-Event-ID` et reconnexion à backoff borné. Les routes non encore
    servies par le control plane v2 (liste des pipelines, métriques,
    journaux, coûts, actions source/destination/flotte — confirmé hors
    périmètre du chantier « control-plane-v2 fondation » parallèle) sont
    codées contre le contrat et marquées « CONTRAT SEUL » en commentaire :
    elles échoueront tant que le serveur ne les sert pas, jamais de donnée
    inventée en repli.
  - `ui/src/domain/controlsPanel.ts` : machine à états pure du panneau de
    contrôles (effet annoncé → confirmation → exécution → vérification par
    relecture ; une action sensible en attente d'approbation reste affichée
    jusqu'à approbation/rejet explicite).
  - `ui/src/components/ControlsPanel.tsx` : panneau réutilisable aux quatre
    niveaux (table/connexion/destination/flotte), n'affiche que les actions
    que l'appelant déclare disponibles.
  - Complété par une tranche suivante (même worktree, commits ultérieurs) :
    mode démo dev-only du cockpit (deux connexions, huit tables, un
    incident, une table en pause, une copie en cours — `ui/src/data/
    fixtures/cockpitDemo.ts`, jamais dans le bundle de production, vérifié
    par `verify-build.mjs`) ; trois écrans du cockpit v2 sous `#/cockpit`,
    `#/cockpit/connection/:id`, `#/cockpit/table/:id/:tab` (accueil une
    ligne par connexion avec bandeau d'attention unique ; connexion en
    BandedTable triable/filtrable avec contrôles connexion/destination/
    flotte ; table avec les cinq onglets Métriques (graphe SVG léger 1h/
    24h), Dernières lignes, Journaux, Coûts, Preuves, et le contrôle des
    cinq actions du contrat) ; panneau de contrôles réutilisable
    (`ui/src/components/ControlsPanel.tsx`, `ui/src/domain/
    controlsPanel.ts`) suivant effet annoncé → confirmation → exécution →
    vérification par relecture, avec attente d'approbation explicite pour
    les actions sensibles ; raccourcis F3/F5/F9/F12 sur les trois écrans ;
    redirection automatique vers l'assistant au premier lancement (accueil
    v1 sans aucune source `/v2` déclarée). Client `/v2` étendu en
    conséquence (pipelines, actions à tous les niveaux, métriques,
    journaux, coûts, confirmations, audit, SSE avec reprise
    `Last-Event-ID`) — les routes non encore servies par le control plane
    v2 (liste des pipelines, métriques, journaux, coûts, actions source/
    destination/flotte) sont codées contre le contrat et marquées
    « CONTRAT SEUL » en commentaire dans `controlPlaneV2Client.ts`.
    Reste hors périmètre : le câblage effectif des routes CONTRAT SEUL une
    fois servies côté serveur (aucun changement UI attendu, seulement leur
    activation réelle), et le lien statique « Cockpit » dans la navigation
    v1 (`AppShell.tsx`) — non ajouté pour ne prendre aucun risque de
    régression sur les tests de navigation existants, la redirection
    premier lancement couvrant le besoin produit exprimé ; à trancher par
    le product owner si un lien permanent est aussi souhaité.

- `src/quadringent_qualification/` (chantier 7) : suite de qualification de
  bout en bout générique, reprise du harnais privé de qualification IBM i
  externe sans aucun identifiant de site réel. Générateur/oracle synthétique
  déterministe (NULL, chaîne vide, accents/apostrophe, CHAR complété
  d'espaces, décimaux signés, date, horodatage) et normalisation canonique
  fixée par type SQL (`schema.py`) ; rapprochement à trois voies
  oracle/source/entrepôt qui liste chaque différence — clés manquantes/
  excédentaires, valeurs, image « avant » incohérente, continuité de journal,
  frontière de bootstrap, rejeu divergent (`reconcile.py`) ; statistiques de
  latence p50/p95 (`latency.py`) ; configuration YAML/JSON par références de
  secrets uniquement (`config.py`) ; interfaces `SourceDriver`/
  `CaptureRunner`/`StorageBackend`/`WarehouseLoader` avec fakes en mémoire
  pour les tests (`adapters.py`) ; orchestrateur de run (`orchestrator.py`) ;
  rapport JSON complet + résumé Markdown en français, coûts marqués
  explicitement absents tant que non mesurés (`report.py`) ; CLI
  `qualification run --config … --steps all|a,b,c` et `qualification report`
  avec mode `--offline-fake` pour qualifier le câblage sans système externe
  (`cli.py`). Paquet volontairement exclu du wheel d'exécution du produit
  (`pyproject.toml`). Voir `docs/qualification.md`. 117 tests, hors ligne.
- Installateur « mode par défaut » (chantier 6) : `quadringent install
  --cloud aws|gcp --target vm|cluster`, `quadringent uninstall`,
  `quadringent status` (`src/quadringent/installer/`). Six modules Terraform
  versionnés sous `deploy/terraform/{aws,gcp}/{base,vm,eks-addon|gke-addon}`
  (stockage, état des checkpoints, identité bornée, VM k3s sans ingress
  publique par défaut, IRSA/Workload Identity pour un cluster existant),
  `terraform validate`/`fmt -check` verts sans accès cloud
  (`terraform init -backend=false`). Le CLI génère les tfvars et les values
  Helm (backend de stockage aws/gcs, digests d'image pinnés via un manifeste
  de version), passe par un exécuteur de commandes injectable (tests hors
  ligne) et propose `--dry-run` (plan complet sans effet). Voir
  `docs/product/install-default.md`, y compris les limites connues de la
  chart actuelle (déclaration de site obligatoire au rendu,
  `controlPlane.serviceAccount.roleArn` propre à AWS, `launch.enabled`
  laissé à `false`, absence de composant Postgres).
- `chart/values.yaml`/`values.schema.json` : `storage.backend` (`aws` ou
  `gcs`) sélectionne `QUADRINGENT_STORAGE_BACKEND` et bascule entre
  `AS400_CHECKPOINT_TABLE` et `AS400_CHECKPOINT_BUCKET` dans le ConfigMap de
  tuning — additif, le rendu par défaut (`aws` implicite) est inchangé.
- Control plane v2 (`/v2`, chantier 3) — confirmations, identité et audit
  (tâches 7, 8, 9, 11, 12, 13) : migrations Alembic `0002_identity_conf_audit`
  et `0003_webhooks_secret_ciphertext` (tables `users`, `activation_tokens`,
  `agent_tokens`, `confirmations`, `audit_records`, `events`, `webhooks`,
  `webhook_deliveries`). Actions de pipeline sensibles (`remove`,
  `restart_initial_copy`, `replay`) protégées par un flux de confirmation
  (`409 pending_confirmation_required`, approbation humaine ou lien signé à
  usage unique ou jeton d'agent pré-autorisé, expiration, usage unique).
  Jetons d'agent (`qdt_<rd|op|ad>_...`, hash sha256+pepper, scope,
  restriction de source, rotation, révocation) authentifiant `/v2` via
  `Authorization: Bearer`. Utilisateurs/rôles admin/reader, premier admin
  activé par lien à usage unique, connexion par mot de passe (scrypt) avec
  cookie de session `HttpOnly`/`SameSite=Strict`. Audit interrogeable
  (`GET /v2/audit`, `actor_kind` humain/agent, `mcp_client`) remplaçant le
  fichier append-only v1 pour `/v2`. SSE étendu (`GET /v2/events`, types
  nommés `pipeline.state_changed`/`action.pending_confirmation`/
  `action.completed`/`alert.fired`/`alert.resolved`, reprise
  `Last-Event-ID`). Webhooks signés (HMAC-sha256, anti-rejeu par
  `(webhook_id, event_id)`, retry à backoff exponentiel borné,
  désactivation après échecs consécutifs, rejeu manuel). Voir
  `docs/api-v2.md`.
- `chart/values.schema.json` : validation Helm native (types, enums, motifs de
  digest immuable, bornes de tuning, `additionalProperties: false`) de toutes
  les clés de `chart/values.yaml`, pour le mode expert self-host. Personnalisations
  supplémentaires à défauts sûrs : `podSecurityContext`, `podAnnotations`/`podLabels`,
  `imagePullSecrets`, `extraEnv`/`extraVolumes`/`extraVolumeMounts`, `affinity`,
  `networkPolicy.enabled`, `controlPlane.serviceAccount.annotations` (IRSA/GKE
  Workload Identity). `scripts/generate_chart_values_doc.py` génère
  `docs/product/chart-values.md` depuis le schéma.
- Fondation du control plane v2 (`/v2`, chantier 3) : schéma Postgres versionné
  par migrations Alembic (`sources`/`destinations`/`tables`/`pipelines`/
  `idempotency_keys`), modèle Source v2 (création chiffrée du mot de passe
  IBM i, secret jamais renvoyé en clair), modèle Destination v2 (génération
  d'une paire de clés RSA 2048 et d'un script SQL Snowflake), machine à états
  déclarée du pipeline (`declared_state`) et enveloppe d'action générique
  (`dry_run`, en-tête `Idempotency-Key` obligatoire, réponses
  `before`/`after`/`verify`). Squelette FastAPI servant `/v2/openapi.json`
  (OpenAPI 3.1) à côté du serveur `/v1` existant, sans le modifier. Voir
  `docs/api-v2.md`. Extra optionnel `api` (FastAPI, SQLAlchemy, Alembic,
  psycopg, cryptography) ; tests unitaires sur SQLite et tests d'intégration
  Postgres 16 réels (`@pytest.mark.postgres`, Docker).
- Découverte de tables IBM i (tâche 4 du contrat control plane v2) : commande
  worker Java `discover` (`PersistentJournalWorker`, catalogue seulement —
  `QSYS2.SYSTABLES`/`SYSTABLESTAT`/`JOURNALED_OBJECTS`/`SYSKEYCST`), parsing
  et classification de disponibilité côté Python
  (`quadringent.table_discovery` : `ready`, `not_journaled`,
  `images_incomplete`, `no_key`, `journal_mismatch`) avec génération des
  commandes CL correctives (`CRTJRNRCV`, `CRTJRN`, `STRJRNPF`, `CHGJRNOBJ`).
  Routes `GET /v2/sources/{id}/tables` (filtres `search`/`library`/
  `readiness`), `POST /v2/sources/{id}/tables/refresh` (client de découverte
  injecté, `dry_run`) et `PATCH /v2/tables/{id}` (choix de clé primaire/index
  unique/RRN, RRN acquitté explicitement). Migration Alembic
  `0002_tables_discovery`. Voir `docs/api-v2.md` et
  `docs/product/install-client.md` (§3bis).
- Paquet installable et commande `quadringent-control-plane`.
- Coûts calculés depuis des crédits mesurés et un prix déclaré.
- Collecteur `quadringent-cost-collect` : volume S3 Standard, tarif public relevé,
  estimation à volume constant et allocation OpenCost au namespace ; total du
  cluster et inutilisé distincts. Les absences et preuves anciennes restent visibles.
- Audit durable des actions et attribution à l’identité du proxy lorsqu’elle existe.
- Guides, présentation statique privée et contrôle des identifiants sensibles.
- Backend de stockage GCS (`QUADRINGENT_STORAGE_BACKEND=gcs`, extra `gcs`) pour la
  capture autonome et la publication du snapshot : objets écrits une seule fois,
  checkpoint et garde de sign-on en compare-and-set sur la génération GCS. S3 et
  DynamoDB restent le backend par défaut.
- Sonde de tail bornée (`AS400_TAIL_PROBE`, activée par défaut) : le worker Java
  répond à une commande `tail` (une seule ligne, receiver ATTACHED uniquement)
  pour détecter une nouvelle entrée ou une rotation sans refaire un catalogue
  complet des receivers, ramenant la fraîcheur du journal à quelques secondes
  au lieu d'une minute.
- Sommeil oisif adaptatif du service de capture continue : l'attente entre
  deux polls repart à `AS400_MIN_POLL_SECONDS` (1 s par défaut) après toute
  activité et double à chaque poll oisif consécutif jusqu'à `AS400_POLL_SECONDS`.

### Ajouté

- Chart : composant Postgres embarqué pour l'état du control plane v2
  (`chart/templates/postgres.yaml`, `postgres.*`) — StatefulSet à une
  réplique (image officielle `postgres:16-alpine` épinglée par digest,
  résolue le 2026-09-23 via `docker pull postgres:16-alpine` puis
  `docker inspect --format='{{index .RepoDigests 0}}' postgres:16-alpine` :
  `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`),
  PVC (`postgres.storage.size`, défaut 5Gi), Service headless, Secret
  d'identifiants conservé entre upgrades (`helm.sh/resource-policy: keep` +
  `lookup`), NetworkPolicy restreignant l'accès au control plane
  (`networkPolicy.enabled`), CronJob de sauvegarde logique optionnel
  (`postgres.backup.enabled`, `scripts/quadringent_postgres_backup.py` via
  un initContainer `pg_dump` dédié). Actif par défaut
  (`postgres.enabled: true`) ; `externalDatabase.url`/`existingSecret`
  bascule vers un Postgres géré externe en mode expert (mutuellement
  exclusif). `infra-values/values-int.yaml` déclare explicitement
  `postgres.enabled: false` (site non encore migré vers v2) pour préserver
  son rendu existant.
- Chart : second conteneur `control-plane-v2` (`controlPlane.v2.enabled`,
  désactivé par défaut) dans le même Pod que le control plane v1 (conservée
  intacte) — surface FastAPI/uvicorn, probes `/v2/healthz`, secrets
  applicatifs (`SECRET_KEY` Fernet, `TOKEN_PEPPER`) générés une fois et
  conservés entre upgrades (`<release>-v2-secrets`,
  `helm.sh/resource-policy: keep` + `lookup`). Le DSN Postgres est soit
  composé au démarrage depuis le Postgres embarqué (mot de passe par Secret
  dédié, jamais en clair dans les values), soit passé directement pour un
  Postgres externe (`externalDatabase.url`/`existingSecret`) — les deux
  sources sont mutuellement exclusives avec `postgres.enabled`.
- `src/quadringent_control_plane/v2/entrypoint.py` (+
  `scripts/quadringent_control_plane_v2.py`) : lanceur uvicorn autonome,
  jamais mêlé au serveur v1 (`http.server`) — construit l'app v2, applique
  les migrations verrouillées, sert `/v2/healthz` et l'API `/v2/*`.
- `src/quadringent_control_plane/v2/db.py::run_migrations_locked` : verrou
  advisory Postgres garantissant qu'une seule réplique migre à la fois
  (rolling update, redémarrage simultané).
- `scripts/quadringent_postgres_backup.py` : sauvegarde logique (`pg_dump`)
  publiée vers le stockage objet du site (S3 ou GCS selon
  `storage.backend`), avec l'identité IRSA/Workload Identity déjà déclarée.

### Corrigé

- Runtime (`src/quadringent/site_config.py`) : `SiteConfig` porte désormais
  `storage_backend` (lu depuis `QUADRINGENT_STORAGE_BACKEND`, absente = `aws`
  pour compatibilité ascendante) ; `QUADRINGENT_AWS_ACCOUNT_ID`/
  `QUADRINGENT_AWS_REGION` ne sont exigées et validées que pour
  `storage_backend=aws`, refusées explicitement si déclarées ailleurs — même
  règle que le chart (gap (c) résiduel, suite du correctif précédent).
  `s3_snowpipe_notification.py` refuse désormais explicitement tout appel sur
  un site `storage_backend=gcs` (mécanisme AWS uniquement) au lieu de
  construire un ARN SQS vide de sens.
- Chart : `controlPlane.serviceAccount.roleArn` (IRSA AWS) n'est plus exigé
  ni validé au format ARN que pour `storage.backend=aws` ; un nouveau champ
  `controlPlane.serviceAccount.gcpServiceAccount` (Workload Identity GCP,
  annotation `iam.gke.io/gcp-service-account`) couvre `storage.backend=gcs`.
  Les deux champs sont mutuellement exclusifs (gap (b) de
  `docs/product/install-default.md`).
- Chart : `site.awsAccountId` et `aws.region` ne sont exigés et validés que
  pour `storage.backend=aws` dans `configmap-site.yaml` ; ils doivent rester
  vides sur `storage.backend=gcs` (gap (c)). L'installateur GCP ne pose plus
  de valeurs de repli fictives (`000000000000`, `eu-west-3`) pour satisfaire
  ce contrat — voir `src/quadringent/installer/plan.py`.
- La dernière entrée d’un receiver attaché est lue dès le poll suivant : une
  modification isolée n’attend plus l’écriture suivante ni une rotation (voir
  `docs/decisions/2026-09-23-queue-vivante.md`).
- Une position de départ explicite n’est plus reculée lorsqu’elle égale la
  dernière séquence du receiver ; seul `__TAIL__` calcule un recul borné.
- La capture refuse un `--max-seconds` qui ne laisse place à aucune fenêtre de
  lecture, au lieu de s'arrêter sans poll.
- Le registre canonique de la relecture externe ne garde qu’une ligne par `event_id`
  lorsqu’un même chargement contient un lot rejoué ; le statut exige désormais
  autant de lignes canoniques que d’événements distincts.
- La lecture `DISPLAY_JOURNAL`, le scan DL et l’oracle de fenêtre acceptent les noms
  système IBM i contenant `_` ou `@` (ex. `QDC_ORDERS`), comme le snapshot ; ils
  refusaient jusqu’ici toute table au nom souligné.
- Un Job terminé ne peut plus être réutilisé comme actif ; la reprise attend
  le nettoyage TTL et la preuve API de son absence.
- CA TLS fourni par le site, hors images.
- Un seul lanceur Python collecte toutes les garanties.
- Avis complets des dépendances et des polices joints au bundle web redistribué.
- Arrêt du démon de télémétrie compatible avec Python 3.12 ; tests CLI et
  d’horodatage indépendants du PATH et du fuseau du poste de test.
- Module de lecture des preuves de certification inclus dans l’image de capture ;
  la CI vérifie aussi le démarrage réel de sa commande de control plane.

### Modifié

- Moteur, API et cockpit local sous Apache-2.0. Suppression du verrou commercial
  des cinq tables, du vérificateur et de l’émetteur de clés de licence.
- Télémétrie opt-in : `tier` vaut désormais `community`, sans lecture de jeton.
- Avis de redistribution, textes et sources des dépendances Java livrés avec les images.

- Expériences séparées du paquet produit ; archives privées exclues.
- Version canonique 0.2.0 dans pyproject.toml, dérivée dans UI, API, Java et chart.
- Cockpit : jetons de fondation (thème sombre, bandes « papier listing »),
  composants StatusWord, MetricTile, BandedTable, FunctionKeyBar/useFunctionKeys
  et ActionButton, et page de référence visuelle réservée au développement
  (`#/_fondation`) pour la revue du product owner.
- UI : police d'interface par défaut (`--font-sans`) basculée de Geist vers
  IBM Plex Sans (déjà autohébergée via `@fontsource`) sur tout le cockpit, plus
  seulement sur les composants « fondation ». Dépendance `@fontsource-variable/geist`
  retirée du manifeste ; plus aucune référence à Geist dans le code ou les styles.

### Corrigé

- Cockpit v2 (accueil, connexion, table) et assistant : finition visuelle des
  écrans complets mais non stylés — nouvelle feuille `styles/cockpit.css` et
  compléments à `styles/foundation.css`/`styles/wizard.css`. `ControlsPanel`
  affiche désormais un en-tête de portée (niveau + cible), une seule action
  primaire à la fois et distingue visuellement les actions sensibles
  (retirer/relancer/rejouer) des actions courantes. Le bandeau d'attention
  (accueil/connexion) porte une puce, un texte court et un lien « Voir » vers
  la première connexion/table concernée, au lieu d'une ligne de texte brute.
  `MetricChart` rend enfin ses courbes (le trait SVG n'avait aucune couleur)
  et affiche min/max. Les onglets de l'écran table sont un vrai `tablist`
  ARIA navigable aux flèches ; le titre affiche le nom humain de la table
  (bibliothèque/table), l'id technique en second, mono. Le bandeau de
  raccourcis (`FunctionKeyBar`) s'aligne sur la largeur réelle du contenu au
  lieu de déborder de la carte, sur les états vide/en échec. `document.title`
  du cockpit connexion/table utilise désormais le nom humain, jamais l'id
  technique ; le titre de l'assistant ne porte plus le mot interne « fleet ».
  Assistant : bouton primaire désactivé lisible (fond gris quiet, plus un
  bloc bleu au texte illisible) ; choix de clé (étape Tables) en colonne,
  une option par ligne, jamais un mot coupé par ligne, table scrollable
  horizontalement dans la colonne à 640px plutôt que débordement de page.
- Cockpit v2, deuxième passe de finition visuelle : le tableau des tables
  d'une connexion affiche désormais retard, débit, lignes source/Snowflake
  et dernière arrivée (`GET /v2/pipelines` publiait déjà ces figures,
  `PipelineListRecord.to_dict` — client v2 étendu avec `PipelineListRecordV2`,
  domaine et mode démo alignés) ; une figure absente rend un tiret avec sa
  raison au survol, jamais une valeur fabriquée. Les trois panneaux de
  contrôles empilés (connexion/destination/flotte) de l'écran connexion sont
  remplacés par une barre compacte unique avec un sélecteur de portée ; les
  actions sensibles (retirer/relancer/rejouer) passent dans un menu « Plus »
  séparé, jamais mêlées aux actions courantes. Le même emplacement (juste
  sous l'en-tête) est repris sur les trois écrans (accueil/connexion/table).
  La barre de raccourcis F touchait le dernier contenu à zéro pixel une fois
  le défilement arrivé au bout : 24px de recul réservés. Graphes de
  métriques : trait aminci (1,5px), couleur sobre (`--ink-2` au lieu du bleu
  d'accent), aire discrète sous la courbe, plage verticale qui ne colle plus
  la courbe aux bords du cadre.

### Base de la préversion

- Moteur, API et cockpit local sous Apache-2.0. Suppression du verrou commercial
  des cinq tables, du vérificateur et de l’émetteur de clés de licence.
- Télémétrie opt-in : `tier` vaut désormais `community`, sans lecture de jeton.
- Avis de redistribution, textes et sources des dépendances Java livrés avec les images.

- Refonte du cockpit : Liaisons, détail, Tables, Journal, Consommation, Installation.
- Déclaration durable distincte d’une mise en service.
- Capacités distinguant lecteur arrêté, phase non pausable et runtime absent.

La qualification en direct couvre GKE DEV et l'installation du control plane
sur EKS et VM AWS DEV ; aucune qualification PROD n'est annoncée.
