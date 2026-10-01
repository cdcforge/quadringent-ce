# Coûts : relevés, attribution et limites

Quadringent affiche des crédits mesurés sur une fenêtre de 24 h, arrêtée au moins
6 h avant le relevé pour tenir compte du délai de métering. La fenêtre et son âge
restent visibles. Cela décrit **le warehouse entier**, pas l’imputation exclusive
à une liaison, ni la facture Snowflake totale. Ne pas additionner plusieurs
liaisons partageant le même warehouse.

## Chaîne de permissions

Le job `verifier` lit une vue sécurisée dédiée, filtrée sur un seul warehouse.
Le control plane lit le document de preuve S3 produit par ce job. Il n’accède ni
à `ACCOUNT_USAGE`, ni à AWS Cost Explorer. Aucun nouveau droit de facturation
n’est requis. Le propriétaire de la vue doit disposer des droits Snowflake
nécessaires à sa définition ; le verifier reçoit seulement l’usage de ses parents
et `SELECT` sur cette vue.

Adapter `infra-values/snowflake-dev-verifier-metering.sql` au site avant revue
par son propriétaire. Le nom attendu est
`<destination_database>.<destination_schema>.<destination_prefix>_<ENV>_WH_METERING`,
et le filtre porte sur `<destination_prefix>_<ENV>_WH`. La configuration du site
permet d’imprimer les noms attendus sans secret :

```sh
python -c 'from quadringent.site_config import current; s=current(); print(s.metering_fqn, s.warehouse_name)'
```

La vue publie `START_TIME`, `END_TIME`, `WAREHOUSE_NAME`,
`CREDITS_USED_COMPUTE`, `CREDITS_USED_CLOUD_SERVICES`, `CREDITS_USED`.
L’absence de lignes, une valeur absente ou une erreur de permissions reste
« Non mesuré ». Un zéro réellement relevé reste zéro.

## Prix du contrat

Déclarer les deux champs, ou aucun. Valeur décimale non négative, au plus huit
chiffres avant et six après le point ; devise en trois lettres majuscules.
Il n’existe aucun tarif public de repli.

```yaml
site:
  snowflakeCreditPrice: "3.10"  # exemple seulement : remplacer par le contrat du site
  costCurrency: EUR
```

Le ConfigMap injecte `QUADRINGENT_SNOWFLAKE_CREDIT_PRICE` et
`QUADRINGENT_COST_CURRENCY`. Pour le CLI, fournir ces mêmes variables.
Le montant calculé est `crédits relevés × prix déclaré`, en décimal. Il exclut
remises, taxes et régularisations de facturation. Un prix explicitement nul
est distinct d’un prix absent.

Vérifier que `/v1/overview` porte le check `snowflake_credits`, son unité
`warehousecredits/delayed24h`, sa fenêtre `metering_window_<début>_<fin>_<lignes>`
et une provenance live fraîche. `pipeline.costs` expose `warehouse`,
`price_per_credit`, `currency`, `amount`. Un montant n’est fourni que pour
le site correspondant et une observation admissible. Les observations anciennes,
historiques ou simulées ne portent pas un montant actuel.

Sans prix : crédits visibles et « Prix non déclaré ». Sans mesure : « Non mesuré ».
Aucun coût d’une campagne passée n’entre dans ce calcul.

## S3 Standard et cluster partagé

Le collecteur séparé `quadringent-cost-collect`, installé par `pip install .`,
lit deux sources. Il ne liste ni ne télécharge les objets métier et ne modifie
aucune ressource cloud.

| Source | Périmètre | Montant affiché |
|---|---|---|
| CloudWatch `AWS/S3`, `BucketSizeBytes`, `StandardStorage`, moyenne quotidienne | Tout le bucket déclaré du site | Volume × tarif public régional relevé : estimation par mois à volume constant |
| OpenCost `/allocation`, regroupé par cluster et namespace, `shareIdle=false` | Namespace Quadringent dans le cluster déclaré | Allocation sur le dernier jour UTC complet |
| OpenCost `/assets` sur la même fenêtre | Actifs du cluster : nœuds, volumes et gestion recensés | Total en contexte, distinct du coût attribué à Quadringent |
| Allocation `__idle__` du même cluster | Ressources inutilisées | Contexte inclus dans les actifs, sans répartition au namespace |

Le tarif S3 vient du [catalogue public AWS régional](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/eu-west-3/index.json),
avec la région déclarée à la place de `eu-west-3`. Il est récupéré à chaque
collecte, sans valeur de repli. Le calcul utilise les octets / 2³⁰, soit les Gio
correspondant à l’unité de stockage AWS `GB-Mo`. Seule la première tranche
Standard est traitée ; au-delà de sa borne, aucun montant n’est calculé.
Le volume et son horodatage restent affichés. S3 publie ces relevés chaque jour,
avec retard ; une observation de plus de 72 h est refusée. Voir les
[dimensions CloudWatch S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/metrics-dimensions.html).

