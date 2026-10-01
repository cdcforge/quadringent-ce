# Destination Snowflake — historique et miroir

Décision produit : `docs/decisions/2026-09-23-miroir-snowflake.md`. Ce
document décrit les modules livrés pour l'implémenter — DDL, chargeur
Snowpipe Streaming ou SQL synchrone, câblage config/chart, script de mise en service,
métriques. Les identités et valeurs ci-dessous sont génériques : aucune ne
provient d'une installation réelle.

## Vue d'ensemble

Par table source, le produit livre :

- une table d'**historique**, un rang par changement, chargée par Snowpipe
  Streaming (défaut) ou par `MERGE` SQL synchrone pour les petits flux ;
- une table **miroir**, une ligne par clé métier, matérialisée par un
  `MERGE` émis par le chargeur juste après chaque flush de l'historique
  (option B — pas de tâche planifiée, pas de droit `EXECUTE TASK`).

```text
lot brut durable (RawBatch, après checkpoint)
        |
        v
HistoryStreamingLoader --append_rows--> canal Snowpipe Streaming --> HISTORY
        |                                                              |
        | (une fois le flush confirmé)                                 |
        v                                                              v
MirrorMergePlan --MERGE (dédup event_id, fenêtre courante)--> MIRROR
```

L'option A (miroir piloté par une tâche planifiée, intervalle minimal 10 s)
reste disponible en mode expert : elle découple le chargeur du miroir mais
exige `EXECUTE TASK` pour le rôle propriétaire (voir « Script de mise en
service » ci-dessous).

## Génération DDL (`quadringent.snowflake_destination`)

`TableDestinationPlan` traduit les colonnes IBM i découvertes en
`CREATE TABLE IF NOT EXISTS` pour l'historique et le miroir, colonnes
techniques incluses.

### Table de correspondance de types

| Type IBM i | Type Snowflake | Remarque |
|---|---|---|
| `CHAR(n)` / `VARCHAR(n)` | `VARCHAR(n)` | `n` en caractères |
| `GRAPHIC(n)` / `VARGRAPHIC(n)` | `VARCHAR(n)` | DBCS ; Snowflake compte en caractères Unicode |
| `DECIMAL(p,s)` / `NUMERIC(p,s)` | `NUMBER(p,s)` | rejeté si `p` &gt; 38 |
| `SMALLINT` | `NUMBER(5,0)` | |
| `INTEGER` | `NUMBER(10,0)` | |
| `BIGINT` | `NUMBER(19,0)` | |
| `DATE` | `DATE` | |
| `TIME` | `TIME` | |
| `TIMESTAMP(p)` | `TIMESTAMP_NTZ(p)` | `p` de 0 à 9 |
| `BINARY(n)` / `VARBINARY(n)` | `BINARY(n)` | rejeté si `n` &gt; 8 388 608 octets |
| `CLOB(n)` / `DBCLOB(n)` | `VARCHAR(n)` | rejeté si `n` &gt; 16 777 216 caractères |
| `BLOB(n)` | `BINARY(n)` | même limite que BINARY ; rejeté au-delà, jamais tronqué |

Une colonne caractère en CCSID 65535 (binaire non converti) est refusée
explicitement : elle doit être reclassée en `BINARY`/`VARBINARY` côté
découverte. Tout type sans correspondance déclarée lève
`UnsupportedColumnTypeError` — jamais une conversion silencieuse.

### Colonnes techniques

| Historique | Miroir |
|---|---|
| `EVENT_ID VARCHAR NOT NULL` | `EVENT_ID VARCHAR NOT NULL` |
| `OPERATION VARCHAR NOT NULL` | — |
| `JOURNAL_RECEIVER VARCHAR NOT NULL` | `JOURNAL_RECEIVER VARCHAR NOT NULL` |
| `JOURNAL_SEQUENCE NUMBER(38,0) NOT NULL` | `JOURNAL_SEQUENCE NUMBER(38,0) NOT NULL` |
| `COMMIT_TIMESTAMP TIMESTAMP_NTZ(6) NOT NULL` | `COMMIT_TIMESTAMP TIMESTAMP_NTZ(6) NOT NULL` |
| `INGESTED_AT TIMESTAMP_LTZ NOT NULL` | — |
| — | `MIRROR_UPDATED_AT TIMESTAMP_LTZ NOT NULL` |

Le miroir porte en outre une clause `PRIMARY KEY` informative (non contrainte
côté Snowflake) sur les colonnes de clé déclarées.

## Chargeur Snowpipe Streaming (`quadringent.snowflake_streaming_loader`)

- **Canal** : nommé de façon déterministe par table et run de copie initiale
  (`channel_name_for(scope, history_table, stream_id)`). Deux redémarrages
  d'un run rouvrent le même canal ; une nouvelle copie utilise un autre canal
  et un autre checkpoint de chargeur.
