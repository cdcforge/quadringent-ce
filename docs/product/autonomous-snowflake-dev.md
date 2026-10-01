# Destination Snowflake autonome — DEV uniquement

Ce runbook décrit le chargement autonome sur une infrastructure non productive.
Les identités ci-dessous sont synthétiques et doivent être adaptées au site. Il ne change ni le lecteur IBM i ni son checkpoint : la capture
continue d'écrire le raw S3 avant d'avancer DynamoDB. Snowflake consomme ensuite
les nouveaux fichiers de façon indépendante.

## Périmètre et objets

- environnement : DEV/INT uniquement ;
- bucket : `example-corp-000000000000-int-example-corp-raw` ;
- préfixe : `as400/sales/sale/` avec suffixe `.jsonl` ;
- schéma : `DEV_RAW.AS400_RD` ;
- stage existant : `AS400_RD_SALE_EXTERNAL_STAGE` ;
- raw autonome : `QUADRINGENT_SALE_RAW` ;
- pipe : `QUADRINGENT_SALE_PIPE` ;
- canonical technique : vue ordinaire `QUADRINGENT_SALE_CANONICAL` ;
- compute de vérification à la demande : `QUADRINGENT_DEV_WH`, X-Small,
  mono-cluster, auto-suspend 60 secondes.

Les schémas tiers, leurs objets Kubernetes, leurs secrets et leurs warehouses
restent hors du périmètre déclaré. Aucun objet PROD n'est accepté par le
plan Python.

## Architecture

```text
IBM i SALE -> capture Quadringent -> raw S3 immuable
                                  |
                                  v
                         notification S3 bornée
                                  |
                                  v
                    Snowpipe -> raw DEV dédié
                                  |
                                  v
                    vue canonical sans refresh
```

Snowpipe utilise le stage et sa storage integration existants. Il n'exige donc
aucune clé Snowflake dans le pod Kubernetes. Le canonical garde une ligne par
`event_id` déterministe avec `QUALIFY ROW_NUMBER()`, en privilégiant la plus
grande séquence journal.

## Préflight

1. `origin/main` doit contenir le cockpit, le loader autonome et la migration
   canonical courants ; travailler dans un worktree propre.
2. Le Deployment `quadringent-quadringent` doit être à `0/0` avant le pilote.
3. Le stage doit pointer exactement vers le préfixe SALE dédié.
4. La configuration de notifications du bucket ne doit contenir aucun filtre
   `ObjectCreated` chevauchant `as400/sales/sale/*.jsonl`.
5. Aucun accès Popsink n'est nécessaire : la capture reste dans son namespace,
   son compte de service et son préfixe DEV dédiés.

Dry-run Snowflake :

```bash
PYTHONPATH=src python3 scripts/quadringent_snowpipe_setup.py
```

Dry-run S3, après obtention du channel SQS Snowpipe :

```bash
PYTHONPATH=src python3 scripts/quadringent_s3_snowpipe_setup.py \
  --notification-channel '<arn-sqs-snowpipe>' \
  --profile example-corp-dev
```

Ce dry-run est hermétique : il affiche la cible et la confirmation requise,
mais n'inspecte pas AWS. L'exécution relit la configuration live, refuse tout
chevauchement, puis applique le document complet seulement après confirmation.

## Installation contrôlée

La première commande crée uniquement les quatre objets DEV dédiés et retourne
le `notification_channel` géré par Snowflake :

```bash
PYTHONPATH=src uv run --with 'snowflake-connector-python>=3.12,<4' python \
  scripts/quadringent_snowpipe_setup.py \
  --connection-name example-corp \
  --execute --confirm AS400_RD_AUTONOMOUS_LOAD_DEV
```

La seconde préserve toute notification S3 existante et ajoute un filtre exact
sur le préfixe SALE JSONL :

```bash
PYTHONPATH=src uv run --with boto3 python \
  scripts/quadringent_s3_snowpipe_setup.py \
  --notification-channel '<arn-sqs-snowpipe>' \
  --profile example-corp-dev \
  --execute --confirm AS400_RD_SNOWPIPE_S3_DEV
```

## Preuve d'autonomie

Une installation n'est pas une preuve runtime. Le pilote doit ensuite :

1. relever T0 et l'état des seuls workloads Quadringent ; aucun accès Popsink
   n'est autorisé dans le périmètre courant, donc ne pas revendiquer une
   stabilité de ses UID qui n'a pas été observée ;
2. lancer un seul Job capture de 10 minutes, Deployment permanent à zéro ;
3. conserver le snapshot final et la liste exacte des clés JSONL créées entre
   T0 et la fin du run ;
4. attendre que `SYSTEM$PIPE_STATUS` soit `RUNNING`, sans fichier pending, puis
   interroger la vue canonical avec le warehouse dédié ;
5. exécuter seulement le vérificateur read-only :

