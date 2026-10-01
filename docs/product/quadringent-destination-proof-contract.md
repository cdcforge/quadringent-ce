# Quadringent — contrat de preuve de destination E2E

Statut : contrat d'implémentation proposé, rétrocompatible avec
`as400-console-v1`.

Ce document définit l'entrée optionnelle permettant au control plane de
projeter honnêtement les étapes `load` et `destination` d'un flux
IBM i → raw → Snowflake. Il ne constitue ni une preuve runtime, ni une
activation de Snowflake. En particulier, il ne transforme pas la simulation
locale ou un snapshot historique en observation live lorsque le serveur IBM i
est arrêté.

## 1. Objectif et limites

Le contrat doit répondre sans ambiguïté à quatre questions :

1. jusqu'à quelle position IBM i les événements ont-ils été publiés dans le
   raw ?
2. jusqu'à quelle position ont-ils été chargés dans le ledger Snowflake ?
3. jusqu'à quelle position ont-ils été appliqués à la destination métier ?
4. une réconciliation bornée établit-elle l'absence de trou, de doublon et
   d'événement inattendu entre ces trois positions ?

L'extension est uniquement un **rapport de preuve produit côté serveur**. Elle
ne configure pas un compte Snowflake, ne transporte pas de credentials et ne
déclenche aucune mutation. Les secrets restent résolus par l'adapter backend,
hors du document et hors du navigateur.

La première version ne prétend pas prouver :

- une disponibilité IBM i lorsque la source est arrêtée ;
- une garantie au-delà de la fenêtre de réconciliation explicitement portée ;
- la qualité métier du décodage C/U/D en l'absence d'une réconciliation métier ;
- une livraison PROD à partir d'une preuve DEV, historique ou simulée.

## 2. Compatibilité

`destination_proof` est une propriété **optionnelle** du document existant
`as400-console-v1`. Le `format_version` ne change pas.

Les nouvelles compositions portent `capture_observed_at`, l'horodatage original
du snapshot de capture. Il est conservé lors d'une nouvelle réconciliation.
`generated_at` date la publication du document combiné et les horodatages de
`destination_proof` datent la vérification destination : ils ne rajeunissent pas
la source. Le cockpit et le SLO capture utilisent `capture_observed_at` lorsqu'il
est présent. Une valeur explicite invalide n'autorise aucun repli silencieux.
Les documents historiques sans ce champ conservent le comportement antérieur ;
leur horodatage source indépendant ne peut pas être retrouvé par inférence.

- Si `destination_proof` est absent, `project_console_document()` doit produire
  exactement la projection actuelle : `load` et `destination` restent
  `unknown`, `quality.coverage` reste `partial`, et aucun statut `healthy` E2E
  ne devient possible. Les tests historiques restent inchangés.
- Les compteurs historiques `events_in_target` et `duplicates_in_target`
  restent acceptés et projetés pour compatibilité. Ils ne suffisent jamais, à
  eux seuls ou ensemble, à valider une étape ou à autoriser le vert.
- Lorsque la preuve valide les étapes `load` et `destination` comme `healthy`,
  la projection utilise `reconciliation.ledger_event_count` et
  `reconciliation.duplicate_event_count` pour ces deux compteurs. Leur portée
  est uniquement la fenêtre réconciliée, jamais le total de la table métier.
  Une preuve périmée, partielle ou contradictoire ne remplit pas les compteurs
  inconnus de la capture et ne prolonge pas leur fraîcheur.
- Si l'extension est présente, elle doit être validée intégralement. Une forme
  invalide lève un `ProjectionError` stable avec le code public
  `invalid_destination_proof`. Le message ne reflète aucune valeur reçue.
- Une extension syntaxiquement valide mais incomplète est projetée
  `unknown` ou `degraded` selon la table de décision ci-dessous ; elle ne doit
  jamais être complétée par inférence.
- Les champs inconnus de l'extension ne sont jamais projetés. La première
  implémentation doit les refuser (`additionalProperties: false`) afin qu'un
  secret ou un payload ajouté par erreur ne traverse pas la frontière.