- **Jeton d'offset** : la position de journal IBM i (`RECEIVER:SEQUENCE`,
  séquence complétée à gauche pour rester comparable en texte).
- **Reprise** : au `latest_committed_offset_token` du canal et au checkpoint
  du chargeur. Les reçus de scan relient chaque fenêtre à sa précédente,
  y compris les fenêtres vides qui portent une rotation de receveur. Si un
  arrêt intervient entre le commit Snowpipe et celui du checkpoint GCS, le
  chargeur rejoue le MERGE sans réinsérer les reçus déjà confirmés. Si le
  canal a été recréé sans offset, il vérifie directement les `EVENT_ID`
  présents dans l'historique avant tout nouvel append.
- **Nouvelle copie initiale** : attend la preuve durable du Job ; vide puis
  reconstruit le miroir depuis le nouvel instantané, conserve l'historique
  des runs précédents et place son checkpoint à la frontière de la copie.
  Un reçu qui chevauche cette frontière est validé puis filtré pour ne
  charger que les événements postérieurs.
- **Client injectable** : `StreamingClient`/`StreamingChannel` (protocoles
  minimaux) ; `SnowpipeStreamingClientAdapter` enveloppe le SDK réel (import
  différé, la dépendance `snowpipe-streaming` n'est pas requise pour les
  tests) ; `FakeStreamingClient` sert les tests unitaires.
