# Quadringent — SLO et alertes DEV

Ce runbook évalue une preuve Quadringent déjà produite. Il ne démarre aucune
capture, ne charge aucun fichier et ne modifie ni AWS ni Snowflake. Les cibles
sont fermées dans le code à `example-corp-dev`, `eu-west-3`, au bucket R&D, au pipe, au
canonical et au warehouse Quadringent de `DEV_RAW.AS400_RD`.

## Contrat

La politique versionnée est `infra-values/slo-policy-dev.json`. Elle couvre :

| Étape | Mesure | Seuil DEV initial |
|---|---|---:|
| Capture | âge du snapshot | 120 s |
| Capture | état du run | `RUNNING` ou arrêt borné `STOPPED_BUDGET` |
| Capture | erreurs | 0 |
| Checkpoint | lag sur un même receiver | 1 000 séquences |
| S3 | âge du snapshot durable | 180 s |
| Coût S3 | `AllRequests` du bucket sur 24 h | 500 000 |
| Snowpipe | fichiers pending | 10 |
| Canonical | âge de la preuve destination | 300 s |
| Livraison | p95 / p99 source→canonical sur la fenêtre du run | 60 s / 180 s |
| Intégrité | pertes, extras, doublons, mutations en erreur | 0 |
| Coût Snowflake | crédits du warehouse Quadringent sur 24 h | 1 crédit |

Ces seuils sont un contrat DEV initial, pas une promesse commerciale acquise.
Ils doivent être recalibrés avec la distribution du soak long. Une mesure
absente, illisible, hors fenêtre ou entre deux receivers sans chaîne explicite
est `unobserved`, jamais `pass`.

`AllRequests` est l'enveloppe conservative du bucket R&D complet, car la
métrique CloudWatch S3 disponible n'est pas ventilée par préfixe. Elle protège
contre une dérive de coût, mais ne permet pas d'attribuer chaque requête à CDC
Forge. L'absence de datapoint est interprétée comme `unobserved`, jamais comme
zéro.

## Collecter les métriques attribuées

La preuve passée à `--proof` fixe la fenêtre de latence exacte entre
`run.started_at` et `destination_proof.observed_at`. Le collecteur refuse une
fenêtre inversée, future ou supérieure à 25 heures.

Pour attribuer la latence et le dernier objet S3 à un run fermé, ajouter
`--run-id <id-du-run>` à la commande ci-dessous. Le collecteur relit les lots
et manifestes du préfixe réservé, vérifie leur intégrité et compare le nombre
d'événements et l'empreinte des identifiants à `stored_event_identity_proof`.
Un run différent de `flux.id`, une corruption ou un écart d'identité bloque
la collecte avant la connexion Snowflake. Les clés exactes et la population
attendue sont transmises aux mesures, sans modifier les dates de la preuve.

Ce mode accepte une archive terminée pour l'observabilité, jamais comme un
nouveau canary. Le vérificateur live conserve sa limite de cinq minutes.
Sans `--run-id`, la collecte reste une observation de fenêtre temporelle et
de préfixe global : ne pas la présenter comme une preuve de population exacte.
La métrique CloudWatch S3 reste dans tous les cas celle du bucket entier.

```bash
PYTHONPATH=src:scripts uv run \
  --with boto3 \
  --with 'snowflake-connector-python>=3.12,<4' \
  python scripts/quadringent_slo_collect.py \
  --proof /tmp/quadringent-autonomy-proof.json \
  --out /tmp/quadringent-slo-telemetry.json
```

La sortie contient uniquement des compteurs, timestamps et percentiles. Les
erreurs de collecte sont réduites à `source` et `error_type`; aucun message
serveur ou secret n'est recopié. Code retour : `0` si les six mesures externes
sont présentes, `2` si au moins une source est non observée, `3` si l'entrée
ou l'exécution est invalide.

## Évaluer les SLO

### Cycle de rafraîchissement sans ingestion

`scripts/quadringent_observability_refresh.py` prépare une observation depuis la
clé de preuve DEV existante. Il vérifie le run archivé et ses identifiants,
conserve tous les champs de livraison, puis compose mesures et historique SLO.
Par défaut, il ne publie rien :