**Ces calculs ne sont pas une facture AWS.** S3 exclut requêtes, transferts,
autres classes, remises et taxes. Le modèle OpenCost dépend des prix configurés
sur le cluster, y compris d’éventuels tarifs de secours personnalisés ; sa devise
doit être déclarée explicitement, sans conversion implicite. Les ressources
inutilisées et les frais partagés ne sont pas imputés à Quadringent. Le total du
cluster donne ce contexte ; il ne s’ajoute pas à son allocation.

Une absence de namespace, de données ou de tarif n’est jamais zéro. Les deux
sources peuvent échouer séparément. La preuve locale expire après 26 h ; une
fenêtre cluster doit durer exactement 24 h et se terminer depuis moins de 72 h.
Les horodatages futurs, preuves simulées/historiques ou identités différentes
(site, environnement, namespace, bucket, région) sont refusés.
Ne pas additionner des liaisons partageant le même bucket/namespace, ni fusionner
le run-rate S3, la journée du cluster et le métering Snowflake.

## Activer la collecte d’infrastructure

Prérequis : profil ou rôle AWS déjà autorisé à `cloudwatch:GetMetricStatistics`,
accès HTTPS au catalogue public AWS et accès de lecture à l’[API OpenCost](https://opencost.io/docs/integrations/api/).
Le port dépend du service du site (souvent 9003). Utiliser un endpoint privé ou
un port-forward autorisé. Le collecteur refuse redirections et identifiants dans
l’URL. Il n’ajoute aucun rôle IAM/RBAC, aucun scheduler et aucun Cost Explorer.

Charger la configuration **réelle non productive du site** dans un terminal
autorisé ; `examples/local.env` contient uniquement des identités fictives.
Déclarer `QUADRINGENT_COST_NAMESPACE` avec le namespace dédié à Quadringent.
Exemple d’exécution (adapter profil, cluster et devise à la configuration OpenCost) :

```sh
quadringent-cost-collect \
  --site-id "$QUADRINGENT_SITE_ID" --environment "$QUADRINGENT_ENVIRONMENT" \
  --namespace "$QUADRINGENT_COST_NAMESPACE" \
  --bucket "$QUADRINGENT_RAW_BUCKET" --region "$QUADRINGENT_AWS_REGION" \
  --aws-profile example-dev --opencost-url http://127.0.0.1:19003 \
  --cluster-id example-cluster --cluster-currency USD \
  --output "$PWD/.local-state/infrastructure-costs.json"
```

Le profil AWS est facultatif pour les rôles de workload. Les trois options
OpenCost peuvent être omises pour ne collecter que S3. Un code de sortie 1
signale qu’au moins une source demandée n’a pas été mesurée ; la preuve est
quand même écrite avec les états disponibles. Les erreurs publiées restent
génériques. Le fichier est écrit atomiquement en 0600 ; son parent est créé en
0700 s’il n’existe pas.

Ajouter à la commande de démarrage du control plane :

```sh
--infrastructure-costs-source "file://$PWD/.local-state/infrastructure-costs.json"
```

Le control plane ne fait aucun nouvel appel réseau pour ces coûts. Il doit
déjà servir une source live liée au site déclaré, avec
`--environment "$QUADRINGENT_ENVIRONMENT"` ; le relevé ne crée pas de liaison.
Vérifier `pipeline.infrastructure_costs` dans `/v1/overview`, puis les dates et
limites de la section « Stockage et cluster » dans Consommation.

Dans la chart, `QUADRINGENT_COST_NAMESPACE` vient de `site.namespace`. L’option
`controlPlane.infrastructureCostsSource` accepte le fichier
`file:///var/lib/quadringent-fleet/infrastructure-costs.json` (ou le même nom sous
`fleetStateDir` déclaré), uniquement avec lancement de flotte et PVC persistant.
Un collecteur géré par le site doit écrire ce fichier sur le même volume, sous
l’identité propriétaire lisible par le control plane. Le chart ne crée pas ce
collecteur ; l’image du cockpit ne contient pas cette commande autonome.
Organiser une collecte quotidienne dans le dispositif d’exploitation du site
pour rester dans les 26 h de validité. Ne pas écraser d’autre état de flotte.

Une future intégration Cost Explorer demanderait une validation explicite de ses
permissions et de son coût d’API. Elle ne fait pas partie de cette intégration.