Cette stratégie est compatible avec le comportement du dépôt : une erreur de
projection est traitée comme un échec de refresh par `ProjectionRepository` ;
une projection précédente est alors conservée en `unknown` et `stale`, sans
réfléter le document fautif.

## 3. Forme normative de l'entrée

Exemple complet. Les valeurs sont fictives et ne sont pas une preuve du
runtime actuel.

```json
{
  "format_version": "as400-console-v1",
  "generated_at": "2026-08-31T08:04:30Z",
  "flux": { "id": "pays" },
  "run": { "state": "RUNNING", "last_error": null },
  "position": {
    "checkpoint": { "receiver": "DEMOJRN3776", "sequence": 42 },
    "source_tail": { "receiver": "DEMOJRN3776", "sequence": 42 }
  },
  "lag": {
    "current": { "value": 0 },
    "verdict": { "value": "STABLE" }
  },
  "counters": { "events_published": { "value": 120 } },
  "destination_proof": {
    "schema_version": "destination-proof-v1",
    "observed_at": "2026-08-31T08:04:28Z",
    "source_checkpoint": {
      "receiver": "DEMOJRN3776",
      "sequence": 42
    },
    "target": {
      "kind": "snowflake",
      "destination_id": "snowflake-dev-rd",
      "environment": "dev"
    },
    "activation": {
      "state": "active",
      "observed_at": "2026-08-31T08:04:25Z",
      "checks": {
        "configuration": "valid",
        "credential": "available",
        "connectivity": "reachable",
        "authorization": "allowed",
        "contract": "compatible"
      },
      "blocker_code": null
    },
    "load": {
      "state": "succeeded",
      "observed_at": "2026-08-31T08:04:26Z",
      "checkpoint": {
        "receiver": "DEMOJRN3776",
        "sequence": 42
      },
      "batch_count": 4,
      "event_count": 120,
      "failed_event_count": 0,
      "incident_code": null
    },
    "destination": {
      "state": "applied",
      "observed_at": "2026-08-31T08:04:27Z",
      "apply_checkpoint": {
        "receiver": "DEMOJRN3776",
        "sequence": 42
      },
      "failed_mutation_count": 0,
      "incident_code": null
    },
    "reconciliation": {
      "state": "matched",
      "observed_at": "2026-08-31T08:04:28Z",
      "window": {
        "from_exclusive": {
          "receiver": "DEMOJRN3776",
          "sequence": 0
        },
        "to_inclusive": {
          "receiver": "DEMOJRN3776",
          "sequence": 42
        }
      },
      "captured_event_count": 120,
      "loaded_event_count": 120,
      "ledger_event_count": 120,
      "distinct_event_count": 120,
      "duplicate_event_count": 0,
      "missing_event_count": 0,
      "unexpected_event_count": 0,
      "failed_mutation_count": 0
    }
  }
}
```

### 3.1 Règles communes

Tous les objets décrits sont fermés : aucune propriété supplémentaire n'est
acceptée.

| Élément | Règle |
|---|---|
| timestamp | chaîne RFC 3339 avec timezone, normalisée en UTC par le projector |
| position | `{receiver, sequence}` uniquement ; `receiver` non vide, 1 à 128 caractères ; `sequence` entier non signé, jamais booléen |
| compteur | entier non signé, jamais booléen, maximum `9_223_372_036_854_775_807` |
| identifiant public | ASCII `[a-z0-9][a-z0-9-]{0,62}`, sans URL, chemin ou nom qualifié |
| valeur absente | propriété omise uniquement lorsqu'elle est déclarée optionnelle ; `null` n'est accepté que pour `blocker_code` et `window.from_exclusive` |

La tolérance d'horloge reste celle du control plane : une observation située
plus d'une minute dans le futur est `clock_untrusted`. Une observation est
`fresh` pendant cinq minutes au maximum. Ces seuils sont appliqués séparément
à `generated_at`, `destination_proof.observed_at`, `activation.observed_at`,
`load.observed_at`, `destination.observed_at` et
`reconciliation.observed_at`.

