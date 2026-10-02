# Qualifier une installation native

La commande installable observe les lecteurs et chargeurs créés par le produit
sur GKE, EKS ou k3s sur VM. Elle ne lance aucun second captureur/chargeur et ne
remplace pas le harnais Docker de développement.

```sh
quadringent qualification native --config /chemin/prive/site.json \
  --out-dir /chemin/prive/preuves --phase baseline
```

Le fichier JSON et ses références sont en **0600**, le dossier de sortie en
**0700**. Chaque phase écrit un fichier exclusif `<phase>.json` en 0600 ; une
preuve existante n'est jamais écrasée. Pour plusieurs observations, choisir un
dossier distinct par ACK. La sortie standard donne seulement statut, chemin et
verdict. Les erreurs expurgées renvoient un code non nul ; les valeurs métier
et les identifiants du site restent dans les preuves privées.

## Admission de la release

`admit-release` accepte une configuration contenant seulement `release`.
Télécharger les deux artefacts originaux du **même run et attempt** par la
session `gh` authentifiée : `oci-scan-receipt` et `oci-admission-metadata`.
Leur rétention CI est actuellement d'un jour : l'admission fraîche doit être
faite **avant expiration**, sans prolongation implicite.

```json
{
  "release": {
    "mode": "fresh",
    "repository": "cdcforge/quadringent-community",
    "visibility": "private",
    "run_id": 123,
    "attempt": 1,
    "source_sha": "<commit exact de 40 caractères hexadécimaux>",
    "tag": "v0.2.3",
    "workflow_sha256": "<SHA256 du workflow release relu>",
    "artifact_id": 456,
    "artifact_zip": "/chemin/prive/oci-scan-receipt.zip",
    "metadata_artifact_id": 457,
    "metadata_artifact_zip": "/chemin/prive/oci-admission-metadata.zip",
    "metadata_root": "/chemin/prive/metadata"
  }
}
```

Les nombres sont des exemples de structure, pas des preuves. La visibilité
attendue est obligatoire (`private` ou `public`) et vérifiée sans repli.
La commande exige le workflow relu, le tag/source exact, les sept jobs CI
canoniques du push main et toutes les étapes de build, scans des six variantes
et couches, SBOM, sceau, métadonnées, promotion et signatures réussies.
Les ZIP sont liés aux ID/run/attempt et à leur digest GitHub authentifié.
Les 21 originaux OCI sont comparés au `passed.json` original : trois wrappers,
index, six manifestes et six configs ; aucune couche n'est reconstruite.

```sh
quadringent qualification native --config /chemin/prive/admission.json \
  --out-dir /chemin/prive/admission-preuves --phase admit-release
```

Conserver les deux ZIP, les métadonnées, le reçu original et le rapport
`admit-release.json`. Épingler le `admission_sha256` retourné dans la
configuration privée de qualification, avec `release.mode="conserved"`,
`release.admission="/chemin/prive/admission-preuves/admit-release.json"` et
`release.admission_sha256="<SHA256 épinglé lors de l'admission authentifiée>"`.
Tous les autres champs release sont conservés. Les phases suivantes exigent
ce mode conservé : elles vérifient le pin, les ZIP et les 21 métadonnées puis
les images et identités actuellement exécutées. Elles ne prétendent pas
réauthentifier un artefact expiré ni revalider localement toutes les couches.

L'opérateur protège cette ancre locale et la configuration : changer le pin
constitue une nouvelle décision de confiance. Un JSON avec seulement
`complete_seal_verified=true` est refusé. La preuve complète des scans est
située dans CI ; l'admission ne prouve pas que le site a été créé après elle.

## Contrat du site

Ajouter `native`, avec les champs suivants ; les valeurs viennent de
l'installation et du pipeline réel. Aucun secret IBM i ou Snowflake n'est
copié dans la configuration : les sondes utilisent les connexions des pods.

| Champs | Contrat |
| --- | --- |
| `kubeconfig`, `context`, `namespace` | Kubeconfig absolu régulier, contexte et namespace explicites |
| `pipeline`, `table_id`, `table` | Pipeline exact, ID table et `LIBRARY.TABLE` source |
| `reader_deployment`, `loader_deployment` | Noms exacts des deux Deployments du produit |
| `capture_digest` | Index OCI **capture** admis |
| `loader_digest` | Index OCI **control-plane** admis, comme le chargeur natif du chart |
| `columns`, `pk` | Fichier JSON 0600 `{ "ID": "int", "NOTE": "text" }`, clé primaire unique ; types int/decimal/text/date/timestamp |
| `database`, `schema`, `history`, `mirror` | Destination et tables Snowflake exactes |
| `journal_library`, `journal_name` | Journal IBM i réel |
| `copy_run_id`, `copy_boundary` | Copie initiale et frontière réelles, liées au Job et à son evidence key |
| `expected_history_mode` | Obligatoirement `streaming` ; un chargeur SQL ne satisfait pas ce contrat |
| `max_seconds`, `poll_seconds` | Budgets de collecte 1–120 s et intervalle 1–10 s, défauts 120/1 |
| `identity` | `irsa`, `vm-metadata`, `gke-workload-identity` ou `gcp-vm-metadata` |

