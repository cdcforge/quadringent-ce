# Installer Quadringent sur un site

Ce guide décrit le mode expert (`helm` piloté à la main), avec les exemples
historiques AWS et API v1. Pour une première
installation en une commande (Terraform + Helm orchestrés), voir
[le mode par défaut](install-default.md) (`quadringent install --cloud
aws|gcp --target vm|cluster`) — il crée le socle infra (stockage, état,
identité) et la VM ou la liaison cluster, avant d'installer cette même
chart. Les deux modes partagent le même artefact (`chart/`).

Ce guide décrit une installation non productive. Les fichiers du dépôt sont
synthétiques ; ils ne prouvent ni accès IBM i, ni ressources cloud existantes.
La démonstration locale du [README](../../README.md) vérifie seulement l’API,
l’interface et la persistance des déclarations.

## 1. Préparer le site

Un responsable du site doit fournir :

- un IBM i joignable, des tables journalisées et un compte de lecture autorisé
  au catalogue, aux tables et au journal ; bibliothèque du journal, receivers,
  format des images et rétention suffisante pour la fenêtre de reprise ;
- le fuseau IANA de l’horloge IBM i (`ibmi.sourceTimeZone`, variable
  `AS400_SOURCE_TIME_ZONE`), relevé depuis `QTIMZON` ; aucun UTC n’est supposé ;
- le CA de confiance IBM i vérifié hors bande ; TLS sur les ports déclarés
  (JTOpen : base 9471, sign-on 9476, commande 9475 par défaut ; vérifier les
  valeurs du site et du lecteur avant ouverture réseau) ;
- un cluster Kubernetes, Helm 3, un namespace dédié, une classe de stockage
  pour l’état du control plane, un accès privé au registre d’images ;
- un bucket S3 et ses règles de rétention, une table DynamoDB de checkpoints,
  les rôles IAM bornés et trusts IRSA/OIDC pour capture, control plane, verifier ;
- un compte Snowflake, warehouse, base et schéma non productifs, intégration S3,
  stages et tables de destination. Snowpipe et notifications S3 sont configurés
  séparément de Helm. Aucun GRANT global de facturation au control plane.

Variante Google Cloud pour la capture autonome : avec
`QUADRINGENT_STORAGE_BACKEND=gcs` et l’extra Python `gcs`, les batches bruts vont
dans le bucket GCS `AS400_RAW_BUCKET` et les checkpoints et la garde de sign-on
dans le bucket GCS `AS400_CHECKPOINT_BUCKET` (objets `checkpoints/` et
`source-gates/`, remplacés seulement depuis la génération lue). L’identité du
runtime (identifiants par défaut de l’application) doit pouvoir lire, créer et
lister les objets ; elle n’a besoin d’aucun droit de suppression. Snowflake lit
le bucket par une intégration de stockage GCS et un stage externe. Le
control plane v2, la chart avec `storage.backend=gcs`, le chargeur Snowpipe
Streaming et le miroir MERGE ont été vérifiés ensemble sur GKE DEV ; voir
[le mode par défaut](install-default.md) pour cette installation. Les
fenêtres de preuve v1, la console S3 et Snowpipe par notifications décrits
ci-dessous restent propres au parcours AWS v1.

Le propriétaire de l’infrastructure valide les droits. Les exemples
`infra-values/iam-*.json` ne s’appliquent jamais sans adaptation. Pour la chaîne
Snowflake, consulter aussi [le guide autonome](autonomous-snowflake-dev.md).

## 2. Fournir les références et les images

Copier `infra-values/values-int.yaml` vers un fichier privé hors du clone.
Adapter `site.*`, `aws.*`, `as400.*`, les ServiceAccounts et rôles. Le namespace
Helm doit être exactement `site.namespace`. Les environnements autorisés sont
`dev`, `int`, `test`, `staging` ; `productionPromotionAllowed` reste `false`.
L’exemple emploie `dev` et `quadringent-demo`.

Les digests d’exemple n’attestent aucune image disponible. Construire les trois
images depuis la même source validée, les placer dans un registre privé autorisé,
puis reporter leur dépôt et leurs digests dans `image.repository`/`image.digest`,
`controlPlane.image.repository`/`controlPlane.image.digest` et
`observability.imageDigest`. Aucun tag mouvant n’est accepté. La configuration
`controlPlane.image.allowedRepositories` doit inclure ce registre. Le dépôt
`ghcr.io/quadringent/quadringent` des exemples est un substitut : il ne prouve
pas qu'un package existe à cette adresse.

Secrets attendus, créés par le mécanisme de secrets du site :

| Référence de configuration | Contenu |
|---|---|
| `ibmi.passwordSecret.name/key` | Mot de passe du compte de lecture |
| `as400.tlsCaSecret.name/key` | Bundle PEM de CA vérifié |
| `image.pullSecret` | Authentification du registre privé |
| `controlPlane.auth.proxySecret.secretName/secretKey` | Secret de proxy si auth activée |
| Secrets du verifier | Authentification Snowflake déclarée par le site |