Tout sous-horodatage doit être inférieur ou égal à
`destination_proof.observed_at` avec la tolérance d'une minute. Le timestamp de
preuve ne peut pas être postérieur à `generated_at` de plus d'une minute. Une
horloge contradictoire rend la preuve `unknown`, jamais `healthy`.

### 3.2 `source_checkpoint`

`source_checkpoint` ancre la preuve de destination à la capture portée par le
même snapshot. Il doit être strictement égal à `position.checkpoint`.

- Une égalité établit seulement que l'adapter parle du même watermark ; elle
  ne prouve pas que Snowflake l'a chargé.
- Une différence est une contradiction de preuve : `load` et `destination`
  deviennent `incident`, avec le code public
  `destination_source_checkpoint_mismatch`.
- Si la position source existante est elle-même `unknown`, aucune étape aval ne
  peut être `healthy`, même si les trois checkpoints aval sont égaux.

L'égalité stricte évite d'inventer un ordre entre deux receivers. Un futur
contrat pourra introduire un ordinal de receiver, mais la v1 traite des
receivers différents comme non comparables et interdit le vert.

### 3.3 `target`

| Champ | Type | Règle |
|---|---|---|
| `kind` | littéral | `snowflake` |
| `destination_id` | identifiant public | alias opaque défini côté control plane |
| `environment` | identifiant public | doit correspondre, sans tenir compte de la casse, à `SourceDescriptor.environment` |

Le document public ne contient ni account locator, host, URL, warehouse,
database, schema, table, stage, bucket, object key, rôle, user, ARN ou référence
de secret. Ces éléments appartiennent à la configuration privée de l'adapter.

Une cible dont l'environnement ne correspond pas à la source est un incident
`destination_environment_mismatch`.

### 3.4 `activation`

`activation` expose l'état nécessaire à un parcours d'activation honnête sans
exposer la configuration sensible.

`state` vaut exactement l'une des valeurs suivantes :

| Valeur | Sens projetable |
|---|---|
| `not_configured` | aucun adapter de destination n'est configuré |
| `validating` | validation backend en cours, conclusion encore inconnue |
| `active` | tous les checks fixes sont positifs |
| `blocked` | un check négatif empêche l'activation |
| `disabled` | adapter explicitement désactivé par configuration |
| `unknown` | l'état d'activation n'a pas pu être observé |

`checks` contient exactement cinq propriétés :

| Check | Valeurs admises | Valeur positive |
|---|---|---|
| `configuration` | `valid`, `missing`, `invalid`, `unknown` | `valid` |
| `credential` | `available`, `missing`, `denied`, `invalid`, `unknown` | `available` |
| `connectivity` | `reachable`, `unreachable`, `unknown` | `reachable` |
| `authorization` | `allowed`, `denied`, `unknown` | `allowed` |
| `contract` | `compatible`, `incompatible`, `unknown` | `compatible` |

`state=active` est valide uniquement si les cinq checks sont positifs.
Réciproquement, un check négatif interdit `active`. Une combinaison
contradictoire rend l'activation `unknown` et ouvre l'incident public
`destination_activation_inconsistent`.

`blocker_code` est `null` sauf pour `blocked`. Pour `blocked`, il est obligatoire
et doit appartenir à cette allowlist :

- `destination_configuration_missing`
- `destination_configuration_invalid`
- `destination_credential_missing`
- `destination_credential_denied`
- `destination_credential_invalid`
- `destination_unreachable`
- `destination_authorization_denied`
- `destination_contract_incompatible`

Les messages d'exception, noms de variables, références de secret et réponses
Snowflake ne sont jamais inclus. Le cockpit dérive le texte opérateur du code
public.

### 3.5 `load`

