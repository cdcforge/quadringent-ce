# Qualification continue (chantier 7)

Ce document décrit le paquet `src/quadringent_qualification/`, qui reprend en
générique — sans aucun identifiant de site réel — le harnais de
qualification de bout en bout mené sur une source IBM i externe le
23 septembre 2026. Il correspond au § 6 « Qualification continue » de
`docs/plans/2026-09-23-produit-fini-design.md`.

## Verdict et couverture

`PASS` signifie seulement que les étapes **sélectionnées** ont réussi. Un diagnostic
partiel conserve ce verdict et son code de sortie ; il ne qualifie pas le produit
complet. `--steps all` exécute les étapes présentes dans `config.steps`, sans ajouter
les étapes omises de la configuration.

Le JSON produit ajoute `coverage` : `selected_steps_status`, `execution_mode`,
`required_steps`, `missing_required_steps`, `complete_product_status`. Les étapes
requises sont seed, snapshot, capture, reconcile, freshness, changes1, changes2, rotate
et changes3 ; absente ou non réussie signifie non couverte. Le verdict complet est
`NOT_VALIDATED` : panne injectée, observabilité native et qualifications GKE/EKS/VM
restent non validées. Même toutes les étapes réussies en mode `real` ne remplacent
pas ces preuves. `offline_fake` désigne une simulation. Un ancien JSON sans mode
ou sans mesure miroir reste `unknown` pour ces preuves ; son ancien `status` ne
permet jamais de déduire une qualification complète.

La mesure miroir utilise `freshness.details.mirror_measurement` :
`target=snowflake_mirror`, `metric=write_to_mirror_observed_upper_bound`,
`scope=sql_loader_bounded_docker_capture`, `steady_state_streaming=false`,
`count=3`, `p95_seconds`, `max_seconds`, `slo_seconds` (10 secondes par défaut),
`accepted`, `status` et `probes` (marker, observed_upper_bound_seconds, poll_count).
Le rendu accepte PASS seulement avec trois mesures finies, un seuil positif et
un maximum inférieur ou égal au seuil. Les trois probes doivent avoir des marqueurs
non vides distincts, des durées finies strictement positives et un `poll_count` entier
entre 1 et 40. Le maximum et le p95 (nearest-rank pour trois probes) doivent
égaler le maximum observé ; scope et absence de streaming permanent doivent
correspondre au contrat. Un verdict offline porte explicitement « simulation ». La latence brute `freshness_raw` et
`freshness.details.raw_latency` n'établit jamais la visibilité du miroir.
Cette mesure bornée ne qualifie pas le streaming permanent ni une plateforme.

## Scénario prévu pour un run nightly

Un run nightly est un essai automatique périodique sur une table synthétique
dédiée. Il sert à détecter une régression du produit ; il n'est pas le flux
CDC permanent d'un utilisateur.

Le harnais définit, pour une future source IBM i de qualification dédiée, le scénario
suivant (voir `src/quadringent_qualification/generator.py`) :

1. **`seed`** — insertion de 100 lignes synthétiques déterministes, couvrant
   volontairement NULL, chaîne vide, accents/apostrophe, CHAR complété
   d'espaces, décimaux signés, date et horodatage.
2. **`snapshot`** — copie initiale via le lecteur produit ; la source doit
   rester immobile pendant la frontière snapshot/journal.
3. **`changes1`** — 10 insertions, 10 modifications, 5 suppressions.
4. **Premier `capture`** — capture depuis la frontière sauvegardée avant
   `snapshot` : bibliothèque et nom du receiver, dernière séquence lue et
   instant de lecture. Le lecteur démarre à la séquence suivante, même si des
   écritures ont avancé le journal entre-temps.
5. **`changes2`** — insertions et modifications pendant l'arrêt du lecteur ;
   le deuxième `capture` doit reprendre au checkpoint durable s'il existe,
   avec la frontière snapshot comme secours si aucun n'a été écrit.