Aucune valeur secrète dans Git, dans les values ou dans les arguments de processus.
Le CA est monté en lecture seule au chemin `as400.tlsCaFile`. Les modèles de
Jobs de flotte fournis par le site doivent conserver ce même volume et montage,
comme `infra-values/job-template-int.json`. Voir [TLS](../../docker/certs/README.md).

L’édition communautaire fonctionne sans clé de licence ni limite commerciale
de tables, y compris pour l’exemple à treize tables. Les contrôles de sécurité,
de capacité déclarée, de préparation et de continuité restent obligatoires.
Les anciennes références `controlPlane.licenseSecret` et la variable
`QUADRINGENT_LICENSE_KEY` ne sont plus utilisées. Retirer ces paramètres des
values et de l’environnement ; aucun Secret existant n’est supprimé par la chart.

## 3. Rendre puis installer

Commencer avec `replicaCount: 0`, `pilot.enabled: false`,
`controlPlane.launch.enabled: false` et l’observabilité désactivée. La lecture
S3 du control plane doit viser une clé du site réellement autorisée ; une clé
absente s’annonce comme preuve absente. La chart ne crée pas les ressources AWS
ou Snowflake. Depuis le clone, avec le fichier privé déjà complété :

```sh
helm lint chart -f /chemin/prive/site.yaml --namespace quadringent-demo
helm template quadringent chart -f /chemin/prive/site.yaml --namespace quadringent-demo > /chemin/prive/rendered.yaml
# Après revue du rendu et autorisation du site :
helm upgrade --install quadringent chart -f /chemin/prive/site.yaml --namespace quadringent-demo
kubectl -n quadringent-demo rollout status deployment/quadringent-quadringent-control-plane
kubectl -n quadringent-demo port-forward deployment/quadringent-quadringent-control-plane 8844:8844
```

Adapter le namespace aux values. Le tunnel expose seulement le loopback local.
Ouvrir <http://127.0.0.1:8844> puis vérifier `/healthz`, `/v1/version` et
`/v1/overview`. **Un pod Ready ne prouve pas une réplication.**

Pour enregistrer des liaisons, le CLI exige `--state-dir` ou `--fleet-state-dir`.
Dans la chart actuelle, ce stockage est raccordé par le mode flotte avec PVC ;
en mode lecture seule sans stockage, `/v1/connections` est absent. Cela ne vaut
pas autorisation d’activer une flotte : le pilote local du README permet de
valider le formulaire indépendamment du cluster.

### Référence des valeurs

Toutes les clés de `chart/values.yaml` sont décrites et validées par
`chart/values.schema.json` (types, enums, motifs de digest, bornes de
tuning, `additionalProperties: false`). Helm applique ce schéma
automatiquement à `helm lint` et `helm template` : une clé mal orthographiée,
un tag mouvant à la place d'un digest `sha256:...`, ou une valeur hors
énumération sont refusés avant même le rendu des manifestes, avec un message
du type :

```
Error: values don't meet the specifications of the schema(s) in the following chart(s):
quadringent:
- at '/image/digest': 'latest' does not match pattern '^(sha256:[a-f0-9]{64})?$'
```

Ce message précise le chemin JSON (`/image/digest`) et la règle violée.
Certaines valeurs restent en plus validées par les gardes du template
(`fail` avec message dédié en français, ex. `site.namespace est
obligatoire`) : elles s'appliquent après le schéma, pour les règles qui
dépendent d'autres valeurs (cohérence entre champs) ou qui doivent guider
plus précisément le site.

Une table de référence lisible (nom, type, obligatoire, description) peut
être générée depuis le schéma avec `scripts/generate_chart_values_doc.py` ;
voir `docs/product/chart-values.md`.

## 3bis. Découverte des tables (écran « Tables »)

`POST /v2/sources/{id}/tables/refresh` interroge le catalogue IBM i
(`QSYS2.SYSTABLES`, `SYSTABLESTAT`, `JOURNALED_OBJECTS`, `SYSKEYCST`) et
classe chaque fichier physique trouvé :

- **`ready`** — journalisée, images `*BOTH`, clé primaire ou index unique
  trouvé. Rien à faire côté IBM i.
- **`not_journaled`** — aucun journal actif sur le fichier. Deux cas : si la
  bibliothèque a déjà un journal utilisable, seule `STRJRNPF` est proposée ;
  sinon, `CRTJRNRCV` puis `CRTJRN` créent d'abord le récepteur et le journal.
  Exemple (bibliothèque synthétique `SALES`, table `ORDHDR`) :

  ```
  CRTJRNRCV JRNRCV(SALES/QSQJRN0001) THRESHOLD(*NONE) TEXT('Récepteur de journal Quadringent')
  CRTJRN JRN(SALES/QSQJRN) JRNRCV(SALES/QSQJRN0001) MNGRCV(*SYSTEM) TEXT('Journal Quadringent')
  STRJRNPF FILE(SALES/ORDHDR) JRN(SALES/QSQJRN) IMAGES(*BOTH) OMTJRNE(*OPNCLO)
  ```