```bash
uv run --with boto3 --with snowflake-connector-python \
  python scripts/quadringent_observability_refresh.py \
  --policy infra-values/slo-policy-dev.json --out /tmp/forge-observation.json
```

Dans l'image du vérificateur, la commande est
`python /app/scripts/quadringent_observability_refresh.py`. Elle accepte
`--aws-default-credentials` et `--snowflake-oidc-token-file` pour les identités
projetées existantes. Aucun accès IBM i, COPY, MERGE ou activation de capture.

Une publication autorisée exige
`--publish-confirm REFRESH_AS400_RD_OBSERVABILITY_DEV`. Les connexions sont
fermées et la sortie locale écrite avant le PUT conditionnel sur l'ETag lu.
Un conflit ne doit pas être contourné : relire la preuve et recalculer.
L'option `--out` doit cibler un volume inscriptible, pas la racine de l'image.

Le résultat distingue `publication_status=not_attempted`, `confirmed` et
`unknown` (erreur pendant le PUT, modification possible). En cas d'issue
incertaine, inspecter l'objet/version avant une nouvelle tentative. Le succès
retourne ETag/version lorsqu'ils sont fournis par S3. Il n'y a pas de boucle
de rejeu applicative ; les tentatives internes du SDK restent possibles.

Le code retour 0 prouve l'exécution du cycle, **pas** des SLO conformes : lire
`slo_status` et la fraîcheur de la preuve avant tout gate de canary/soak.
Le code 3 signale une erreur d'exécution et précise l'état de publication.
Ce script seul n'installe aucun scheduler et n'envoie aucune notification.

### Contrat temporel de l'évaluation

Le rapport porte l'heure `collected_at` des métriques, pas celle du lancement
de l'évaluateur. À preuve et politique identiques, rejouer une collecte ne
rajeunit ni le rapport ni le cycle d'alerte. Le cockpit détermine ensuite la
fraîcheur depuis ce timestamp, avec son horloge courante. Le statut `pass` d'un
rapport historique ne constitue pas une autorisation de démarrer un nouveau run.

Une collecte datée dans le futur, sans fuseau ou avec une date invalide est
rejetée. Si `collected_at` manque, les six mesures externes sont considérées
inconnues, même si des valeurs figurent dans le fichier. Une preuve plus récente
que la collecte ne peut pas être certifiée par celle-ci. Modifier la preuve ou
la politique à timestamp de collecte inchangé peut produire un conflit de rapport
dans le gestionnaire d'alertes : refaire une collecte, ne pas modifier sa date.

```bash
PYTHONPATH=src:scripts python3 scripts/quadringent_slo_check.py \
  --proof /tmp/quadringent-autonomy-proof.json \
  --telemetry /tmp/quadringent-slo-telemetry.json \
  --policy infra-values/slo-policy-dev.json \
  > /tmp/quadringent-slo-report.json
```

Codes retour :

- `0` : tous les checks passent ;
- `1` : au moins un seuil est dépassé ;
- `2` : aucun dépassement observé, mais une mesure obligatoire manque ;
- `3` : preuve, télémétrie ou politique invalide.

Le tableau `alerts` décrit l'observation instantanée. Il n'est pas, à lui seul,
une notification fiable : il ne sait ni dédupliquer un signal répété, ni
conserver l'instant du premier déclenchement, ni produire une résolution.

## Projeter le cycle de vie des alertes

Le projecteur local transforme le rapport SLO en un document atomique
`quadringent-alert-batch-v1`. Le même fichier porte le dernier batch d'événements
et l'état persistant `quadringent-alert-state-v1`; il peut donc être réutilisé au
run suivant sans base externe :

```bash
PYTHONPATH=src:scripts python3 scripts/quadringent_slo_alerts.py \
  --report /tmp/quadringent-slo-report.json \
  --state /tmp/quadringent-alert-state.json
```

Le contrat garantit :