Pour AWS : `expected_role_arn`, vérifié avec STS et le provider réel de la
session. Pour GCP : `expected_gcp_project`, `expected_gcp_service_account`,
`expected_gcs_bucket`, `gcs_read_key` ; identité Google metadata rafraîchie et
lecture GCS réelle de la preuve de copie. Sur VM GCP, ajouter
`expected_gcp_instance_id`, `expected_gcp_instance_name`, `expected_gcp_zone`,
`expected_gcp_machine_type` : métadonnées instance, scopes et nœud exact sont
vérifiés. Un fichier de clé statique n'est pas une preuve d'identité metadata.

Le pod Ready est lié par ses UID à ReplicaSet/Deployment et à son conteneur
produit, sans utiliser un sidecar pour certifier le digest. L'architecture
Linux du nœud sélectionne le manifeste/config exacts de son index scellé.

## Phases et références

`refs` contient seulement des chemins de fichiers 0600, sauf
`observations` et `observation_receipts` qui sont des listes ordonnées de
chemins. Aucun reçu ou acquittement source n'est fabriqué par la commande.

| Phase | Références / effet |
| --- | --- |
| `oracle` | Copie immuable liée au Job + relecture journal IBM i complète + ROWPOS indépendant ; écrit un oracle indépendant de Snowflake |
| `baseline` | Collecte cet oracle dans `baseline.oracle.json`, puis snapshot source/HISTORY/MIRROR |
| `snapshot` | `history_oracle` ; `before` après reprise pour lire aussi ROWPOS depuis son checkpoint |
| `positions` | `before` ; positions source exactes depuis son checkpoint |
| `pause` | `before` ; observe absence du Deployment lecteur et zéro pod sélectionné |
| `resume` | `before`, `pause_proof` pour une action explicite ; observe nouveau Deployment/pod Ready, mêmes noms/périmètre/candidat/identité |
| `observe` | `baseline`, `receipt` ; timestamps du premier HISTORY et MIRROR visibles, depuis début et ACK de la mutation |
| `bind-mutations` | `ack_receipts`, `history_oracle`, `pause_proof`, `resume_proof`, `before` ; lie les ACK aux images journal/ROWPOS réels |
| `evaluate` | `before`, `after`, `baseline`, `pause_proof`, `resume_proof`, `mutation_receipts`, `observations`, `observation_receipts` |

Les ACK externes explicitement autorisés contiennent les cinq champs de
périmètre (`pipeline`, `table`, `table_id`, `namespace`, `context`),
`acknowledged=true`, `affected_rows=1`, `operation` (insert/update/delete),
`write_started_utc`, `write_ack_utc` et `mutation_identity` (image complète
pour insert/update, PK pour delete). `expected`, s'il est fourni, doit égaler
`mutation_identity`. `source_positions` peut être fourni ; sinon le binder
exige une image indépendante unique. Les ACK bruts n'incluent jamais
`expected_events` ; le binder les ajoute depuis la source réellement relue.
Les listes ACK et observations couvrent les mêmes mutations dans le même
ordre. Une suppression exige un événement HISTORY `d` et la disparition de
la clé MIRROR, avec ligne attestée dans la baseline.

Séquence : admission fraîche → pin conservé → baseline → pause → mutations
externes acquittées → reprise et observations → oracle après reprise →
snapshot après reprise → bind-mutations → evaluate. Lancer les observations
assez tôt, éventuellement en parallèle de la reprise, pour mesurer la borne
réelle ≤10 s : une collecte tardive ne raccourcit pas la latence.

Par défaut, pause/reprise **observent** des actions effectuées séparément par
l'opérateur avec la CLI native. Pour les exécuter depuis cette commande,
installer l'extra `api` et activer explicitement :

```json
"actions": {
  "enabled": true,
  "environment": "dev",
  "api_config": "/chemin/prive/site-api.json"
}
```

La configuration API 0600 contient `url` et `token` du site choisi ; aucune
variable locale ne remplace cette cible. L'environnement DEV est une
restriction opérateur explicite, pas une attestation serveur inventée.
Le pipeline/table est relu par API avant POST, la transition est relue et
l'effet Kubernetes attendu est observé. Les actions utilisent uniquement
`/v2/pipelines/{id}/actions/pause|resume`, jamais `kubectl scale`. Le reçu
`<phase>.action.json` distingue action exécutée et observation seule.
`<phase>.action-request.json` conserve aussi la clé d’idempotence avant POST :
si un acquittement ou une relecture manque, l’exécution reste non attestée et
la phase échoue ; elle ne répète pas automatiquement la mutation.
Une dernière table active pausée supprime son reader ; avec une autre table
active du même journal, ce scénario d'absence ne peut pas être certifié.