- **`images_incomplete`** — journalisée mais en images `*AFTER` seules : sans
  l'image avant, les suppressions et mises à jour ne peuvent pas être
  répliquées correctement.

  ```
  CHGJRNOBJ OBJ((SALES/ORDHDR *FILE)) ATR(*IMAGES) IMAGES(*BOTH)
  ```

- **`no_key`** — journalisée en `*BOTH` mais sans clé primaire ni index
  unique. Aucune commande CL n'est proposée : la table peut être répliquée
  par sa position physique (RRN), mais **une réorganisation de fichier
  (`RGZPFM`, `CLRPFM`) exigera une resynchronisation** — même avertissement
  que pour la sélection sans clé de l'assistant v1 (`onboarding.py`). Le
  choix RRN doit être acquitté explicitement (`PATCH /v2/tables/{id}` avec
  `acknowledge_rrn: true`).

Ces commandes sont à exécuter par un administrateur IBM i habilité (l'API ne
les exécute jamais). Sources : IBM i Knowledge Center, référence des
commandes CL — `CRTJRNRCV`, `CRTJRN`, `STRJRNPF`, `CHGJRNOBJ` (voir
`src/quadringent/table_discovery.py` pour les liens exacts).

Si les tables sélectionnées appartiennent à des journaux IBM i différents,
l'écran signale un **mésappariement de journal** : ce n'est pas bloquant,
mais chaque journal nécessite son propre lecteur (débit et erreurs séparés).

## 4. Déclarer puis mettre en service

Dans Installation, renseigner la source et la référence Secret, le journal, les
tables et la destination. La réponse `201` de `/v1/connections` signifie
`declared_not_in_service`. La validation des champs ne teste pas la connectivité.

La mise en service nécessite un catalogue et un sidecar relevés sur le site,
un modèle de Job compatible, un checkpoint et une continuité prouvés. Activer
`controlPlane.launch.enabled` uniquement après validation de ces prérequis, avec :

```sh
helm template quadringent chart -f /chemin/prive/site.yaml --namespace quadringent-demo \
  --set controlPlane.launch.enabled=true \
  --set-file controlPlane.launch.jobTemplate=/chemin/prive/job-template.json \
  --set-file controlPlane.launch.fleetCatalog=/chemin/prive/fleet-catalog.json \
  --set-file controlPlane.launch.fleetSidecar=/chemin/prive/fleet-sidecar.json
```

Ce rendu est à revoir avant toute application. Conserver le PVC d’état. Un seul
lecteur de journal doit être actif ; ne pas activer simultanément un Deployment
permanent et le pilote de flotte. Un pré-vol (`preflight.enabled`) peut effectuer
des écritures S3 de sonde : c’est une opération de site, pas un test local neutre.

## 5. Prouver le résultat

Pour une fenêtre bornée et un périmètre de tables explicite, conserver :

1. source et catalogue accessibles, TLS validé, journal et receivers continus ;
2. positions de début/fin et checkpoint durable, absence de trou ;
3. publication S3 relue et identités du batch conformes ;
4. chargement Snowflake observé puis rapprochement clés, opérations et valeurs ;
5. fraîcheur des preuves et comportement sur insert/update/delete, puis reprise
   au checkpoint après un arrêt contrôlé autorisé.

Un `COUNT(*)`, un lag nul, un succès HTTP ou un bouton vert ne suffit pas.
L’écran doit conserver les absences de mesures et les erreurs. Activer les coûts
selon [FinOps](../finops.md). Le [runbook](../operations.md) décrit les reprises.
Pour S3 et le cluster, le guide FinOps décrit la collecte séparée et le fichier
local/PVC partagé avec le control plane. Cette option ne déploie aucun Job et
n’accorde aucun accès cloud. Sans collecteur configuré, les coûts restent non mesurés.

## Désinstallation et limites

Arrêter les lecteurs et vérifier l’absence de Job actif avant de retirer la
release. Les Jobs créés par le runtime ne sont pas nécessairement possédés par
Helm. Le retrait de la release peut retirer son PVC selon le cluster : sauvegarder
l’état, les intentions et l’audit avant toute suppression approuvée.

S3, versions d’objets, DynamoDB, Snowflake, notifications, IAM et secrets du site
subsistent. Leur suppression est une décision séparée du propriétaire des données.
Ce guide n’atteste pas une installation cloud neuve ; consigner précisément le
premier prérequis absent et la preuve obtenue à chaque étape.