| Champ | Type | Obligatoire |
|---|---|---|
| `state` | `not_started`, `running`, `succeeded`, `failed`, `unknown`, `planned_stop` | oui |
| `observed_at` | timestamp | oui sauf `not_started` et `unknown`, où il peut être omis |
| `checkpoint` | position | obligatoire pour `running`, `succeeded` et `planned_stop` |
| `batch_count` | compteur | optionnel |
| `event_count` | compteur | obligatoire pour `succeeded` |
| `failed_event_count` | compteur | obligatoire pour `succeeded` et `failed` |
| `incident_code` | allowlist ou `null` | obligatoire et non nul pour `failed`, nul sinon |

Allowlist `incident_code` :

- `destination_load_failed`
- `destination_load_timeout`
- `destination_load_contract_invalid`
- `destination_load_checkpoint_conflict`

Un load `succeeded` n'est `healthy` que si :

- l'activation est `active` ;
- son observation est fraîche et son horloge fiable ;
- `failed_event_count == 0` ;
- son checkpoint est égal à `source_checkpoint` ;
- `event_count == reconciliation.loaded_event_count` ;
- la réconciliation est elle-même `matched` et fraîche.

Un checkpoint de load en retard sur la source, avec le même receiver, est
`degraded` : la livraison est connue mais en retard. Un checkpoint de load en
avance sur la source est un incident `destination_load_ahead_of_source`. Des
receivers différents sont non comparables et produisent `unknown`.

### 3.6 `destination`

| Champ | Type | Obligatoire |
|---|---|---|
| `state` | `not_started`, `applying`, `applied`, `failed`, `unknown`, `planned_stop` | oui |
| `observed_at` | timestamp | oui sauf `not_started` et `unknown`, où il peut être omis |
| `apply_checkpoint` | position | obligatoire pour `applying`, `applied` et `planned_stop` |
| `failed_mutation_count` | compteur | obligatoire pour `applied` et `failed` |
| `incident_code` | allowlist ou `null` | obligatoire et non nul pour `failed`, nul sinon |

Allowlist `incident_code` :

- `destination_apply_failed`
- `destination_apply_timeout`
- `destination_apply_contract_invalid`
- `destination_apply_checkpoint_conflict`

Une destination `applied` n'est `healthy` que si :

- l'activation est `active` ;
- son observation est fraîche et son horloge fiable ;
- `failed_mutation_count == 0` ;
- `apply_checkpoint == load.checkpoint == source_checkpoint` ;
- la réconciliation `matched` couvre ce même checkpoint ;
- le stage `load` est `healthy`.

Un apply en retard sur le load, avec le même receiver, est `degraded`. Un apply
en avance sur le load est un incident `destination_apply_ahead_of_load`. Des
receivers différents produisent `unknown`.

### 3.7 `reconciliation`

`state` vaut `not_run`, `running`, `matched`, `mismatch`, `failed` ou `unknown`.
`observed_at` est obligatoire pour `running`, `matched`, `mismatch` et `failed`.

`window` est obligatoire pour `matched` et `mismatch`. Elle est bornée par
`from_exclusive` (position ou `null` pour le début connu) et `to_inclusive`.
Pour autoriser `healthy`, `to_inclusive` doit être égal à
`source_checkpoint`, `load.checkpoint` et `destination.apply_checkpoint`.
Les deux bornes doivent utiliser le même receiver ; la v1 ne prétend pas
réconcilier une fenêtre multi-receiver.

Les huit compteurs de l'exemple sont obligatoires pour `matched` et `mismatch`.
Ils mesurent le ledger d'événements techniques, pas le nombre final de lignes
métier :

- `captured_event_count` : événements raw valides dans la fenêtre ;
- `loaded_event_count` : événements acceptés par le landing Snowflake ;
- `ledger_event_count` : lignes du ledger canonique dans la fenêtre ;
- `distinct_event_count` : identités `event_id` distinctes du ledger ;
- `duplicate_event_count` : identités dupliquées dans le ledger ;
- `missing_event_count` : événements capturés absents du ledger ;
- `unexpected_event_count` : événements du ledger absents du raw borné ;
- `failed_mutation_count` : mutations métier rejetées ;
- `reconciled_event_count` n'est volontairement pas ajouté : il serait
  redondant et pourrait masquer un écart entre les autres comptes.