## Verdict et limites

Les collectes restent `native_cdc_qualified=false`. `evaluate` peut qualifier
**le scénario observé et borné** : copie réelle, source=MIRROR, HISTORY exact
selon l'oracle indépendant, absence de duplications, pause/suppression puis
recréation du reader, checkpoint avancé sur le même receiver, ACK mutations
couverts par ROWPOS/HISTORY et visibilité HISTORY/MIRROR ≤10 s **depuis début
et ACK**, pour chaque mutation. Les identités/images courantes sont relues.

`rotation_qualified=false` et `maximum_10s_qualified=false` restent explicites :
ces observations ne prouvent ni rotation de receiver ni SLO universel.
Le budget est limité à 1 000 lignes/événements, 1–20 mutations, un receiver et
un reader/chargeur prêt. Une donnée manquante, permission refusée, fenêtre
partielle, drift de release ou seuil dépassé produit un verdict incomplet.
Aucune DML synthétique, migration, ressource cloud ou installation n'est créée
par ce CLI. Les sondes nécessitent les outils locaux `kubectl` et, pour
l'admission fraîche, `gh`, plus les capacités natives existantes des pods.

## Crash abrupt du lecteur (DEV uniquement)

Le scénario `crash` est distinct de `pause`/`resume`. Il exige une baseline
native qualifiée, l'admission conservée de la release et cette activation
opérateur explicite dans `actions` :

```json
{"enabled": true, "environment": "dev", "crash_reader": true}
```

Cette restriction locale n'est pas une attestation serveur de l'environnement.
Le kubeconfig doit déjà autoriser la lecture du pod, de son Deployment,
ReplicaSet et nœud, puis `pods/exec` sur le conteneur lecteur exact. La commande
n'ajoute aucun droit. Elle n'efface aucun pod et ne change aucune réplication.

L'image capture doit démarrer le lecteur comme enfant direct de
`/usr/bin/tini -g --`; aucun override du processus principal n'est accepté.
Le collecteur contrôle l'exécutable, les arguments attendus, le propriétaire,
le namespace PID, le démarrage du processus et d'init ainsi que l'identifiant
de démarrage du noyau. Ces gardes minimales sont conservées sans dump de
cmdline ou d'environnement. Il persiste `crash.action-request.json` avant
mutation, relit le même Pod UID et containerID et exige un unique pod
sélectionné, y compris les pods non prêts ou en terminaison, puis ouvre un pidfd et revalide
l'incarnation avant **un seul SIGKILL de l'enfant Python**. Un noyau sans pidfd,
une course, un timeout ou une intention impossible à écrire arrêtent le
scénario sans repli sur un simple PID ni répétition automatique.

```sh
quadringent qualification native --config /chemin/prive/site.json \
  --out-dir /chemin/prive/crash-preuves --phase crash
```

La phase attend au plus `max_seconds` (plafond 120 s) le redémarrage du même
pod sous le même Deployment/ReplicaSet, avec la même release et identité.
Elle exige `restartCount + 1`, un nouveau containerID et le `lastState`
kubelet lié à l'ancien containerID : signal 9 ou code 137, horodatage après
l'intention, motif différent de `OOMKilled`. Le marqueur préparatoire seul
et un pod Ready ne constituent jamais une preuve de crash ou de CDC.

Pour qualifier la récupération des données, organiser une mutation source
réellement acquittée pendant la fenêtre d'arrêt, puis recueillir l'oracle
journal/ROWPOS indépendant et le snapshot après reprise. Ajouter
`refs.crash_proof`, `refs.ack_receipts`, `refs.history_oracle`, `refs.before`
et `refs.after` vers les preuves privées. `bind-crash-mutations` lie les ACK
aux images source réelles ; référencer son résultat par
`refs.mutation_receipts`, puis exécuter `evaluate-crash`. Aucune mutation
source n'est produite par ces phases.

Les ACK doivent commencer après la terminaison prouvée et finir avant
le démarrage du nouveau conteneur (et non la simple observation Ready). Si le kubelet n'horodate qu'à la seconde, la borne basse
est la seconde suivante : une fenêtre trop courte est donc incomplète. La panne
du chargeur et un point de crash déterminé entre RAW et checkpoint ne sont
pas couverts par ce scénario lecteur.
L'évaluation contrôle la progression du checkpoint dans le même receiver,
la couverture journal/ROWPOS, HISTORY exact sans doublon, la conservation
des événements antérieurs et MIRROR réconcilié avec la source. Un résultat
`crash_recovery_qualified=true` reste limité à ce scénario :
`native_cdc_qualified=false`, aucune rotation de receiver ni latence maximale
10 secondes n'est ainsi qualifiée. L'image modifiée doit être reconstruite,
scannée et admise avant une exécution native réelle.
