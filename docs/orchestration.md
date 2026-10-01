# Orchestration Kubernetes v2 (chantier 4 — démarrage automatique)

Statut : implémentation initiale (exécuteur, protocole de bascule, tests
hors ligne). Rien de ce document n'a été validé sur un cluster ou un IBM i
réels — voir « À valider en conditions réelles » en fin de document.

Ce chantier répond au design produit (`docs/plans/2026-09-23-produit-fini-design.md`
§2) : quand une table démarre depuis `/v2`, le produit lit automatiquement la
position du journal, prend une copie initiale cohérente, bascule
exactement sur le journal (bootstrap explicite jamais reculé), puis capture
en continu — avec pause/reprise à tous les niveaux et un lecteur par
journal.

Code : `src/quadringent_control_plane/v2/executor/` (`boundary.py`,
`manifests.py`, `evidence.py`, `reconcile.py`, `kubernetes.py`) et
`src/quadringent_control_plane/k8s_deployments.py`. Tests :
`tests/test_v2_executor_*.py` (45 tests, tous hors ligne — clients
Kubernetes et sonde IBM i factices).

## 1. Protocole de bascule (« boundary protocol »)

Ordre imposé par le design (§2) : **position du journal → copie initiale
cohérente → bascule**. Concrètement :

1. **Lecture de la position** — une seule requête JDBC
   (`QSYS2.JOURNAL_RECEIVER_INFO`, via `ReadOnlyReceiverCatalog`/le worker
   Java persistant) lit dans la même fenêtre de transaction le receiver
   attaché et sa dernière séquence. C'est la position de bascule,
   enregistrée **avant** le lancement de la copie — jamais recalculée
   après coup.
2. **Copie initiale cohérente** — `ReadOnlyTableSnapshot` (Java), avec un
   identifiant de run **UUID** et un répertoire de sortie **vide**
   (`--reserve-run-id`-like : la classe refuse tout répertoire non vide).
   `restart_initial_copy` génère toujours un nouveau `run_id`, donc un
   nouveau répertoire — jamais de réutilisation.
3. **Bascule explicite** — la capture continue démarre à
   `sequence + 1` du receiver lu à l'étape 1. `plan_bootstrap`
   (`executor/boundary.py`) valide qu'aucune régression n'a eu lieu par
   rapport à un éventuel bootstrap déjà enregistré pour la même table :
   même receiver avec une séquence qui reculerait, ou un retour à un
   receiver antérieur (horodatage qui reculerait), lève
   `BoundaryRegressionError` — le pipeline bascule alors en `attention`
   (jamais une correction automatique silencieuse).

### Pourquoi ce protocole est le plus sûr réalisable avec les facilités IBM i du produit

Il n'existe pas, avec les facilités disponibles (JDBC, `QSYS2.JOURNAL_RECEIVER_INFO`,
`ReadOnlyTableSnapshot`), de moyen d'obtenir une lecture atomique
« position du journal + image de la table » sans imposer une fenêtre de
maintenance à l'utilisateur (hors périmètre du produit — design §1 :
« aucune fenêtre de maintenance à fournir »). Le choix retenu — lire la
position **avant** de lancer une copie qui peut durer — a deux propriétés :

- La bascule ne dépend jamais de la durée de la copie : une copie longue
  (grosse table) ne retarde jamais le début de la capture continue.
- Le bootstrap est un fait immuable dès l'étape 1, cohérent avec
  « un bootstrap explicite n'est jamais reculé » (design §2).

### Risque résiduel et comment il est couvert

Toute ligne modifiée entre la lecture de position (étape 1) et la fin de la
copie (étape 2) peut être capturée par la copie dans un état partiel ou
incohérent avec la modification — la copie n'est pas isolée de ces
écritures concurrentes par un verrou de table applicatif. Deux garanties,
déjà posées ailleurs dans le produit, absorbent ce risque :

- **Historique (brut durable + Snowpipe Streaming)** : chaque évènement
  journal a une clé naturelle `(receiver, sequence)`. La capture continue,
  qui commence exactement à `sequence + 1` du bootstrap, rejoue tout
  évènement survenu pendant la copie sans jamais le dupliquer (dédoublonnage
  déjà en place côté chargeur).
- **Miroir (MERGE par clé, ordonné par position)** : la ligne écrite par la
  copie initiale porte une position synthétique (le bootstrap) strictement
  antérieure à toute position réellement capturée ensuite pour la même
  table. Le MERGE applique toujours la ligne dont la position est la plus
  grande — une modification survenue pendant la copie est donc *toujours*
  rattrapée par l'évènement journal correspondant, qui écrase la valeur
  potentiellement obsolète déposée par la copie, jamais l'inverse.

Conséquence pratique : le miroir converge vers l'état correct dès que la
capture continue a rattrapé son retard, sans exiger que la source soit
immobile pendant la copie ni une synchronisation fine entre copie et
capture. **Ce que ce protocole ne couvre pas** : si le MERGE du miroir
n'appliquait pas la règle « position la plus grande gagne » (bug
d'implémentation du chargeur), une régression silencieuse redeviendrait
possible — c'est une invariance à vérifier explicitement dans les tests du
chargeur, hors périmètre direct de ce chantier (ce chantier fournit
uniquement le bootstrap et la preuve, pas le MERGE lui-même).

## 2. Objets Kubernetes

Toute la construction de manifestes est pure (`executor/manifests.py`,
aucun appel réseau) ; l'application est idempotente
(`executor/reconcile.py` compare une empreinte de `spec` — annotation
`quadringent.io/spec-sha256` — avant de créer/mettre à jour/supprimer).

| Objet | Cardinalité | Nom | Rôle |
|---|---|---|---|
| `Deployment` (lecteur) | un par `(source, journal)` — jamais par table | `qdt-reader-<hash(source,journal)>` | Capture continue de toutes les tables `copying`/`live` de ce journal. Un seul réplica (`strategy: Recreate` — jamais `RollingUpdate`, qui créerait transitoirement deux lecteurs sur le même journal). |
| `Job` (copie initiale) | un par table, par tentative | `qdt-copy-<hash(table,run_id)>` | `ReadOnlyTableSnapshot` → publication → écriture de la preuve. `run_id` UUID, jamais réutilisé. |
| `Job` (rejeu) | un par plage rejouée | `qdt-replay-<hash(pipeline,from,to)>` | Rejeu borné d'une plage de séquences journal pour une table. |