La relation normative pour `matched` est :

```text
captured_event_count
  = loaded_event_count
  = ledger_event_count
  = distinct_event_count

duplicate_event_count = 0
missing_event_count = 0
unexpected_event_count = 0
failed_mutation_count = 0
```

Toute violation avec `state=matched` est une contradiction et produit
`destination_reconciliation_inconsistent`. `state=mismatch`, ou un seul des
quatre compteurs d'écart supérieur à zéro, produit l'incident
`destination_reconciliation_mismatch`. `failed` produit
`destination_reconciliation_failed`. `not_run`, `running` et `unknown`
interdisent `healthy` mais ne sont pas, seuls, des incidents.

## 4. Projection des étapes

Le modèle public `StageProjection` ne change pas. Les seules valeurs de statut
restent `healthy`, `degraded`, `incident`, `unknown` et `planned_stop`.
`headline` et `detail` sont générés par le control plane à partir d'une table de
messages fixe ; aucune chaîne du document d'entrée n'est réémise.

### 4.1 Étape `load`

Évaluation dans cet ordre :

1. contradiction de checkpoint, `load.state=failed`, activation `blocked`, ou
   réconciliation en incident → `incident` ;
2. horloge non fiable, receiver non comparable, état inconnu ou données
   requises absentes → `unknown` ;
3. `not_started` avec activation non configurée/désactivée → `unknown` ;
4. observation périmée → `unknown` ;
5. `planned_stop` → `planned_stop` ;
6. `running`, checkpoint en retard ou réconciliation non terminée → `degraded` ;
7. toutes les conditions de la section 3.5 → `healthy` ;
8. toute combinaison restante → `unknown`.

### 4.2 Étape `destination`

Évaluation dans cet ordre :

1. contradiction de checkpoint, `destination.state=failed`, activation
   `blocked` ou réconciliation en incident → `incident` ;
2. horloge non fiable, receiver non comparable, état inconnu ou données
   requises absentes → `unknown` ;
3. `not_started` avec activation non configurée/désactivée → `unknown` ;
4. observation périmée → `unknown` ;
5. `planned_stop` → `planned_stop` ;
6. `applying`, apply en retard, load non healthy ou réconciliation non terminée
   → `degraded` ;
7. toutes les conditions de la section 3.6 → `healthy` ;
8. toute combinaison restante → `unknown`.

### 4.3 Statut pipeline et couverture

Lorsque l'extension est présente et valide, `_pipeline_status` considère les
cinq étapes dans cet ordre de priorité :

```text
incident > unknown > degraded > planned_stop > healthy
```

Les modificateurs globaux s'appliquent ensuite :

- une fraîcheur autre que `fresh` produit `unknown` ;
- une `evidence_kind` autre que `live` plafonne le statut à `degraded` ;
- un lag absent, contradictoire ou non comparable produit `unknown` ;
- un verdict de lag `CATCHING_UP` plafonne le statut à `degraded` ; seuls
  `STABLE` et `BOUNDED`, avec un lag numérique non négatif, sont compatibles
  avec `healthy` ;
- `run.state=STOPPED_BUDGET` plafonne le statut à `planned_stop`, même si la
  dernière position est complètement réconciliée ;
- `healthy` n'est retourné que si les cinq étapes sont `healthy` et que toutes
  les conditions E2E sont satisfaites.

`quality.coverage` devient `complete` uniquement si `load`, `destination` et la
réconciliation couvrent exactement `source_checkpoint`. Dans tous les autres
cas, il reste `partial`. Les valeurs existantes de `quality.freshness` et
`quality.evidence_kind` restent inchangées.

## 5. Conditions exactes du vert et du mouvement

Le vert est une conséquence, pas une donnée d'entrée. L'API et l'UI doivent
appliquer la même garde :