6. **`rotate`, `changes3`, troisième `capture`** — franchissement de receiver,
   puis reprise et contrôle de continuité. Le rapprochement utilise le couple
   receiver/séquence et l'ordre des receivers fourni par la source ; un oracle
   ancien qui ne fournit que des séquences est refusé. Le pilote Java réel
   fournit cet oracle ; son scénario complet reste à éprouver en runtime.
7. **`reconcile`** — rapprochement à trois voies : oracle synthétique, source
   relue directement, état matérialisé depuis l'historique de l'entrepôt cible.
   Le miroir Snowflake est également relu et comparé à l'oracle et à l'historique
   matérialisé ; une clé absente, excédentaire, dupliquée ou une valeur différente
   fait échouer le run. Une lecture du miroir absente ne peut pas produire PASS. Chaque
   différence est listée (clés manquantes/excédentaires, valeurs qui
   diffèrent, image « avant » incohérente, séquence de journal manquante/
   dupliquée/inattendue, évènement antérieur à la frontière de bootstrap,
   rejeu divergent) — jamais seulement comptée.
8. **`freshness`** — trois mises à jour isolées et marquées pour ce run.
   Chaque écriture est suivie de capture, chargement et relecture du marqueur
   dans le miroir avant la suivante. Le temps jusqu'à cette observation est une
   borne supérieure écriture → miroir, évaluée contre le SLO (10 s par défaut).
   La publication du lot brut est mesurée séparément. La capture Docker et le
   chargeur SQL bornés ne prouvent pas un lecteur CDC déjà actif en permanence.

Les fonctions de génération, de normalisation canonique, de rapprochement et
de statistiques de latence sont **testées hors ligne** :
`tests/qualification/` ne contacte jamais un système réel. L'orchestrateur
(`orchestrator.py`) dépend d'adaptateurs ; `build_real_adapters` compose
maintenant le pilote IBM i, la copie/capture Docker, le stockage S3/GCS et le
chargeur Snowflake. Sans `--offline-fake`, la CLI exige un runner Linux avec
Docker, une identité cloud et les secrets référencés ; un défaut de
préparation renvoie `AdapterWiringError`. Le rapprochement appelle le chargeur
avant de lire Snowflake ; un échec d'adaptateur, une sélection d'étapes vide,
une fraîcheur non mesurée ou une rotation sans oracle ordonné font échouer le
run. Une exception d'adaptateur devient un échec sans recopier son message
potentiellement sensible dans le rapport. Ce paquet ne prouve donc pas encore
une qualification nocturne sur un système externe.

## Configuration (YAML ou JSON)

Le run est piloté par un fichier de configuration ; aucun secret n'y figure
en clair, seulement des références (variable d'environnement `${VAR}` ou
chemin de fichier secret déjà déposé hors dépôt). Voir
`src/quadringent_qualification/config.py` pour le schéma complet ; extrait
représentatif :

```yaml
run_id: "${QUALIF_RUN_UUID}"
table:
  qualified_name: "${QUALIF_LIBRARY}.QUALIF_ORDERS"
  primary_key: ORDER_ID
  columns:
    - {name: ORDER_ID, kind: integer}
    - {name: LABEL, kind: varchar, length: 40}
    - {name: CODE, kind: char, length: 8}
    - {name: AMOUNT, kind: decimal, precision: 11, scale: 2}
    - {name: EVENT_DATE, kind: date}
    - {name: UPDATED_AT, kind: timestamp, timestamp_precision: 6}
    - {name: NOTE, kind: varchar, length: 80}
source:
  driver: ibmi_java
  library_whitelist: ["${QUALIF_LIBRARY}"]
  connection_secret_file: "${QUALIF_SOURCE_SECRET_FILE}"
  journal_library: "${QUALIF_LIBRARY}"
  journal_name: "${QUALIF_JOURNAL_NAME}"
  source_time_zone: "${QUALIF_SOURCE_TIME_ZONE}" # fuseau IANA vérifié contre IBM i
  allow_dml: false       # true seulement pour une table synthétique dédiée et vérifiée
  allow_rotation: false  # true seulement pour un journal de qualification dédié
capture:
  image: "${QUALIF_CAPTURE_IMAGE}"
  max_seconds: 180
storage:
  backend: gcs   # ou s3
  bucket: "${QUALIF_BUCKET}"
  raw_prefix: "qualification/${QUALIF_RUN_UUID}"
  checkpoint_location: "${QUALIF_STATE_LOCATION}"
warehouse:
  loader: snowflake
  account_secret_file: "${QUALIF_SNOWFLAKE_SECRET_FILE}"
  database: "${QUALIF_SF_DATABASE}"
  schema: "${QUALIF_SF_SCHEMA}"
steps: [seed, snapshot, changes1, capture, changes2, capture, rotate, changes3, capture, reconcile, freshness]
bootstrap_receiver: null   # les deux champs restent nuls si le snapshot fixe la frontière
bootstrap_sequence: null   # sinon ils sont fournis ensemble
```