- une empreinte SHA-256 stable par pipeline DEV et par check ;
- un digest d'intégrité de l'état complet, vérifié avant toute transition ;
- une seule transition `opened`, puis aucune notification pour un signal
  identique répété ;
- une transition `changed` si le statut, la sévérité, la raison, le seuil ou
  l'unité changent ;
- une transition `resolved` uniquement après un `pass` explicite du même
  check ; l'omission d'un ancien check ne le résout jamais ;
- une transition `reopened` et un compteur d'occurrences après une résolution ;
- le rejet d'un rapport ancien, contradictoire ou incohérent avec ses checks ;
- le rejet d'un état précédent altéré ;
- une écriture atomique qui préserve le dernier état valide en cas d'échec.

Codes retour :

- `0` : aucun signal actif ;
- `1` : au moins un signal critique actif (`breach`) ;
- `2` : uniquement des signaux d'observabilité incomplets actifs
  (`unobserved`) ;
- `3` : rapport ou état invalide, sans écrasement du dernier état valide.

La sortie standard ne contient que le statut et les nombres de signaux ; une
erreur est réduite à son type. Aucun message de backend ou contenu de preuve
n'est recopié.

Le digest d'état détecte une corruption ou une modification accidentelle. Ce
n'est pas une signature cryptographique et il ne remplace pas les permissions
du futur stockage persistant.

## Raccorder la preuve au cockpit

Le cockpit ne lit pas le rapport SLO ni l'état d'alerte séparément. Ils sont
scellés dans une copie de la même preuve autonome avec un contrat
`quadringent-observability-v1` :

```bash
PYTHONPATH=src:scripts python3 scripts/quadringent_observability_snapshot.py \
  --proof /tmp/quadringent-autonomy-proof.json \
  --report /tmp/quadringent-slo-report.json \
  --alerts /tmp/quadringent-alert-state.json \
  --out /tmp/quadringent-autonomy-observed.json
```

La composition est atomique et refuse les documents dont le pipeline,
l'environnement, le timestamp ou le digest ne correspondent pas. Le control
plane projette ensuite ce contrat dans les mêmes réponses REST et révisions SSE
que le verdict de livraison. Une observabilité absente ou invalide devient
explicitement indisponible ; elle ne rend jamais la livraison saine.

Dans le cockpit :

- **Incidents** expose le signal, sa première occurrence, sa dernière
  observation et sa résolution ;
- **Télémétrie** expose les douze contrôles, les valeurs observées et leurs
  seuils derrière un détail progressif ;
- une preuve simulée, historique, périmée ou partielle reste une observation
  conservée et ne devient jamais une action opérateur courante ;
- une alerte résolue conserve les valeurs et le seuil de sa résolution, même si
  la politique courante évolue ensuite.

Ce raccord prouve le contrat backend vers navigateur sur une source locale. Il
ne remplace ni la preuve live IBM i vers Snowflake, ni un scheduler, ni un canal
de notification déployé.

Ce projecteur rend le contrat de notification intégrable, mais **aucun
scheduler et aucun canal de notification ne sont déployés par ce lot**. Il est
donc toujours interdit d'annoncer que l'alerting opérationnel de bout en bout
est actif. La seule alerte déjà vérifiée live reste l'alarme CloudWatch DEV
`example-corp-raw-s3-request-spike`, sur `AllRequests > 500000/jour`.

## Gate du canary et du soak

Un canary ou un soak ne passe que si :

1. la preuve autonome elle-même passe sa réconciliation ;
2. le collecteur retourne `0` ;
3. l'évaluateur retourne `0` ;
4. le projecteur retourne `0`, sans alerte active ;
5. la capture revient ensuite à zéro ;
6. Popsink n'a subi aucune mutation par Quadringent.

Une ancienne preuve CLI qui ne correspond pas au canonical autonome produit
une latence `unobserved`. C'est le comportement attendu : la présence de lignes
dans une autre table ne certifie pas le pipe courant.

Une comparaison de débits exige des charges, des fenêtres et des preuves
équivalentes ; aucun benchmark client n’est distribué avec le produit.