```text
green =
  pipeline.status == healthy
  AND quality.coverage == complete
  AND quality.freshness == fresh
  AND quality.evidence_kind == live
  AND source.status == available
  AND activation.state == active
  AND every stage.status == healthy
  AND source_checkpoint == load.checkpoint == destination.apply_checkpoint
  AND reconciliation.state == matched
  AND reconciliation.window.to_inclusive == source_checkpoint
```

Une preuve `historical` ou `simulation`, même complète et fraîche par rapport à
son fichier, ne devient jamais verte. Une capture `planned_stop` peut montrer
une dernière position « réconciliée », mais le pipeline n'est ni `healthy` ni
vert.

Le mouvement de transit représente une progression observée, jamais une simple
connexion SSE. Il est autorisé uniquement si :

```text
motion =
  green
  AND previous revision was green for the same pipeline
  AND run.state == RUNNING
  AND SSE connection == live
  AND current revision > previous revision
  AND common E2E checkpoint progressed between both consecutive revisions
```

En v1, « progressed » signifie : même pipeline, même receiver et séquence
strictement croissante. Une rotation de receiver coupe le mouvement jusqu'à ce
qu'un futur contrat d'ordinal rende l'ordre prouvable. Un heartbeat sans
progression peut animer l'indicateur de connexion, pas le transit de données.
Le réglage `prefers-reduced-motion` reste prioritaire et désactive toute
animation.

## 6. Cas opérateurs normatifs

| Situation | Load | Destination | Pipeline | Vert | Mouvement |
|---|---|---|---|---|---|
| extension absente | unknown | unknown | comportement legacy, jamais healthy | non | non |
| simulation locale complète | healthy au mieux au niveau technique | healthy au mieux au niveau technique | degraded | non | non |
| historique complet | healthy au mieux au niveau technique | healthy au mieux au niveau technique | degraded | non | non |
| live, frais, checkpoints égaux, réconciliation matched | healthy | healthy | healthy | oui | seulement après progression prouvée |
| load en retard sur la source | degraded | degraded ou unknown | degraded | non | non |
| apply en retard sur le load | healthy ou degraded | degraded | degraded | non | non |
| réconciliation en cours | degraded | degraded | degraded | non | non |
| timestamp périmé ou futur non fiable | unknown | unknown | unknown | non | non |
| receiver non comparable | unknown | unknown | unknown | non | non |
| load/apply en avance sur l'amont | incident | incident | incident | non | non |
| écart, trou, doublon ou mutation rejetée | incident | incident | incident | incident, jamais vert | non |
| adapter non configuré, désactivé ou en validation | unknown | unknown | unknown ou degraded selon le comportement legacy | non | non |
| activation blocked, échec d'auth, de permission ou de load | incident | incident | incident | non | non |
| arrêt worker planifié et dernière position réconciliée | planned_stop ou healthy | planned_stop ou healthy | planned_stop | non | non |
| source indisponible après un succès | dernière projection conservée stale | idem | unknown | non | non |

Le dimanche ou pendant toute fenêtre d'arrêt IBM i, seule une observation
explicite `STOPPED_BUDGET` fraîche peut produire `planned_stop`. L'absence de
refresh devient `stale/unknown`. Aucun calendrier codé en dur, nom de jour ou
supposition d'exploitation ne doit fabriquer un arrêt planifié.

## 7. Sanitisation et frontière de sécurité

L'adapter privé peut utiliser Secrets Manager, une identité Snowflake et des
noms physiques. Son résultat public est construit dans un nouvel objet à partir
d'allowlists ; il n'est jamais sérialisé par copie du résultat SDK.

Interdits dans `destination_proof` et dans la projection REST/SSE :

- mots de passe, tokens, clés privées/publiques ou empreintes de clé ;
- ARN ou nom de secret, variable d'environnement ou chemin de fichier ;
- account locator, URL/host, user, rôle, warehouse ou noms physiques
  database/schema/table/stage ;
- SQL, query ID, payload, échantillon de ligne ou valeur métier ;
- stack trace, message d'exception, réponse de driver ou texte libre ;
- clé S3, manifest complet, batch payload ou contenu d'événement.