Une variable `${VAR}` non résolue fait échouer le chargement avec la liste
**complète** des variables manquantes (`ConfigError`), pas seulement la
première.

Le chargeur Snowflake réel exige un `run_id` UUID canonique, conforme à l'identité
de tentative de la copie initiale IBM i. `storage.checkpoint_location` désigne
la table DynamoDB (S3) ou le bucket d'état (GCS) déjà provisionné. Le schéma
Snowflake doit être créé pour ce seul run et se terminer par les 12 premiers
caractères hexadécimaux en majuscules du SHA-256 de `run_id`, précédés de `_`
(par exemple `QUAL_<suffixe>`). Cette règle empêche de mêler les lignes
d'historique de plusieurs runs ; le chargeur ne crée pas le schéma ni les
droits Snowflake.

## Secrets requis (par référence uniquement)

| Rôle | Référence attendue | Jamais |
|---|---|---|
| Connexion IBM i | `source.connection_secret_file` (fichier privé ou `keychain:<service>` sur macOS) | Mot de passe en clair dans la configuration, les logs ou les arguments de processus |
| Entrepôt cible | `warehouse.account_secret_file` (clé privée Snowflake) | Clé en clair dans le dépôt ou dans un receipt |
| Stockage brut | identité IAM/Workload Identity du runner CI | Clé d'accès statique en variable d'environnement persistée |