Variables d'environnement systématiques : `QUADRINGENT_STORAGE_BACKEND`,
`AS400_SOURCE_TIME_ZONE` (fuseau IANA, obligatoire — `start`/`restart_initial_copy`
échouent fermé si la source n'a pas de `detected_timezone`, ce qui impose
d'avoir relancé `POST /v2/sources/{id}/test` avec une sonde branchée avant
tout démarrage), `AS400_READER_TIMEOUT_SECONDS` et `AS400_MAX_SECONDS` (le
budget dépasse toujours le délai du lecteur d'au moins 30 s — leçon de
qualification réelle : une égalité tronque le dernier poll), le bootstrap
(`AS400_BOOTSTRAP_RECEIVER`/`AS400_BOOTSTRAP_SEQUENCE` pour la copie,
`AS400_TABLE_BOOTSTRAP_JSON` — une entrée par table — pour le lecteur).
Identifiants Snowflake et IBM i : toujours `envFrom.secretRef`, jamais de
valeur en clair (même garde-fous que `fleet_job_launcher.py`) ; le nom du
Secret est une convention déterministe (`qdt-destination-<id>`,
`qdt-source-<id>`) — le provisionnement réel de ces Secrets (à partir des
identifiants chiffrés en base) reste hors périmètre de ce chantier, à
brancher dans la chart ou par un contrôleur dédié.

### Preuve de fin de copie initiale (`copying -> live`)

Le Job de copie écrit lui-même (jamais le control plane) un petit objet
JSON à `<raw_prefix>/<table>/evidence/<run_id>.json`
(`executor/evidence.py::InitialCopyEvidence`) : `pipeline_id`, `table_id`,
`run_id`, `boundary` (receiver + séquence), `rows_copied`, `completed_at`.
Le control plane ne fait que le *lire* (`EvidenceReader`, jamais écrire) —
un objet absent est un signal normal (copie en cours), jamais une erreur ;
un objet mal formé échoue fermé (`EvidenceError`). La transition
`copying -> live` elle-même (évènement `bootstrap_completed` de la machine
à états, `v2/services/state_machine.py`) n'est **pas encore déclenchée
automatiquement** par ce chantier — un réconciliateur de fond qui lit la
preuve et appelle la transition reste à écrire (voir « Limites »).

## 3. Pause/reprise à tous les niveaux

Une table `paused` n'est simplement plus incluse dans le jeu de tables
(`AS400_FLEET_TABLES`/`AS400_TABLE_BOOTSTRAP_JSON`) du `Deployment` de son
journal au prochain appel de `_reconcile_reader_for_source` : le
`Deployment` est mis à jour (jamais recréé), les checkpoints durables
(hors Kubernetes — objet/DynamoDB, par table) ne sont jamais perdus par
cette mise en pause. Si plus aucune table d'un journal n'est
`copying`/`live`, le `Deployment` est supprimé (pas seulement mis à zéro
réplica) pour ne pas laisser un lecteur orphelin.

Point d'attention d'implémentation : `PipelinesService.apply_action`
appelle l'exécuteur **avant** de persister `declared_state` — l'exécuteur
ne peut donc pas relire l'état suivant depuis la base pendant son propre
appel. Il recalcule la même transition (déjà validée en amont par
`state_machine.transition`) à partir de l'état encore courant, et passe un
`override` explicite au calcul du jeu de tables désiré
(`KubernetesPipelineExecutor.execute`).

### Pause/reprise de source, destination et organisation

Le chantier MCP/CLI a ajouté `POST /v2/sources/{id}/actions/{pause,resume}`,
`/v2/destinations/{id}/actions/{pause,resume}` et
`/v2/actions/{pause_all,resume_all}`, qui ne posaient d'abord qu'un
marqueur d'intention (`sources.paused_at`/`destinations.paused_at`/
`organizations.paused_at`, migration `0006_source_destination_pause`) sans
jamais arrêter les flux. Complété ici : ces routes pausent/reprennent
réellement chaque pipeline `copying`/`live` de leur portée via
`PipelinesService.apply_scope_action` (même exécuteur, même mécanisme que
`/v2/pipelines/{id}/actions/pause`).

Une table pausée **individuellement** (`POST /v2/pipelines/{id}/actions/pause`)
n'est jamais relancée par la reprise d'une source, d'une destination ou de
l'organisation entière : `pipelines.paused_by_scope_action` (migration
`0008_pipeline_scope_pause_marker`) distingue les deux — vrai seulement
quand une action de *portée* a pausé ce pipeline précis, jamais posé par
une pause individuelle. `resume` de portée ne relance que les pipelines
qui portent ce marqueur, et l'efface au passage ; une table pausée
individuellement reste pausée après la reprise de sa source.

`apply_scope_action` reste best-effort par pipeline (une table dont la
transition est interdite dans son état courant, ou dont la pause était
individuelle, est listée en `skipped` — jamais bloquante pour les autres)
et fait échouer fermé toute la portée (`503 executor_unavailable`) si un
exécuteur est requis (au moins un pipeline à piloter) mais absent — jamais
un pilotage partiel silencieux. Un évènement SSE `pipeline.state_changed`
(`cause: "source.pause"`/`"destination.resume"`/`"organization.pause_all"`)
est émis pour chaque pipeline effectivement affecté ; l'audit passe par
l'enveloppe générique existante (`AuditContext` déjà posé par ces routes).

## 4. Réconciliation idempotente et redémarrage du control plane

`reconcile.py` ne compare jamais le contenu complet d'un objet observé
(le serveur Kubernetes ajoute des champs par défaut) : il compare
uniquement l'empreinte SHA-256 de la `spec` désirée, posée en annotation au
moment de l'application (`with_spec_hash`). Un objet déjà conforme ne
produit aucune action — un redémarrage du control plane qui rejoue la
réconciliation (ou un nouvel appel d'action) est donc un no-op tant que
rien n'a changé (couvert par
`test_reconcile_is_idempotent_on_control_plane_restart`).