Les erreurs publiques sont des codes allowlistés. Les identifiants publics sont
validés avant stockage. Les nombres refusent booléens, NaN, infini, négatifs et
dépassements. La limite existante de 2 MiB sur le document reste applicable.

Le fingerprint du repository porte la projection déjà sanitisée. Une rotation
de secret ou un changement de message interne ne doit donc pas créer une
révision SSE, sauf si l'état public de preuve change.

### 7.1 Ce que les signaux Snowflake établissent — et n'établissent pas

L'adapter peut consulter plusieurs sources Snowflake côté serveur, mais il ne
doit jamais confondre leurs portées :

- [`COPY_HISTORY`](https://docs.snowflake.com/en/sql-reference/functions/copy_history)
  expose notamment le fichier, `ROW_COUNT`, `ROW_PARSED`, `ERROR_COUNT`, le
  statut et l'heure de fin. Ces signaux peuvent étayer `load`, après
  rattachement du fichier au manifest raw et à son checkpoint. Ils ne prouvent
  pas l'application métier.
- `LOAD_HISTORY` ou un succès de job/COPY isolé n'établit pas davantage
  `destination=applied`. Un fichier « Loaded » sans ledger/checkpoint relié à la
  source reste au mieux une preuve partielle de load.
- [`QUERY_HISTORY`](https://docs.snowflake.com/en/sql-reference/organization-usage/query_history)
  peut confirmer le statut d'exécution d'une requête d'application. Son
  `execution_status`, son query ID et son éventuel message d'erreur sont des
  données privées de diagnostic. Un statut `success` ne devient un
  `apply_checkpoint` qu'après commit et persistance explicite de la position
  IBM i correspondante par l'adapter.
- Les [Streams Snowflake](https://docs.snowflake.com/en/user-guide/streams-intro)
  représentent les différences entre offsets transactionnels ; la clause
  [`CHANGES`](https://docs.snowflake.com/en/sql-reference/constructs/changes)
  permet une lecture bornée non consommatrice et expose notamment
  `METADATA$ROW_ID`. Ces mécanismes peuvent étayer l'application et la
  réconciliation, mais leur offset n'est pas un receiver/sequence IBM i. Le
  mapping doit être persisté et vérifié ; il n'est jamais déduit par égalité de
  timestamps ou de nombres de lignes.

Ainsi, aucune de ces sources prise seule ne peut rendre `destination` healthy.
Le checkpoint d'application et la réconciliation restent deux preuves
distinctes, même lorsque le MERGE s'est exécuté avec succès.

## 8. Interface d'adapter recommandée

L'implémentation doit séparer acquisition privée et projection publique :

```python
class DestinationAdapter(Protocol):
    def observe(
        self,
        *,
        pipeline_id: str,
        source_checkpoint: JournalPosition,
        now: datetime,
    ) -> DestinationProofInput:
        """Observe sans muter et ne retourne que le modèle public sanitisé."""
```

Le control plane reçoit un `DestinationProofInput` typé, pas un dictionnaire de
driver. Les credentials sont injectés dans l'instance de l'adapter par le
runtime ; ils ne sont pas des paramètres d'`observe()` et ne figurent pas dans
le modèle.

L'activation reste read-only dans cette verticale. Une future commande
d'activation devra être authentifiée, auditée et séparée des routes de lecture
actuelles ; ce contrat ne l'autorise pas.

## 9. Migration par étapes

1. **Modèle sans comportement** : ajouter des dataclasses gelées pour les
   positions, activation, load, destination et réconciliation. Conserver
   `PipelineProjection.to_dict()` inchangé hors des cinq étapes et de
   `quality.coverage`.
2. **Parser optionnel** : valider `destination_proof` seulement lorsqu'il est
   présent. Préserver bit pour bit les projections des fixtures v1 sans
   extension.
3. **Projection fail-closed** : implémenter les tables de décision et les codes
   publics. Ne pas retourner `healthy` avant que tous les tests négatifs soient
   présents.
4. **Adapter fixture/historique** : produire une preuve complète fictive avec
   `evidence_kind=simulation`, puis historique. Elle doit rester immobile et
   non verte.
5. **Adapter Snowflake DEV en shadow** : observer server-side l'activation, les
   checkpoints et la réconciliation, sans exposer sa configuration. Tant que
   le runtime IBM i est indisponible, conserver la preuve `historical` ou
   `unknown`; ne pas changer le descripteur en `live`.
6. **Live DEV** : seulement pendant une fenêtre IBM i réelle, comparer le
   checkpoint source courant, le load, l'apply et la réconciliation sur la même
   fenêtre. Capturer deux révisions consécutives avant d'autoriser le mouvement.
7. **Généralisation** : introduire une v2 seulement si une position
   multi-receiver comparable ou une preuve métier plus riche devient
   obligatoire. L'absence de l'extension v1 doit rester lisible durant toute la
   migration.

## 10. Tests obligatoires avant activation

### Projection et modèle

- absence de `destination_proof` : égalité exacte avec les projections et tests
  actuels ;
- forme complète live/fresh/matched : cinq étapes `healthy`, couverture
  `complete`, pipeline `healthy` ;
- même forme avec `simulation` puis `historical` : pipeline `degraded`, jamais
  vert ;
- chacun des six timestamps stale, futur au-delà de la tolérance et sans
  timezone : résultat fail-closed attendu ;
- `source_checkpoint` différent de `position.checkpoint` ;
- load derrière la source, load devant la source, apply derrière le load, apply
  devant le load et receivers différents ;
- chaque état activation/load/destination/reconciliation ;
- `matched` avec chaque égalité de compteur cassée séparément ;
- `mismatch` avec manque, inattendu, doublon et mutation rejetée ;
- nombres booléens, négatifs, NaN, infinis et supérieurs à la borne ;
- propriété supplémentaire et enum inconnue ;
- messages, identifiants et codes publics stables en français, sans réflexion de
  valeur contrôlée.

### Repository et serveur

- extension invalide après un succès : dernière projection conservée,
  `freshness=stale`, source `unavailable`, aucune donnée fautive réfléchie ;
- changement d'un checkpoint, d'un état ou d'un compteur public : nouvelle
  révision ;
- changement privé sans changement de projection : pas de nouvelle révision ;
- REST et SSE ne contiennent aucune chaîne injectée dans un champ refusé ;
- refresh concurrents : une ancienne preuve ou un ancien échec ne peut pas
  écraser une observation plus récente ;
- ETag, replay SSE et reset d'historique conservent le comportement actuel.

### UI et parcours opérateur

- table de vérité `green` testée pour chaque condition prise isolément ;
- table de vérité `motion` avec progression, heartbeat sans progression,
  rotation de receiver, SSE reconnecté et `prefers-reduced-motion` ;
- état `not_configured` sans champ de credential ;
- états `blocked`, `unknown`, `degraded`, `incident` et `planned_stop` lisibles
  dans Overview, Pipelines, Proofline et drawer ;
- source coupée après une observation live : l'écran bascule en stale/unknown et
  ne conserve ni vert ni mouvement ;
- fixture du dimanche : aucune règle calendaire ne transforme l'absence de
  runtime en planned stop ;
- vérification qu'aucun secret, nom physique de cible ou message driver n'est
  rendu, loggé ou stocké dans l'état navigateur.

### Gate E2E DEV

Le gate live requiert des preuves runtime et ne peut pas être exécuté pendant
l'arrêt IBM i. Lorsqu'une fenêtre est disponible, il doit établir :

1. source IBM i réellement disponible et `evidence_kind=live` ;
2. raw et checkpoint source observés ;
3. load et apply au même checkpoint ;
4. réconciliation `matched` sur la même fenêtre ;
5. deux révisions consécutives montrant une progression monotone ;
6. arrêt de l'animation et passage fail-closed lorsque l'un de ces signaux est
   retiré.

Jusqu'à cette campagne, le statut commercial honnête reste : **contrat prêt à
implémenter, preuve E2E live non établie**.