Le harnais de référence illustre le bon réflexe : mot de passe
généré et lu depuis le trousseau macOS, jamais affiché ni écrit hors de lui ;
transmis au driver **par stdin**, jamais en argument de ligne de commande.
Le pilote IBM i générique (`real_source.py`) accepte soit un fichier secret
JSON avec `host`, `user` et `password`, possédé par le runner et lisible
seulement par lui (`0600`, sans lien symbolique), soit une référence
`keychain:<service>` sur macOS. Dans ce second cas, `source.host` et
`source.user` sont requis dans la configuration ; le mot de passe reste dans
le trousseau et n'est envoyé au conteneur que par stdin. L'image de capture
doit être épinglée par digest `sha256` (registre ou identifiant d'image locale)
et la connexion est TLS.
`allow_dml` et `allow_rotation` sont deux permissions indépendantes,
désactivées par défaut. Ne les activer que sur une table synthétique et un
journal dédiés, après vérification de leur propriétaire et de leur contenu.
Le fichier `warehouse.account_secret_file` est un JSON privé (`0600`, possédé
par le runner, sans lien symbolique) avec `account`, `user`, `role` et
`private_key_pem`. Le rôle suit la convention produit `QDT_ROLE_*`, dont le
warehouse est `QDT_WH_*`. Le pilote Snowflake lit ce fichier sans exposer la
clé dans les arguments de processus.

L'adaptateur Snowflake disponible dans `real_warehouse.py` exécute le chargeur
SQL du produit et relit l'historique et le miroir, sous une limite de lignes.
Son contrôle de rejeu compare l'enveloppe canonique complète pour chaque
identifiant des événements des lots bruts validés, retrouvés par les reçus de
journal et la preuve de copie initiale. Chaque référence répétée est comptée
au-delà de la première observation, comme identique ou divergente ; les listes
d'identifiants concernés restent dans le JSON. Ces comptes portent sur les
références brutes, pas sur des tentatives internes du chargeur Snowflake.
Le rapport conserve la preuve brute même si le chargeur rejette ensuite un
conflit. La CLI compose les adaptateurs réels ; une
exécution complète sur source et entrepôt réels reste nécessaire avant de
qualifier un run nocturne.

## CLI

```sh
python -m quadringent_qualification.cli run --config path/to/run.yaml --steps all
python -m quadringent_qualification.cli run --config path/to/run.yaml --steps seed,snapshot,reconcile
python -m quadringent_qualification.cli report --run-json run/<run_id>/report.json --out-markdown run/<run_id>/report.md
```

`run` écrit `report.json` (rapport complet, toutes les différences listées)
et `report.md` (résumé PASS/FAIL par étape, comptes, p50/p95 de latence,
emplacement de coûts explicitement marqué absent tant qu'aucune mesure n'a
été fournie) sous `run/<run_id>/` par défaut.

`--offline-fake` utilise des adaptateurs en mémoire (les mêmes que les tests)
pour vérifier le câblage CLI → orchestrateur → rapport sans système externe :
utile en CI pour qualifier ce paquet lui-même, pas pour qualifier le produit.
Le JSON enregistre `execution_mode: offline_fake` ou `real`, également affiché
dans le résumé Markdown. Un rapport ancien ou construit sans provenance est
explicitement marqué sans mode d'exécution renseigné ; aucun mode réel n'est
déduit de son statut PASS. Un rapport ancien sans lecture du miroir est marqué
« non vérifié » lors de son affichage.

## Classification des échecs

Un run peut échouer pour trois raisons distinctes, à ne pas confondre dans
l'astreinte :

1. **Échec produit** — une étape DML/`snapshot`/`capture`/`rotate` renvoie un
   code de sortie non nul côté lecteur/chargeur produit, ou le rapprochement
   final détecte un écart (clé manquante, valeur divergente, séquence de
   journal trouée ou dupliquée, image « avant » incohérente, rejeu divergent,
   suppression non appliquée). C'est le seul cas qui doit ouvrir un ticket
   produit.
2. **Infrastructure source indisponible** — la connexion IBM i échoue avant
   toute étape DML (authentification refusée, hôte injoignable, journal non
   accessible). Un IBM i de qualification partagé et gratuit n'a aucune
   garantie de disponibilité (§ 6 du design) ; ce cas doit être distingué
   explicitement d'un échec produit dans le résumé de run, jamais compté
   comme un FAIL produit.
3. **Échec du harnais** — une erreur dans `quadringent_qualification`
   lui-même (bug de configuration, adaptateur mal câblé, timeout du côté
   orchestrateur). Il s'agit d'un bug de ce paquet, pas du produit ni de la
   source ; `AdapterWiringError` (préparation des adaptateurs réels invalide) en est
   un cas particulier documenté.

En pratique : la CLI distingue `ConfigError` (code 2), préparation
d'adaptateur incomplète (code 3) et échec de run (code 1, `report.status ==
"FAIL"`) — un tableau de bord nightly doit conserver cette distinction plutôt
que de réduire le résultat à un booléen unique.

## Ajouter une nouvelle cible

1. Écrire un nouveau fichier de configuration (YAML ou JSON) décrivant la
   table, la bibliothèque, le bucket/préfixe et le schéma Snowflake cibles —
   toujours par référence pour les secrets, jamais en clair.
2. Si la table cible n'a pas les colonnes génériques attendues par le
   générateur par défaut (`ORDER_ID`/`LABEL`/`CODE`/`AMOUNT`/`EVENT_DATE`/
   `UPDATED_AT`/`NOTE`), fournir un générateur de scénario dédié plutôt que
   `generator.step_plan` — la fonction `require_default_columns` échoue tôt
   et explicitement sur un schéma incompatible.
3. Vérifier que les adaptateurs réels (`build_real_adapters`, voir
   « Reste à faire » ci-dessous) savent parler à cette cible : whitelist de
   bibliothèque/table côté driver IBM i, backend de stockage (S3 ou GCS),
   chargeur Snowflake.
4. Lancer d'abord avec `--offline-fake` pour vérifier que la configuration se
   charge et que les étapes demandées existent, puis avec les adaptateurs
   réels sur un run borné (`--steps seed,snapshot,reconcile` par exemple)
   avant d'activer le scénario complet en nightly.

## Reste à faire pour un run nightly réel

Ce paquet fournit la logique pure et quatre adaptateurs réels composés par la
CLI. Leur contrat est testé hors ligne. Pour annoncer un run nightly qualifié,
il reste à exécuter et mesurer le scénario complet sur une cible dédiée :

- **`SourceDriver` réel** : `real_source.py` lance le pilote Java de
  l'image de capture. Le
  pilote Python refuse toute DML hors de l'ensemble exact généré par le
  scénario ; Java ajoute une garde sur le verbe, la table et le journal
  réellement attaché à celle-ci avant toute écriture ou rotation. Le mot de
  passe passe par stdin. L'oracle renvoie les `SRC_RECEIVER` par ordre d'attachement
  et les `SRC_ROWPOS` par couple receiver/séquence. La bibliothèque du receiver
  est lue dans `QSYS2.JOURNAL_RECEIVER_INFO` et passée à `DISPLAY_JOURNAL`,
  sans supposer qu'elle égale celle du journal. Ces contrats passent hors
  ligne ; ils n'ont pas encore été vérifiés sur un IBM i réel avec un journal
  de qualification dédié.
- **`CaptureRunner` réel** : `real_capture.py` invoque la copie initiale puis
  le lecteur continu de l'image épinglée par digest, avec le mot de passe
  transmis uniquement sur stdin. Il utilise une clé de checkpoint propre au
  `run_id`, le préfixe de journal du produit et des reçus JSON bornés pour
  produire `CaptureResult`. L'orchestrateur transmet au job de copie la
  frontière lue juste avant l'instantané, dont la bibliothèque du receiver
  peut différer de celle du journal. Le lecteur commence à `last_sequence + 1`
  si aucun checkpoint durable n'existe ; un nombre de lignes reçu peut
  être enregistré sans fabriquer de faux évènements. Le runner Linux utilise
  le réseau hôte pour accéder à son identité cloud de VM et exige que l'image
  exacte soit déjà présente localement (`--pull=never`). La copie/capture sur
  une vraie cible reste à vérifier.
- **`StorageBackend` réel** : `real_storage.py` réutilise la lecture bornée des
  backends S3/GCS du produit et ajoute le listage/horodatage dans le préfixe
  du run ; l'identité cloud vient du runner. Le module passe ses tests hors
  ligne et est branché à la CLI. Son listage et sa lecture ont été éprouvés
  séparément sur des buckets temporaires ; le scénario nightly complet reste
  à éprouver.
- **`WarehouseLoader` réel** : `real_warehouse.py` appelle maintenant le
  chargeur SQL du produit, relit l'historique Snowflake et contrôle les lots
  bruts publiés. Il est branché à la CLI ; il reste à éprouver ce trajet sur
  une vraie destination. Ce mode SQL ne qualifie pas à lui seul la reprise
  Snowpipe Streaming utilisée par le déploiement continu.
- **`freshness` en runtime** : les trois écritures, le reçu indexé, les lots
  validés et le rapprochement final sont câblés ; les exécuter sur la source
  synthétique et les buckets réels. Sans association vérifiable, le run est
  FAIL, pas une latence fictive. Une mesure séparée reste nécessaire pour le
  seuil de 10 s jusqu'au miroir Snowflake.
- **Secrets en environnement CI** : décider du mécanisme de dépôt des
  références de secret (fichier monté, secret manager cloud) pour le
  pipeline nightly, et le documenter ici une fois choisi.
- **Tableau de bord des runs** : § 6 du design mentionne un tableau de bord ;
  ce paquet produit `report.json`/`report.md` par run, pas encore
  d'agrégation multi-runs.
