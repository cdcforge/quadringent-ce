# API HTTP `/v1`

API locale du cockpit, JSON UTF-8. Voir [sécurité](../SECURITY.md) pour l’identité.
Les routes GET acceptent aussi HEAD. Les erreurs simples ont la forme
`{"error":{"code":"..."}}` ; les refus d’évaluation peuvent retourner un verdict
structuré. Aucun code 200 ne garantit une réplication complète.

| Méthode | Route | Réponse |
|---|---|---|
| GET | `/healthz` | 200 sans donnée, exempt d’auth |
| GET | `/v1/version` | `version`, `api` |
| GET | `/v1/overview` | révision, synthèse, pipelines |
| GET | `/v1/pipelines` | `revision`, `pipelines` |
| GET | `/v1/pipelines/{id}` | `revision`, `pipeline` ; 404 absent |
| GET | `/v1/events` | notifications SSE de révision |
| GET | `/v1/onboarding/defaults` | `defaults`, identité publique `site` |
| POST | `/v1/onboarding/evaluate` | verdict de déclaration, jamais sonde réseau |
| GET | `/v1/connections` | déclarations persistées, si store configuré |
| POST | `/v1/connections` | 201 et `connection`, si store configuré |
| POST | `/v1/pipelines/{id}/actions/{action}` | reçu d’action ou refus |

## Lecture et actualisation

Les projections fournissent un ETag. Envoyer `If-None-Match` permet un 304 sans
corps. Les réponses de mutation sont `no-store`. La projection précise statut,
fraîcheur, provenance, compteurs et preuves ; une valeur `null` reste inconnue.
`observability.checks` contient les mesures ; `costs` peut être absent, ou porter
un montant nul au sens JSON (`null`) même si un prix est déclaré.

`infrastructure_costs` est facultatif. Sans preuve admissible :
`{"status":"unavailable","reason":"cost_evidence_unavailable"}`.
Sinon il contient `status: "available"`, `collected_at`, `namespace` et deux blocs :

- `storage` : `status: "measured"`, `observed_at`, `bytes`,
  `price_per_gib_month`, `monthly_run_rate`, `currency: "USD"`,
  `basis: "aws_public_standard_first_tier"` ;
- `cluster` : `status: "measured"`, `start`, `end`, `allocated_amount`,
  `cluster_amount`, `idle_amount` (nullable), `currency` déclarée,
  `basis: "opencost_no_idle_share"`.

Chaque bloc peut être `{"status":"unavailable"}` indépendamment de l’autre.
Les montants et octets sont des chaînes décimales ; `"0"` est une mesure,
pas une absence. Le run-rate mensuel S3, l’allocation cluster quotidienne et les
crédits Snowflake ne forment pas un total. Fraîcheur et périmètres : [FinOps](finops.md).

SSE : `stream.cursor`, `projection.updated`, `projection.reset` selon rétention,
avec `id` de révision et `data: {"revision":N}`. Le client recharge ensuite la
projection HTTP ; le flux ne transporte pas les pipelines. `Last-Event-ID`
reprend depuis un entier non négatif ; invalide → 400. Les commentaires keepalive
n’indiquent pas une nouvelle preuve.

## Déclaration d’une liaison

Lire d’abord `/v1/onboarding/defaults`. Exemple synthétique correspondant au site
local du README ; les étapes déclarées par le navigateur ne prouvent aucun accès :

```json
{
  "display_name": "Exemple local",
  "step": "destination",
  "ibmi_host": "ibmi.example.invalid",
  "ibmi_user": "CDCUSER",
  "tls": true,
  "allow_plaintext": false,
  "secret_ref_name": "quadringent-ibmi",
  "secret_ref_key": "password",
  "schema": "SALES",
  "table": "SALE",
  "journal_library": "DEMOLIB",
  "journal_name": "DEMOJRN",
  "snowflake_database": "EXAMPLE_RAW",
  "snowflake_schema": "IBMI_TEST",
  "snowflake_stage": "IBMI_TEST_SALE_EXTERNAL_STAGE"
}
```

Le nom du stage est dérivé de la configuration : utiliser celui publié par
`site.snowflake_stage`, pas un nom deviné.
Pour plusieurs tables, fournir `tables` à la place de `table`. La réponse porte
`connection_id`, horodatage, champs déclarés et `lifecycle_state:
"declared_not_in_service"`. Il n’existe pas d’activation implicite ni de route
DELETE de liaison. Un POST répété peut créer une nouvelle déclaration : relire
la liste après une réponse perdue.

Codes : 400 corps invalide/verdict bloqué, 401 identité absente, 403 groupe ou
origine refusée, 404 route absente sans store, 503 store indisponible.

## Actions

Actions fermées : `prepare`, `start`, `pause`, `resume`, `refresh`. Lire
`fleet_runtime.capabilities` lorsqu’il est présent, sinon `fleet.capabilities`.
Seul `state: "available"` autorise le bouton. Le serveur revalide avant effet.

```json
{"fleet_id":"example-corp-test","environment":"test","confirmation":"RESUME EXAMPLE-CORP TEST"}
```

`fleet_id` et environnement proviennent du site. La confirmation exacte est
`ACTION SITE_ID_EN_MAJUSCULES ENVIRONNEMENT_FLOTTE` ; `refresh` exige `null`.
Le corps possède exactement ces trois champs. Le reçu est un objet Python/JSON,
décrit côté UI par `PipelineActionReceipt`, pas une classe Python du même nom.
Il porte `id`, `action`, `fleet_id`, `environment`, `created_at`, `state`, et
`stages.intent`, `stages.execution`, `stages.observed_effect` ; chaque étape a
`state`, `code`, `message`. Évaluer les trois étapes, pas seulement le HTTP.

Codes usuels : 200 reçu, 400 corps invalide, 403 confirmation/environnement
incorrects, 404 liaison/action inconnue, 409 action concurrente ou conflit,
503 exécuteur/capacité indisponible ou audit en panne ; 500 résultat interne
invalide. Une route d’action en GET donne 405.

Les POST exigent `application/json`, Content-Length et une origine autorisée.
Pas de CORS permissif. Les gardes d’origine complètent l’authentification sans la
remplacer. Voir [audit et reprise](operations.md) avant de réessayer un résultat
incertain. Les corps sont bornés ; les champs inconnus ne doivent pas être utilisés
comme extension du contrat.