- **MERGE miroir** (`MirrorMergePlan`) : déduplique d'abord par `EVENT_ID`
  (absorbe un jeton d'offset rejoué ou un canal recréé sous un autre nom),
  dans le lot courant, puis retient par clé métier la plus haute séquence de
  ce lot. L'ordre entre receveurs vient de la chaîne de reçus et non du nom
  ou de la séquence du receveur. Le `MERGE` est rejoué même si le canal
  Snowpipe avait déjà validé le lot avant un arrêt. `u_before` est ignoré (état antérieur technique, ne doit jamais
  écraser l'état courant). Une suppression matchée efface la ligne miroir ;
  non matchée, elle n'insère rien.

## Durées du cycle Streaming

Le journal existant du chargeur émet un relevé `loader_cycle` par passage
ayant ajouté des événements ou matérialisé le miroir, et en cas de panne.
Un passage au repos ou une fenêtre vide avançant seulement le checkpoint
n'émet pas de relevé de succès. Aucun événement par ligne ni sondage
Snowflake supplémentaire n'est ajouté.

Le format `quadringent-loader-cycle-v1` contient les bornes UTC
`started_at`/`finished_at`, la durée monotone `cycle_ms` et les durées
cumulées `stages_ms` en millisecondes : découverte et lecture du RAW
(`discovery_raw`), création du client et ouverture du canal (`open_channel`),
`append`, demande de `flush`, attente du `commit`, matérialisation du miroir
(`merge`, y compris l'effacement d'une nouvelle copie), puis `checkpoint`.
Une étape non exécutée reste `null` : canal réutilisé, flush désactivé ou
étapes non atteintes après une panne. Les contrôles, la recherche SQL
`EVENT_ID` déjà requise à la reprise et la fermeture d'un client ponctuel
restent dans la durée totale, sans être attribués à ces sept étapes.

`status` indique le résultat ; `failed_stage` désigne l'étape interrompue,
ou `prepare` pour une erreur hors de ces étapes. Le checkpoint du reçu
concerné reste après la validation du commit et du MERGE ; les reçus déjà
achevés conservent leur progression. Le relevé ne contient aucun message
d'exception, clé métier, position de journal ni identifiant de compte.
Son `table_tag` est le SHA256 de l'identité interne de table déclarée dans
le manifeste. Le service de journaux v2 ne l'expose qu'à la table exacte
résolue depuis le pipeline et la destination ; il reconstruit un texte
borné à partir des champs validés.

Ces durées décrivent le **cycle du chargeur**, pas le parcours depuis
l'acquittement IBM i, ni le délai avant que le RAW soit découvert. Elles
n'alimentent pas les séries de retard métier et ne qualifient pas le
maximum de 10 s. La conservation reste celle du journal Kubernetes existant
(2 000 dernières lignes, au plus trois pods).

## Historique SQL synchrone (`quadringent.snowflake_sql_loader`)

`QUADRINGENT_HISTORY_MODE=sql` sélectionne un `MERGE` historique paramétré,
par lots de 100 événements maximum, avant le `MERGE` miroir. Le chargeur
reste à un seul écrivain par table. `EVENT_ID` est la clé de rapprochement :
si un processus s'arrête après le premier `MERGE`, le reçu durable est relu,
l'historique ignore les lignes déjà présentes et le miroir est rejoué avant
le checkpoint. Les littéraux décimaux du JSON brut sont conservés sans
conversion en `float` puis castés selon le type IBM i déclaré ; l'enveloppe
base64 des colonnes binaires est contrôlée avant toute mutation SQL.

Ce mode sollicite le warehouse à chaque lot. Il est destiné aux petits flux
où la latence prime ; un gros rattrapage peut multiplier les requêtes. Le
warehouse dédié doit garder son `AUTO_SUSPEND` court et le coût doit être
mesuré sur le volume du site. Le mode par défaut reste `streaming`.

Après son initialisation, le chargeur au repos vérifie les reçus dans le
stockage objet sans interroger Snowflake. L'âge de la dernière mutation n'est
mesuré qu'après un lot traité :
une requête de retard toutes les 30 s empêcherait le warehouse de se suspendre
malgré `AUTO_SUSPEND`. Le cockpit affiche les dernières livraisons conservées
dans les journaux et signale l'absence de mesure récente sans présenter le
repos comme une preuve de fraîcheur source.

Le processus conserve les octets des reçus immuables publiés par `put_once`
dans un cache LRU commun aux tables et aux cycles : au plus 5 000 entrées
et 16 MiB d'octets de reçus. Le cache est isolé par instance de backend,
préfixe et clé ; les reçus du journal partagé sont ainsi relus une seule
fois tant qu'ils restent en mémoire. Chaque passage liste encore les reçus
pour découvrir les nouvelles fenêtres et revalide toute la chaîne, ses
rotations et ses conflits. Les limites de listing et de lecture restent
appliquées, y compris sur une lecture servie depuis le cache.

Les checkpoints, preuves de copie, manifestes et payloads ne sont pas mis
en cache. Une éviction ou un redémarrage provoque une nouvelle lecture
objet. Cette réduction des GET de reçus est une optimisation locale ;
elle ne garantit pas une livraison en 10 s, qui nécessite une mesure du
parcours complet sur le runtime cible.

## Câblage config/chart

- `controlPlane.v2.loaderHistoryMode` : `streaming` (défaut) ou `sql` ; la
  chart transmet `QUADRINGENT_V2_LOADER_HISTORY_MODE` au control plane, qui
  place `QUADRINGENT_HISTORY_MODE` dans le Deployment du chargeur. En mode
  `sql`, aucun canal ni profil Snowpipe n'est ouvert.

- `QUADRINGENT_DESTINATION_MODE` (`quadringent.site_config`) : `copy_merge`
  (défaut, chemin COPY INTO + MERGE existant) ou `streaming`. Absente : les
  sites déjà déployés restent sur `copy_merge` sans changement de
  comportement.
- `QUADRINGENT_STREAMING_PROFILE_JSON` : chemin du `profile.json` du SDK
  (clé privée + compte), monté en volume depuis un Secret existant — jamais
  en variable d'environnement en clair. Obligatoire quand
  `destination_mode=streaming`, refusé sinon.
- Chart (`chart/values.yaml`) : `site.destinationMode` et
  `streaming.profileSecret`/`streaming.mountPath` publient le contrat
  déclaratif (chemin attendu du profil) via `QUADRINGENT_STREAMING_PROFILE_JSON`.
  Le Deployment « capture » (lecteur IBM i) ne charge jamais Snowflake
  (raw-first, voir `continuous.py`) : cette chart ne monte donc pas encore
  `streaming.profileSecret` dans un Pod — c'est la charge qui exécutera le
  chargeur Snowpipe Streaming, hors périmètre de cette chart pour l'instant,
  qui devra monter ce Secret sous `streaming.mountPath`.

## Script de mise en service (control plane v2)

`DestinationsService.create(..., destination_mode=, mirror_option=)`
(`quadringent_control_plane.v2.services.destinations`) étend le script SQL
généré :

- `destination_mode="streaming"` ajoute `GRANT CREATE TABLE ON SCHEMA
  QUADRINGENT.CURATED` : le rôle applicatif crée et possède l'historique et
  le miroir, donc aucun `GRANT INSERT/SELECT/UPDATE/DELETE` distinct n'est
  requis (propriétaire de ce qu'il crée). Snowpipe Streaming haute
  performance ne crée pas d'objet `PIPE` (contrairement au Snowpipe
  classique) : rien à ajouter côté pipe.
- `mirror_option="task_driven"` (option A, mode expert, seulement sous
  `destination_mode="streaming"`) ajoute `GRANT EXECUTE TASK ON ACCOUNT`.
- `destination_mode="copy_merge"` (défaut) laisse le script inchangé.

## Métriques exposées

`quadringent.snowflake_streaming_loader.LagQueryPlan` mesure, par une
requête `DATEDIFF` à la milliseconde sur `MAX(COMMIT_TIMESTAMP)`, l'écart entre le dernier
événement chargé et l'instant de la mesure — jamais une moyenne, jamais un
débit. Une table encore vide renvoie `None`, jamais `0`.

Ces deux valeurs sont portées par
`quadringent_control_plane.v2.services.observation.PipelineObservation` :

- `history_lag_seconds` : fraîcheur de l'historique ;
- `mirror_lag_seconds` : fraîcheur du miroir.

Comme le reste de `PipelineObservation`, elles restent `None` avec une
raison explicite dans `absent_reasons` tant qu'aucun fournisseur
d'observation réel ne les alimente — jamais une valeur inventée.

## Vérification en direct (qualification)

La décision du 23/09/2026 s'appuie sur une mesure réelle contre le compte
Snowflake de qualification (103 événements rejoués, oracle 110 clés,
91-95 supprimées) : historique 5,2 s médiane / 7,2 s au 95e centile,
miroir B 6,3 s / 8,3 s. Voir la décision pour le détail des trois options de
miroir comparées et leurs limites de mesure (compte unique, faible volume).

Une seconde vérification (23/09/2026, après l'implémentation) a rejoué le
module produit lui-même (`TableDestinationPlan`, `HistoryStreamingLoader`,
`MirrorMergePlan`) contre `QUALIFICATION_DB.DEST_CHECK` (nouveau schéma),
en lisant en lecture seule un lot GCS de qualification (snapshot de
100 lignes et 5 lots journal, dont un rejeu partiel imbriqué détecté et
correctement absorbé par le dédoublonnage). Résultat : 110 lignes miroir,
0 écart contre l'oracle. Deux défauts réels corrigés à cette occasion :

- le profil Snowpipe Streaming doit porter `"authorization_type": "JWT"`
  pour l'authentification par paire de clés — `"KEY_PAIR"` est refusé par
  le SDK (`ConfigError: unsupported authorization type`) ;
- `INGESTED_AT` doit porter `DEFAULT CURRENT_TIMESTAMP()` dans le DDL
  historique : une ligne streamée par le SDK ne porte jamais cette colonne
  elle-même (aucune expression SQL évaluée côté client), et sans défaut
  serveur la contrainte `NOT NULL` bloquait silencieusement la
  matérialisation — le `COPY INTO` du pipe géré s'exécutait
  (`pendingFileCount` retombait à 0) sans qu'aucune erreur ne remonte ni
  dans le SDK ni dans `INFORMATION_SCHEMA.COPY_HISTORY`.

Constat opérationnel supplémentaire : la matérialisation d'un canal
Snowpipe Streaming vers la table (pipe géré `<historique>-STREAMING`,
`SYSTEM$PIPE_STATUS`) a pris de l'ordre de la minute lors de cette
vérification (nombreux petits lots, compte de qualification partagé) —
sensiblement au-delà de la médiane mesurée par la décision (5,2 s, une
seule ligne par flush). Le chargeur de destination ne doit donc jamais
supposer une visibilité immédiate après `wait_for_commit` : c'est
exactement le rôle du retard mesuré (`LagQueryPlan`), pas une hypothèse de
latence fixe.

Sur GKE DEV, la chaîne réelle IBM i → GCS → Snowflake a ensuite été mesurée
sur des mises à jour synthétiques isolées : 9,56 à 31,31 s entre l'acquittement
IBM i et le miroir, malgré un lecteur et un chargeur sondant chaque seconde,
un canal persistant et un flush explicite par lot. Le lecteur publiait
généralement le brut en 1 à 4 s ; l'attente du commit Snowpipe Streaming
dominait les essais lents. La cible de 10 s maximum n'est donc **pas
qualifiée**. Les essais de faible volume de la décision initiale ne sont
pas une garantie pour une installation. Le flush par lot peut accroître le
coût ou provoquer du throttling ; il reste un réglage de site, désactivé
par défaut.

Un essai de bout en bout GKE DEV avec le chargeur SQL local, sur des mises à
jour synthétiques successives, a donné 12,39 s puis 12,06 s pour les
premiers événements après démarrage du lecteur, et 7,37 s puis 7,60 s en
régime établi (acquittement IBM i → miroir visible). L'historique a conservé
un nombre de lignes égal au nombre d'`EVENT_ID` distincts. Cela valide une
piste de réduction de latence, **pas** la cible de 10 s maximum après
démarrage, ni les images ou artefacts de release : ces essais utilisaient le
code local pour le chargeur SQL.

Pour la vérification initiale du 23/09, objets créés (nettoyés en fin de
vérification, warehouse suspendu, aucune tâche laissée active) : schéma
`QUALIFICATION_DB.DEST_CHECK`, tables
`QDC_ORDERS_HISTORY`/`QDC_ORDERS_MIRROR`, pipe géré
`QDC_ORDERS_HISTORY-STREAMING` (créé implicitement par le SDK, jamais par
ce module).