```bash
PYTHONPATH=src uv run --with 'snowflake-connector-python>=3.12,<4' python \
  scripts/quadringent_autonomous_verify.py \
  --connection-name example-corp \
  --capture-snapshot /tmp/quadringent-capture.json \
  --object-keys-file /tmp/quadringent-object-keys.txt \
  --run-tag AUTONOMY20260901 \
  --proof-output /tmp/quadringent-autonomy-proof.json \
  --proof-s3-uri s3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/proofs/quadringent-autonomous-latest.json \
  --aws-profile example-corp-dev \
  --publish-confirm PUBLISH_AS400_RD_AUTONOMOUS_PROOF_DEV
```

La clé `quadringent-autonomous-latest.json` est l'unique source live du cockpit.
Elle est distincte des `.jsonl` consommés par Snowpipe, porte `no-store` et le
versioning du bucket conserve ses générations précédentes. Le publisher refuse
tout autre bucket, préfixe ou nom de fichier.

### Vérification depuis un workload

Le vérificateur conserve `--aws-profile example-corp-dev` par défaut pour l'opérateur
local. L'option explicite `--aws-default-credentials` utilise la chaîne
d'identifiants standard du SDK sans imposer de profil local ; elle est
incompatible avec `--aws-profile`. Avant toute publication S3, STS doit
confirmer le compte DEV `000000000000`. Une identité absente, inaccessible
ou appartenant à un autre compte fait échouer la commande sans publication.

Cette option ne crée pas une identité et ne garantit pas que le SDK utilise
IRSA : les variables et fichiers de configuration présents restent soumis
à l'ordre de résolution de la
[chaîne Boto3](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html).
Le manifeste du futur workload devra donc exclure les profils locaux et
credentials statiques, puis faire vérifier le rôle réellement utilisé.

Ce support AWS ne suffit pas à rendre la supervision autonome : l'identité
Snowflake doit aussi être dédiée et validée dans le workload (mode OIDC ci-dessous).
Il reste également à acquérir les fenêtres de capture, produire les preuves
d'identités et SLO, publier les erreurs et superviser les essais indépendamment
du Mac. Aucun Job de vérification périodique n'est installé par cette option.

Validation locale du 8 septembre 2026 : 42 tests et 18 sous-tests passent
sur le vérificateur, la preuve destination, la télémétrie et le collecteur
SLO. Le test d'import isolé couvre aussi la conservation des imports entre
scripts lorsque le vérificateur est utilisé comme module. Ces contrôles sont
hermétiques et ne certifient pas une identité AWS ou Snowflake en cluster.

### Authentification Snowflake du vérificateur en Pod

Le mode local reste `--connection-name example-corp` par défaut. Le mode workload est
explicite et mutuellement exclusif avec ce profil :

```bash
PYTHONPATH=src python scripts/quadringent_autonomous_verify.py \
  --capture-snapshot /work/capture.json \
  --object-keys-file /work/object-keys.txt \
  --run-tag DEV_BOUNDED_RUN \
  --proof-output /work/reconciled.json \
  --snowflake-oidc-token-file /var/run/secrets/snowflake/token
```

Ce chemin utilise le compte `EXAMPLE-EXAMPLE_CORP`, le rôle
`QUADRINGENT_DEV_VERIFIER_ROLE`, le warehouse `QUADRINGENT_DEV_WH` et le périmètre
`DEV_RAW.AS400_RD`. Le connecteur lit le fichier projeté ; le CLI ne reçoit
pas la valeur du jeton. Un chemin relatif ou une combinaison des deux modes
est rejeté. Une erreur OIDC ne déclenche jamais de repli sur `example-corp`.

Le fichier doit être projeté par le service account `quadringent-verifier` du
namespace `quadringent-demo`, audience `snowflakecomputing.com`. Prévoir un
connecteur compatible WIF (le smoke runtime a utilisé 4.7.3). L'image dédiée
est construite avec `docker build -f docker/verifier.Dockerfile .` ; elle
contient Python 3.14 et les dépendances verrouillées, sans Java. L'image de
capture reste séparée. Pour renouveler le lock, utiliser la commande UV
inscrite en tête de `docker/verifier-requirements.txt`, puis retester l'image.

L'exemple n'effectue aucune publication S3. Pour publier, le Job doit aussi
avoir une identité AWS DEV autorisée, utiliser `--aws-default-credentials`
et les options de confirmation de publication déjà décrites. Ces permissions
AWS ne découlent pas de l'identité Snowflake.

La qualification de l’identité, du chargement et des fenêtres doit être
rejouée sur le site cible. Un relevé historique ne certifie pas une fenêtre fraîche.

### Acquisition d'un run isolé (code, non déployé)

Le vérificateur accepte désormais `--run-id <identifiant>` à la place du
couple `--capture-snapshot` / `--object-keys-file`. Ces modes ne peuvent pas
être mélangés. Le run doit disposer d'un préfixe exclusif :