Limite assumée : retrouver un `Deployment` de lecteur devenu orphelin (plus
aucune table live) exige de connaître son nom déterministe — obtenu en
énumérant tous les journaux jamais vus pour la source
(`_known_journals`), pas par un sélecteur d'étiquettes (le client
Kubernetes reste volontairement minimal, comme `k8s_jobs.py`). Un Job de
copie/rejeu qui a échoué (voir §5) n'est jamais réappliqué automatiquement
sous le même nom (immutabilité des Jobs) ; `restart_initial_copy` doit
générer un nouveau `run_id`.

## 5. Échecs et `attention`

Un `Job` de copie initiale en échec (`status.conditions[].type == Failed`)
doit faire basculer le pipeline en `attention` avec l'action suivante
« relancer la copie initiale » (`restart_initial_copy`) — ce branchement
(lecture périodique du statut des Jobs par un réconciliateur de fond) n'est
pas encore écrit dans ce chantier (voir « Limites »). La détection
`BoundaryRegressionError` (bootstrap qui reculerait) doit produire le même
effet : `attention`, jamais de correction automatique.

## 6. RBAC minimal requis

Le control plane a besoin, dans le namespace cible, des verbes suivants
(à traduire en `Role`/`RoleBinding` par la chart — non fournis par ce
chantier) :

| Ressource | Verbes | Pourquoi |
|---|---|---|
| `apps/deployments` | `get`, `create`, `update`, `delete` | Lecteur de capture continue |
| `batch/jobs` | `get`, `create` | Copie initiale, rejeu (jamais `update`/`delete` — immutabilité) |
| `secrets` (core) | `create`, `patch` | Provisionnement de `qdt-source-<id>`/`qdt-destination-<id>` par `SecretsProvisioner` (tâche 3) — **jamais `get`/`list`/`watch`** : le control plane écrit les Secrets qu'il vient de déchiffrer en base, il ne les relit jamais depuis Kubernetes (`k8s_secrets.py::KubernetesSecretsClient` n'expose d'ailleurs aucune opération de lecture). |

Aucun autre verbe, aucune autre ressource (pas de `pods/exec`). Les valeurs
provisionnées ne sont jamais journalisées : `SecretsProvisioner` ne
retourne que le **nom** du Secret provisionné, jamais son contenu.

## 7. Boucle de réconciliation en arrière-plan

`services/reconciler.py::ReconciliationLoop.tick()` couvre les deux
transitions automatiques laissées manuelles par la première version de ce
chantier :

- **Copie terminée -> live** : pour chaque pipeline `copying` avec un
  `active_run_id` (posé par l'exécuteur au moment de `start`/
  `restart_initial_copy`, migration `0007_reconciliation`), lit la preuve
  durable via `copying_evidence` ; si elle existe, transition
  `bootstrap_completed` et évènement SSE `pipeline.state_changed`.
- **Job en échec -> attention** : sinon, lit `copy_job_outcome` (réutilise
  `fleet_job_launcher.job_is_terminal`) ; un Job terminé en échec bascule le
  pipeline en `attention` avec `attention_reason` explicite (« relancer
  `restart_initial_copy` ») — jamais une correction automatique.

Sûreté à plusieurs réplicas : un bail applicatif portable (table `leases`,
`services/lease.py::acquire_lease`) — pas de `pg_advisory_lock` spécifique
Postgres, incompatible avec les tests SQLite de ce dépôt. Un seul réplica
détient le bail à la fois (TTL court, revalidé à chaque tour) ; un tour
manqué (réplica qui redémarre pendant qu'il détient le bail) est sans
conséquence, retenté au tour suivant. Câblage optionnel dans
`create_v2_app` (`reconciliation_executor` + `reconciliation_interval_seconds`,
tâche asyncio dans le `lifespan` FastAPI) — désactivé par défaut, aucune
tâche créée si l'un des deux est omis (sûr en test).

Depuis la migration `0010_unify_leases`, cette même table `leases` porte
aussi le bail de l'ordonnanceur de rafraîchissement d'observation
(`services/scheduler.py::ObservationRefreshScheduler`, via
`services/scheduler_lock.py::SchedulerLock` — désormais une fine enveloppe
autour de `services/lease.py::Lease`) : les deux boucles en tâche de fond
du control plane v2 partagent une seule implémentation de bail, avec un
compteur de génération (fencing) qui permet à un titulaire de détecter
qu'il a perdu le bail avant d'agir, même sans avoir observé l'expiration
lui-même. Les deux tables jumelles d'origine (`reconciler_leases` —
`0007_reconciliation`, `scheduler_locks` — `0009_scheduler_locks`)
n'existent plus.

Limite assumée : `tick()` ne couvre que la transition `copying -> live` et
la détection d'échec du Job de copie ; elle ne réconcilie pas activement
les Deployments/Jobs eux-mêmes (ça reste synchrone, au moment d'une action
HTTP via `_reconcile_reader_for_source`) — un chantier suivant pourrait
fusionner les deux boucles.

## 8. Provisionnement des Secrets Kubernetes référencés

`v2/executor/secrets_provisioner.py::SecretsProvisioner` déchiffre (via
`SecretBox`, même mécanisme que `services/sources.py`/`services/destinations.py`)
le mot de passe IBM i et la clé privée RSA Snowflake, puis les pousse vers
`qdt-source-<id>`/`qdt-destination-<id>` (noms partagés avec
`manifests.py::ibmi_secret_ref`/`destination_secret_ref` — une seule
fonction, jamais deux formats qui divergeraient) via
`k8s_secrets.py::KubernetesSecretsClient` (`POST`, repli sur `PATCH` en cas
de conflit 409 — upsert idempotent). Clés à l'intérieur des Secrets :
`ISERIES_PASSWORD` (même variable que le lecteur v1 résout déjà via
`QUADRINGENT_IBMI_PASSWORD_SECRET`/`_KEY`, `site_config.py`),
`SNOWFLAKE_ACCOUNT` et `SNOWFLAKE_PRIVATE_KEY_PEM` (nouvelle convention v2).