```text
as400/sales/sale/runs/<identifiant>/
  console-snapshot.json
  batch-<identité>.jsonl
  batch-<identité>.manifest.json
```

Avant une future capture, le superviseur devra réserver un identifiant neuf,
configurer `AS400_RAW_PREFIX` sur ce préfixe et configurer le snapshot au même
endroit. Ne pas réutiliser un préfixe après un run échoué : le collecteur ne
prouve pas à lui seul l'exclusivité d'un identifiant réutilisé. Aucun essai
existant n'a été déplacé vers ce nouveau format.

La collecte utilise le bucket DEV fixe, jamais les dates d'upload pour choisir
des fichiers. Elle exige un snapshot STOPPED_BUDGET, sans erreur, connu et âgé
de 300 secondes maximum. Elle valide l'ensemble payload/manifeste, l'intégrité
des lots par le lecteur raw existant, les identités DEV SALE, les comptes et
l'absence de doublon entre lots, puis relit le snapshot pour détecter une
modification concurrente. Elle renvoie aussi une empreinte des identifiants.
Cette empreinte décrit les objets stockés, **pas un oracle source indépendant**.

En mode `--run-id`, le CLI transmet obligatoirement cette empreinte au contrôle
Snowflake : identifiants RAW distincts et identifiants canoniques sont lus
triés, par pages de 1000, sans retourner de valeurs métier dans les logs.
Une substitution d'identité, un doublon, un nombre changé ou une empreinte
différente fait échouer la vérification avant publication, même à comptes
égaux. La preuve supplémentaire `stored_event_identity_proof` nomme sa base
`verified_s3_batches`. Le mode historique avec fichiers locaux reste un
contrôle de comptes sans ce complément ; il ne revendique pas cette preuve.

Budgets : au plus 1000 lots, 10 pages de listing, 32 MiB par objet et 256 MiB
par fenêtre. Un run plus grand doit être découpé explicitement ; aucun
échantillonnage ni succès partiel n'est produit.

Le collecteur de coûts différés exige la vue et le grant décrits dans
`infra-values/snowflake-dev-verifier-metering.sql`, puis une image contenant le
collecteur. Voir [FinOps](../finops.md). La présence d’une politique IAM ne
prouve pas son application ni une mesure effective.

Validation locale : 76 tests sur acquisition, vérificateur, preuves, SLO et
stockage raw passent. Revue indépendante sans défaut bloquant ; les angles
morts relevés (pagination, budget global d'octets, timestamps mal formés)
ont reçu des tests de refus supplémentaires, exécutés avec succès.

### Lecture par le control plane

Le control plane consomme la preuve en lecture seule :

```bash
PYTHONPATH=src python3 scripts/quadringent_control_plane.py \
  --source live:dev-sale:s3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/proofs/quadringent-autonomous-latest.json \
  --environment dev \
  --ui-dist ui/dist
```

Le verdict `AUTONOMIE PROUVÉE` exige, pour la même fenêtre :

- capture terminée sans erreur et lag borné ;
- checkpoint strictement croissant, y compris après restart ;
- toutes les clés de la fenêtre chargées automatiquement ;
- `captured = raw distinct = canonical`, doublon zéro ;
- un événement SALE post-restart présent dans le canonical ;
- aucune commande de load entre le début et la fin du run ;
- aucun accès ni changement Popsink par Quadringent ;
- retour à `replicaCount=0`, `pilot.enabled=false` après la preuve.

Après publication, collecter puis évaluer les SLO avec
[`slo-alerting-dev.md`](slo-alerting-dev.md). Une preuve réconciliée dont les
métriques sont absentes ou hors seuil ne peut pas passer le gate du soak.

La validation C/U/D et des scénarios de reprise suit le gate fail-closed
[`sale-mutation-certification.md`](sale-mutation-certification.md). Un contrat
synthétique vert ne vaut jamais mutation IBM i live.

## Rollback non destructif

Le rollback arrête le flux et le compute, mais conserve S3, DynamoDB et les
tables Snowflake.

1. Ramener la capture à `replicaCount=0` et `pilot.enabled=false`.
2. Retirer uniquement la notification S3 Quadringent :

```bash
PYTHONPATH=src uv run --with boto3 python \
  scripts/quadringent_s3_snowpipe_setup.py \
  --rollback --profile example-corp-dev \
  --execute --confirm ROLLBACK_AS400_RD_SNOWPIPE_S3_DEV
```

3. Pauser le pipe et le warehouse ; la vue canonical n'a aucun compute à
   suspendre :

```bash
PYTHONPATH=src uv run --with 'snowflake-connector-python>=3.12,<4' python \
  scripts/quadringent_snowpipe_setup.py \
  --rollback --connection-name example-corp \
  --execute --confirm ROLLBACK_AS400_RD_AUTONOMOUS_LOAD_DEV
```

Ne jamais supprimer le raw, le checkpoint ou les tables pour effectuer un
rollback opérationnel.