**Jamais journalisé** : `SecretsProvisioner` ne retourne que le nom du
Secret provisionné, jamais son contenu ; `KubernetesSecretsClient` encode
les valeurs en base64 côté client (`data`, pas `stringData`) et ne journalise
rien lui-même. `KubernetesPipelineExecutor` accepte un
`secrets_provisioner` optionnel : injecté, il maintient les Secrets à jour
(upsert, potentiellement plusieurs fois par `start` — avant le Job de
copie, puis avant le Deployment de lecteur qui référence le même Secret,
sans conséquence puisque l'opération est idempotente) avant chaque
Job/Deployment qui les référence ; sans lui (valeur par défaut), les
Secrets doivent déjà exister par un autre moyen (chart, admin) — jamais un
échec bloquant pour un déploiement qui gère ses Secrets autrement.

## 9. Limites de ce chantier (à faire dans un chantier suivant)

- `ReconciliationLoop` ne réconcilie que `declared_state` (copying->live,
  copying->attention) ; les objets Kubernetes eux-mêmes (Deployments/Jobs)
  ne sont réconciliés que de façon synchrone, au moment d'une action HTTP
  — pas encore par la boucle de fond.
- `KubernetesPipelineExecutor._reconcile_reader_for_source` relit la
  position du journal (sonde IBM i) pour **chaque** table à chaque appel,
  y compris pause/reprise — à borner (cache court, ou ne lire que pour les
  tables réellement nouvelles) avant un déploiement à grande échelle : le
  coût réel d'une requête `QSYS2.JOURNAL_RECEIVER_INFO` répétée n'est pas
  mesuré dans ce chantier.
- `SecretsProvisioner` ne supprime jamais un Secret devenu inutile (source/
  destination retirée) — pas de nettoyage, à ajouter si la rétention de
  secrets orphelins devient un problème réel.

## 8. Sonde IBM i réelle (`POST /v2/sources/{id}/test`)

`SourcesService.test` accepte désormais une sonde optionnelle
(`SourceProbeProtocol`, `v2/services/source_probe.py`) : réseau, TLS
(confiance explicite avec empreinte pour une CA privée), authentification,
version IBM i, fuseau depuis `QTIMZON`. Sans sonde injectée (déploiement
qui n'a pas encore câblé l'outil réel), le comportement historique est
conservé (`reachable: "unknown"`, seul le déchiffrement du secret est
vérifié) — jamais une erreur bloquante.

Table `QTIMZON` → règle de capture (`QTIMZON_TO_CAPTURE_ZONE`) : couvre les valeurs les plus
courantes (Europe/Amérique du Nord/Japon/Australie/UTC). `QP0100CET` utilise
une règle IBM dédiée : son heure d'été se termine le dernier dimanche de
septembre, contrairement à `Europe/Paris`. Une valeur absente
de la table n'est **jamais devinée** : `detected_timezone: null`,
`timezone_ambiguous: true` — à charge pour un humain de confirmer via
l'assistant (design §2.1). Le worker de diagnostic dédié mesure le réseau,
TLS, l'authentification, la version IBM i et `QTIMZON`. Le worker de capture
revérifie le décalage horaire réel avant de lire le journal ; une divergence
bloque la connexion.

## 9. Découverte de tables réelle (`refresh`)

`PersistentJavaWorkerTableDiscoveryClient`
(`v2/services/table_discovery_client.py`) adapte
`quadringent.java_worker.PersistentJavaWorker.discover()` (texte ligne à
ligne) au contrat `TableDiscoveryClientProtocol` attendu par
`TablesService.refresh`, en réutilisant
`quadringent.table_discovery.parse_discover_output` — une seule vérité de
parsing entre ce chemin et le CLI/worker historique. Le câblage du worker
réel (gestion du processus, cf. `PersistentJavaWorker`) dans
`app.state.table_discovery_client` au démarrage du control plane reste à
faire dans le module de démarrage du serveur (hors périmètre de ce
chantier, qui livre l'adaptateur testé).

## 10. Câblage production (chantier « prod-wiring », 24 septembre 2026)

Constat de départ (installation réelle GKE, 24 septembre 2026) : `v2/
entrypoint.py::build_app` appelait `create_v2_app(...)` sans aucun
adaptateur réel — `/v2/sources/{id}/test` répondait toujours
`reachable: "unknown"`, la découverte n'interrogeait rien, démarrer un
pipeline ne créait aucune charge Kubernetes. Ce chantier branche la sonde
de source et la découverte de tables ; l'exécuteur de pipelines
(`KubernetesPipelineExecutor`) reste **non branché** (voir « Reste non
câblé » ci-dessous).

### Sonde de source et découverte de tables — Jobs Kubernetes éphémères

Le Pod control plane v2 n'a pas Java/JTOpen : ces deux opérations
(mesures en dizaines de secondes) tournent désormais dans un **Job
Kubernetes éphémère** avec l'image de capture, orchestré par
`v2/executor/diagnostic_jobs.py` :

1. `KubernetesJobSourceProbe`/`KubernetesJobTableDiscoveryClient`
   provisionnent un **Secret éphémère** (mot de passe IBM i, jamais en
   argument de commande) via `KubernetesSecretsClient.upsert_secret`.
2. Un Job est créé (`executor/manifests.py::build_source_probe_job`/
   `build_table_discovery_job`) : image de capture, `command` explicite
   vers `scripts/quadringent_source_probe_job.py`/
   `quadringent_table_discovery_job.py`, `securityContext` non-root (UID
   10001, celui de l'image), ressources bornées, `backoffLimit: 0`,
   `activeDeadlineSeconds` borné (60 s sonde / 120 s découverte),
   `ttlSecondsAfterFinished` en filet de sécurité.
3. Le control plane attend la fin du Job (borné par le même délai), puis
   **relit le résultat depuis les journaux du pod du Job** (une ligne JSON
   préfixée, `KubernetesPodsClient.read_pod_log`) — mécanisme retenu plutôt
   qu'un second Secret/ConfigMap écrit par le Job lui-même : cela aurait
   exigé de donner à une charge qui parle à un système externe non
   fiabilisé un droit d'écriture sur l'API Kubernetes, alors que la
   lecture de journaux n'ajoute qu'un droit `get` de plus
   (`pods/log`), déjà le mécanisme utilisé pour un besoin symétrique par
   `services/logs_kubernetes.py`.
4. Le Job et le Secret éphémère sont **systématiquement supprimés**
   (`finally`), y compris en cas d'échec de création du Job ou de
   dépassement de délai (`DiagnosticJobError("timeout", ...)`).

Câblage dans `entrypoint.py::build_diagnostic_adapters` : seulement si
`KUBERNETES_SERVICE_HOST` est présent (Pod réel) **et**
`QUADRINGENT_V2_CAPTURE_IMAGE` est déclarée (posée par la chart, valeur
`repository@digest` de l'image de capture) — sinon `(None, None)`, le
comportement hors-cluster/test reste strictement identique à avant ce
chantier.

Limite documentée : `TableDiscoveryClientProtocol.discover()` ne reçoit
aucun `source_id` (contrat hérité — une installation = une source IBM i).
`KubernetesJobTableDiscoveryClient` résout donc *la* source déclarée à
chaque appel et refuse explicitement (`TableDiscoveryUnavailableError`) si
zéro ou plusieurs sources existent.

Authentification + version IBM i + `QTIMZON` : `quadringent_source_probe_job.py`
ouvre un `quadringent.java_worker.PersistentJavaWorker` (**JTOpen**, même
pilote que le reste du produit — jamais ODBC/pyodbc, propriétaire et
absent de l'image de capture), puis lance la commande `probe`, ajoutée au
protocole ligne de `PersistentJournalWorker` (`SourceInfo.java`,
`QSYS2.SYSTEM_STATUS_INFO`/`QSYS2.SYSTEM_VALUE_INFO`) sur le même modèle
que `discover`. L'authentification est prouvée par l'ouverture de la
connexion JDBC elle-même (`connect_error` sinon, classifié comme pour le
lecteur continu). La mesure réseau/TLS (empreinte du certificat) reste une
étape Python pure indépendante, contre le service `as-signon` — dont le
port (8476/9476 TLS) est la convention documentée IBM, non revérifiée sur
un IBM i réel — à confirmer avant mise en production (cf. « À valider en
conditions réelles »).

#### Worker Java de diagnostic (`DiagnosticWorker`, 24 septembre 2026)

Preuve contre un IBM i de qualification (24 septembre 2026) : les deux Jobs de
diagnostic ci-dessus démarraient en réalité
`io.quadringent.as400.PersistentJournalWorker` — le worker de **capture** —
avec une bibliothèque/table fictives (`QSYS2/PROBE`, `QSYS2/DISCOVER`) et
`AS400_SOURCE_TIME_ZONE=UTC`. `JournalSession.connect` y vérifie le fuseau
déclaré contre le décalage UTC réel de la source
(`JournalTimestamps.verifySourceClock`) : avec `UTC` contre une source en
`Europe/Berlin`, l'échec était systématique, classé `CONNECTION_FAILED` par
`FleetCatalogProbe.classifyConnectFailure` — une erreur de *configuration*
présentée à tort comme une source indisponible.

`io.quadringent.as400.DiagnosticWorker` corrige cela : il réutilise la même
tuyauterie de connexion que `JournalSession` (`TlsTrust`,
`JournalSession.newAs400`, `configureServicePorts`, `openJdbcConnection`,
factorisées pour être partagées sans duplication) mais n'exige ni fuseau, ni
bibliothèque/table capturée, ni journal — `verifySourceClock` et
`verifyCapturedJournal` n'y sont jamais appelés. Il sert le même protocole
ligne que `PersistentJournalWorker` pour `probe` et `discover`. Les deux
scripts de Job (`quadringent_source_probe_job.py`,
`quadringent_table_discovery_job.py`) le démarrent par défaut
(`AS400_JAVA_WORKER_CLASS`, remplaçable) et ne déclarent plus de
bibliothèque/table fictive : `quadringent.java_worker.PersistentJavaWorker`
accepte désormais `schema`/`table` optionnels. Le contournement
`ensure_diagnostic_time_zone` (fuseau neutre forcé) a été retiré des deux
scripts — devenu inutile.

Une vraie discordance d'horloge (fuseau mal déclaré sur une source qui
*est* configurée pour la capture) est désormais classée
`SOURCE_CLOCK_MISMATCH` (`SourceClockMismatchException`, avant le repli
générique `CONNECTION_FAILED`) et traitée côté Python comme une erreur de
configuration non rejouable (`ConnectFailureClass.CONFIGURATION`,
`SourceConfigurationBlockedError`) : la garde de connexion bloque jusqu'à
correction et reset opérateur, le lecteur de capture ne la retente jamais
en boucle et ne la présente jamais comme une simple coupure (`STOPPED_
CONFIG_BLOCKED`). Garde-fou associé : les manifests des charges de capture
(lecteur, copie initiale, rejeu — `v2/executor/manifests.py`) ne
sélectionnent jamais `DiagnosticWorker`.

### Exécuteur de pipelines in-cluster (chantier « pipeline-exec », 24 septembre 2026)

`KubernetesPipelineExecutor` est désormais branché dans
`entrypoint.py::build_pipeline_executor`, appelé depuis `build_app` en plus
de `build_diagnostic_adapters` (deux fonctions séparées, un seul point
d'appel chacune — voir la docstring de `build_pipeline_executor`). Démarrer,
mettre en pause ou reprendre un pipeline depuis le wizard ou le MCP crée et
pilote désormais des charges Kubernetes réelles (Deployment de lecteur,
Job de copie initiale/rejeu).

#### Lecteur de frontière — in-process, jamais un Job

Le rapport du chantier précédent (ci-dessus) supposait qu'aucun adaptateur
réel n'était possible sans Job Kubernetes dédié. Ce n'est pas le cas :
l'image du control plane (`docker/control-plane.Dockerfile`) embarque déjà
le JRE, `probe.jar` et JTOpen (`AS400_JAVA`/`AS400_JAVA_CLASSPATH`) — le
conteneur v2 (`control-plane-v2`, même image, voir `chart/templates/
control-plane.yaml`) les a donc aussi. `v2/executor/boundary_reader.py::
JavaBoundaryReader` lit la position du journal **dans le Pod control
plane**, via `quadringent.java_catalog.JavaReceiverCatalog` — le lecteur
in-process *déjà existant* pour ce besoin exact (`ReadOnlyReceiverCatalog`,
un processus JVM court et jetable par appel, `subprocess.run`, pas de
session JDBC tenue ouverte comme `PersistentJavaWorker`).

Décision **in-process plutôt que Job**, justifiée dans le docstring du
module :

- `read_boundary` est appelé à **haute fréquence** (à chaque tour de
  réconciliation du lecteur, pour chaque table `copying`/`live` d'une
  source), jamais une opération ponctuelle déclenchée par un opérateur
  (contrairement à la sonde/découverte, §10 ci-dessus) — un Job par lecture
  coûterait un cycle de scheduling/pull/JVM à chaque tour pour une requête
  JDBC de quelques centaines de millisecondes ;
- le control plane décrypte déjà des secrets de connexion IBM i en mémoire
  pour les Jobs de diagnostic (`KubernetesJobTableDiscoveryClient.
  _resolve_source`) — ce n'est donc pas une frontière de confiance
  nouvelle ;
- `PersistentJavaWorker` (session JDBC tenue ouverte, utilisé par la
  capture continue elle-même) a été écarté : son protocole `catalog`/`tail`
  ne prend pas la bibliothèque/le nom de journal en paramètre de commande
  — ils viennent de l'environnement du processus, posé une fois pour un
  seul journal par pod de capture. Le control plane doit au contraire lire
  la frontière de plusieurs sources/journaux dans le même processus ;
  maintenir un worker persistant (connexion JDBC ouverte en permanence)
  par journal ajouterait un état à gérer sans bénéfice.

`quadringent.java_catalog.JavaReceiverCatalog` a été étendu d'un paramètre
`password` optionnel (rétrocompatible, `None` par défaut) : les appelants
historiques (un seul lecteur par pod) laissent `ISERIES_PASSWORD` déjà posé
dans l'environnement du processus par le Secret monté ; le control plane
v2, qui gère plusieurs sources dans le même processus, n'a pas cette
garantie et fournit le mot de passe explicitement — posé uniquement dans
l'environnement du sous-processus JVM éphémère, jamais journalisé.

Un receveur `ATTACHED` fraîchement créé sans entrée encore écrite
(`last_sequence` absent) bascule à `first_sequence - 1` (la capture
continue démarre alors à `first_sequence`) — jamais une valeur inventée.

#### Câblage de l'exécuteur

`build_pipeline_executor` reprend la même garde que `build_diagnostic_
adapters` (`KUBERNETES_SERVICE_HOST` présent **et** `QUADRINGENT_V2_
CAPTURE_IMAGE` déclarée), plus un ServiceAccount à identité cloud déclaré
(`QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT`) et un bucket/préfixe brut
(`QUADRINGENT_RAW_BUCKET`/`QUADRINGENT_RAW_PREFIX_ROOT`, déjà publiés par
le ConfigMap `-site`) — sans l'un de ces éléments, `(None, None)`, jamais
un défaut de production inventé ; la sonde/découverte restent branchées
indépendamment. La boucle de réconciliation (§7) démarre dans le lifespan
de l'app dès que l'exécuteur est branché (`reconciliation_executor`/
`reconciliation_interval_seconds`, déjà supportés par `create_v2_app`
depuis la tâche 2 — jamais modifiés ici).

**ServiceAccount des charges de capture — jamais celui du control plane.**
Les Deployments/Jobs créés par l'exécuteur (lecteur, copie initiale, rejeu,
futur chargeur) tournent avec `serviceAccount.name` de la chart (« Identité
déclarée du site : S3/GCS/DynamoDB »), jamais `controlPlane.serviceAccount.
name` : c'est la première identité qui porte les droits d'écriture sur le
bucket brut, le control plane n'en a pas besoin pour lui-même (il ne fait
que piloter l'API Kubernetes et lire la preuve de copie, §1). Le control
plane connaît ce nom via `QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT`, posé par
la chart depuis `.Values.serviceAccount.name`. `ExecutorConfig.service_
account_name` est obligatoire (`ManifestError` sinon) ; tous les manifestes
(`build_reader_deployment`/`build_initial_copy_job`/`build_replay_job`/
`build_loader_deployment`) posent `serviceAccountName` et un
`securityContext` non-root (UID 10001, la même convention que l'image de
capture, `docker/Dockerfile` : `USER 10001`) au niveau du pod et du
conteneur.

**RBAC** (`chart/templates/control-plane.yaml`, Role/RoleBinding
`-control-plane-v2-executor`, séparé du Role de diagnostic) : au strict
nécessaire aux verbes réellement appelés par `executor/kubernetes.py` et
`executor/secrets_provisioner.py` —

| Ressource | Verbes | Pourquoi |
|---|---|---|
| `apps/deployments` | `create`, `get`, `update`, `delete` | Lecteur de journal/chargeur : création, lecture avant comparaison, remplacement complet (`PUT`, jamais un patch partiel), suppression quand plus aucune table n'est live/copying. |
| `batch/jobs` | `create`, `get`, `delete` | Copie initiale et rejeu : un Job est immuable une fois créé (jamais de mise à jour) ; `delete` accordé pour l'alignement avec les autres Roles Jobs de la chart, jamais appelé par l'exécuteur lui-même (nettoyage laissé à `ttlSecondsAfterFinished`). |
| `secrets` | `create`, `get`, `patch` | Secrets *durables* (identifiants IBM i/Snowflake, `SecretsProvisioner.provision_*_secret`) : `upsert_secret` crée (`POST`) ou, sur conflit, remplace par fusion (`PATCH`) — jamais `delete` (contrairement aux Secrets éphémères des Jobs de diagnostic, nettoyés systématiquement). |

Le chargeur de destination (`LoaderDesiredSpec`/`build_loader_deployment`,
historique Snowpipe Streaming + MERGE miroir) reste **hors périmètre** de
ce câblage : `ExecutorConfig.loader_image` n'est pas déclaré depuis la
chart (aucune valeur dédiée n'existe encore), le chemin COPY/MERGE existant
continue d'être piloté hors de cet exécuteur — un chantier séparé pour
brancher le chargeur reste à faire (voir « À valider en conditions
réelles » ci-dessous, qui reprend ce point).

## 11. Disposition de stockage brut (chantier « storage-layout », 24 septembre 2026)

### Constat

Le 24 septembre 2026, premier pipeline réel sur GKE (`QDC_ORDERS`) : la copie
initiale publie 110 lignes, le lecteur capture une insertion, le chargeur
Snowflake tourne et crée ses tables `..._HISTORY`/`..._MIRROR` — mais elles
restent **vides**. Cause : les trois composants n'utilisaient pas la même
disposition de chemins sous `AS400_RAW_PREFIX` :

- le lecteur, en mode une seule table, écrivait ses lots **à la racine** du
  préfixe (aucun segment par table, et sans reçus de fenêtre — voir plus
  bas) ;
- le chargeur cherchait ses lots sous `<racine>/<SCHÉMA>/<TABLE>`
  (convention jamais produite par le lecteur v2) ;
- la copie initiale publiait son instantané sous `<racine>/snapshot/<table>/...`
  (segment `snapshot` *avant* la table) et le chargeur ne le chargeait de
  toute façon jamais.

### Disposition retenue

Une source unique de vérité, `src/quadringent/storage_layout.py`, la table
en premier segment (déjà la convention documentée — mais pas appliquée
partout — par `quadringent.fleet_capture.table_object_prefix`) :

```
<AS400_RAW_PREFIX>/<table minuscule>/journal/...     # lots + reçus du lecteur
<AS400_RAW_PREFIX>/<table minuscule>/snapshot/...    # instantané de copie initiale
<AS400_RAW_PREFIX>/<table_id>/evidence/<run_id>.json # preuve de copie (control plane,
                                                      # evidence.py::evidence_key — convention
                                                      # déjà en place, par table_id, non modifiée)
```

`journal`/`snapshot` sont routés par **nom** de table (ce que le lecteur
connaît sans consulter la base) ; `evidence` reste routée par `table_id`
(clé stable du control plane, déjà testée). Utilisée par :

- `quadringent.fleet_capture.table_object_prefix` (mode flotte *et*
  désormais mode une seule table) ;
- `scripts/quadringent_destination_loader.py::raw_prefix_for_table` (le
  chargeur lit exactement où le lecteur écrit) ;
- `scripts/as400_snapshot_publish.py::snapshot_object_key` (copie
  initiale) ;
- `v2/executor/manifests.py` (construction des trois manifestes).

### Mode une seule table : reçus sans mode flotte

`quadringent.fleet_capture.fleet_mode_from_environment` refuse toujours une
flotte à une seule table (`test_fleet_mode_refuses_a_single_table_or_an_
absolute_root`, contrat verrouillé) : le mode une seule table ne passe donc
jamais par le routage de flotte. Plutôt que de forcer ce mode, une nouvelle
variable d'environnement, `AS400_RECEIPTED_SCANS`, posée par
`build_reader_deployment` pour **tous** les Deployments de lecteur (seule
table ou flotte), active les reçus de fenêtre
(`ContinuousCaptureService.receipted_scans`) indépendamment du mode flotte —
c'est ce mécanisme, pas le mode flotte lui-même, que le chargeur exploite
(`ObjectStore.list_receipt_keys`). Solution retenue plutôt que le repli
`discover_new_batches_from_manifest_keys` (listage manuel côté appelant) :
elle réutilise un mécanisme déjà écrit et testé plutôt que d'exiger un
listage général par backend, que ce dépôt évite délibérément sur
`ObjectStore`.

### Le chargeur charge l'instantané avant les événements

`quadringent_destination_loader.py::load_snapshot_once` (appelé par
`load_table_once` avant `discover_new_batches`) :

1. si la table ne porte pas de clé de preuve (`LoaderTable.evidence_key`,
   posée par `kubernetes.py::_desired_loader_manifest` dès que
   `pipelines.active_run_id` est connu — jamais effacée à la promotion
   `copying -> live`), ne fait rien (comportement identique à avant cette
   disposition) ;
2. si le checkpoint du chargeur porte déjà une position, ne fait rien —
   c'est la garde d'idempotence : un redémarrage ne recopie jamais
   l'instantané ;
3. sinon, lit la preuve (`EvidenceReader`) ; absente (copie pas encore
   prouvée) : ne fait rien, retente au tour suivant ; présente : charge
   chaque lot d'instantané qu'elle référence (`InitialCopyEvidence.
   snapshot_batches`, les clés exactes publiées par `as400_snapshot_
   publish.publish_snapshot_batches` — sans capacité de listage général sur
   `ObjectStore`, la preuve porte donc les clés, pas un répertoire à
   énumérer) dans l'historique (`HistoryStreamingLoader.load_batch`, chaque
   ligne d'instantané est déjà une `ChangeEvent` d'opération `c` à position
   unique — voir `ReadOnlyTableSnapshot.java`) et matérialise le miroir
   (`MirrorMergePlan.execute`), puis pose le checkpoint du chargeur à la
   frontière de bascule (`boundary.receiver_name`/`boundary.last_sequence`)
   — les événements de journal à cette position ou avant, sur le même
   receiver, sont ainsi ignorés par `discover_new_batches` (déjà couverts
   par l'instantané) ; ceux d'un autre receiver (rotation après la bascule)
   restent chargés normalement.

**Limite connue** : si des lots de journal ont déjà été chargés (et donc le
checkpoint déjà avancé) avant que la preuve ne devienne lisible — schéma
possible si le chargeur tournait déjà et récupère juste sa preuve plus tard
— `load_snapshot_once` ne rejoue jamais l'instantané (le checkpoint n'est
plus `None`). Non rencontré dans le déroulé nominal (la preuve précède
toujours le bootstrap du lecteur), mais non couvert par une garde
supplémentaire dans ce chantier.

### Test de bout en bout en mémoire

`tests/test_pipeline_end_to_end_in_memory.py` exécute réellement (pas de
double simulée) : la publication de la copie initiale
(`as400_snapshot_publish.publish_snapshot_batches`) avec un faux instantané
Java de quelques lignes ; le cœur du lecteur (`RawFirstCaptureCoordinator.
capture_receipted_window_result`) sur un faux journal (insertion, mise à
jour, suppression), avec la disposition réelle rendue par
`build_reader_deployment` ; le cœur du chargeur (`run_once`) vers un faux
puits Snowflake (`FakeStreamingClient` pour l'historique, un curseur
factice qui matérialise le MERGE miroir en mémoire avec la même règle de
dédoublonnage que `MirrorMergePlan.merge_sql`), avec l'environnement rendu
par `build_loader_deployment`. Vérifie que l'historique porte l'instantané
et les événements sans doublon, que le miroir reflète l'état attendu
(insertion visible, mise à jour appliquée, suppression effective), et
qu'une relance du chargeur ne change rien. Le magasin objet est un faux
client GCS en mémoire (`tests/test_gcs_backend.py::FakeGcsClient`) derrière
les adaptateurs réels (`GcsObjectStore`/`GcsCheckpointStore`).

### Mode flotte : les reçus restent à la racine du journal

Constat en réel sur GKE, après la correction ci-dessus : deux tables sur un
même journal (`AS400_FLEET_TABLES`, mode flotte) publient deux choses
différentes, jamais au même endroit (voir
`quadringent.fleet_capture.FleetWindowCoordinator`) :

- le **lot combiné** de la fenêtre (toutes les tables de la flotte, octets
  d'origine) et le **reçu de fenêtre**, tous deux à la racine du préfixe du
  journal — une seule lecture de journal produit un seul reçu, commun à
  toute la flotte (`RawFirstCaptureCoordinator` interne au coordinateur) ;
- le **lot routé par table** (`FleetTableRouter`), publié sous
  `<racine>/<table>/journal/` — sans reçu propre : la couverture d'une table
  est un sous-produit de la fenêtre couverte à la racine, jamais un
  évènement séparé.

Le lot combiné à la racine n'est donc **pas un doublon fautif** — c'est la
preuve de fenêtre, commune à la flotte, et rien dans la qualification v1 n'en
dépend (le lecteur v1 ne route jamais par table). Ce qui manquait : le
chargeur ne cherchait les reçus que sous le préfixe de la table
(`discover_new_batches`), jamais à la racine, donc ne voyait jamais rien
pour une table en flotte, même si son lot routé était bien publié.

Deux options existaient : écrire des reçus par table (ré-ouvrir
`FleetTableRouter`, dupliquer la preuve de fenêtre par table), ou faire lire
au chargeur les reçus là où le lecteur les écrit déjà. La seconde est
retenue (`quadringent_destination_loader.discover_new_fleet_batches`) : elle
ne change rien à ce que le lecteur écrit (aucun risque de régression sur la
qualification v1 ou le format des reçus déjà en place), respecte l'ordre par
position (mêmes règles de tri/filtrage que `discover_new_batches`) et
l'idempotence (même checkpoint du chargeur, mêmes garanties `put_once`) —
elle réutilise `fleet_capture.split_window_batch`, le découpage déterministe
déjà écrit et testé pour retrouver les octets d'une table dans le lot
combiné, sans exiger de capacité de listage général sur `ObjectStore`.
`load_table_once` appelle désormais `discover_new_batches` (préfixe de
table — mode une seule table) et `discover_new_fleet_batches` (racine —
mode flotte) sans condition, et fusionne leurs résultats triés par
position : une table n'est jamais couverte par les deux à la fois, l'appel
sans objet ne renvoie simplement rien.

`tests/test_pipeline_end_to_end_in_memory.py::FleetPipelineEndToEndTests`
couvre ce cas : deux tables sur un même journal, insertion/mise à
jour/suppression sur l'une, un évènement sur l'autre, chargées sans mélange
(canaux Snowpipe Streaming distincts, MERGE miroir par table identifié par
son nom de table miroir) et relance idempotente. Échoue sans
`discover_new_fleet_batches` (0 événement chargé au lieu de 3/1).

## À valider en conditions réelles (avant toute mise en production)

- Le protocole de bascule (§1) sur un IBM i réel avec des écritures
  concurrentes pendant une copie longue : vérifier que le MERGE du miroir
  respecte bien « position la plus grande gagne » pour une même clé.
  Ce chantier bordé n'a pu tester ce que le chargeur applique réellement.
- Le coût réel d'une lecture répétée de `QSYS2.JOURNAL_RECEIVER_INFO` à
  chaque réconciliation (§7, dernier point) sur un IBM i partagé de
  qualification — désormais servie in-process (§10,
  `JavaBoundaryReader`/`JavaReceiverCatalog`, un processus JVM court par
  appel) : le coût réel d'un `subprocess.run` JVM répété à chaque tour de
  réconciliation (au lieu d'un Job) reste à mesurer sur un IBM i réel.
- Le comportement de `strategy: Recreate` du `Deployment` de lecteur en
  conditions réelles de rollout (durée de la fenêtre sans lecteur actif,
  impact sur le retard affiché au cockpit).
- Le RBAC de l'exécuteur de pipeline (§10, Role
  `-control-plane-v2-executor`) rendu par la chart, mais jamais appliqué à
  un vrai cluster ni exercé contre l'API Kubernetes réelle (`kubectl auth
  can-i` avec le ServiceAccount du control plane) — seul `helm template`
  est vérifié ici.
- La détection et la remontée en `attention` d'un `Job` de copie initiale
  en échec (non câblée dans ce chantier, cf. §5/§7).
- Le comportement de `ReadOnlyTableSnapshot` sur un répertoire de sortie
  effectivement vide en environnement Kubernetes (volume éphémère par
  `run_id`) — la garantie « répertoire vide » suppose un volume neuf par
  Job, à vérifier dans le modèle de Job réel de la chart.
- La sonde IBM i réelle de `POST /v2/sources/{id}/test` (§8) — non écrite
  dans ce chantier, seul le contrat et la traduction `QTIMZON` sont testés
  hors ligne.
- Le chargeur de destination (§10, dernier paragraphe) : `ExecutorConfig.
  loader_image` n'est câblé depuis aucune valeur de chart — un chantier
  séparé doit déclarer l'image, brancher le RBAC correspondant (déjà
  couvert par le Role `-control-plane-v2-executor`, qui n'est pas
  spécifique au lecteur) et vérifier le chemin Snowpipe Streaming + MERGE
  miroir de bout en bout.
