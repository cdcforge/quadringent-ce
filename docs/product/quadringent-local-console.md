# Quadringent — exploiter la console locale

Ce runbook démarre la verticale locale same-origin : un snapshot borné, puis
un control plane qui sert REST, SSE et le cockpit. Il ne contacte ni IBM i,
ni AWS, ni Snowflake et n'effectue aucune mutation cloud.

## Pré-requis

- Python 3 avec les dépendances du dépôt ;
- Node.js 24 LTS, épinglé par `.mise.toml` (Node.js 26 également vérifié),
  et les dépendances verrouillées de `ui/` installées avec
  `npm ci --prefix ui` ;
- un bundle `ui/dist` produit par `npm run build` ;
- Helm disponible pour rendre la chart locale ;
- deux shells ouverts à la racine du dépôt, sauf indication contraire.

## Terminal 1 — produire un snapshot borné

```bash
PYTHONPATH=src python3 scripts/emit_console_snapshot_dev.py --out /tmp/quadringent-console.json
```

Cette commande exerce la vraie boucle raw-first sur un journal simulé, dans un
répertoire temporaire. Elle écrit seulement `/tmp/quadringent-console.json`.

## Terminal 2 — servir le control plane et le cockpit

```bash
PYTHONPATH=src python3 scripts/quadringent_control_plane.py \
  --source historical:local-proof:file:///tmp/quadringent-console.json \
  --ui-dist ui/dist
```

Le serveur se lie par défaut à `127.0.0.1:8844`. Ouvrir
`http://127.0.0.1:8844/`. La provenance `historical:local-proof` est
volontaire : le fichier est une preuve locale bornée, pas le heartbeat courant
d'un worker IBM i. Les routes de lecture restent `/v1/overview`,
`/v1/pipelines`, `/v1/pipelines/{id}` et `/v1/events`. Le parcours
`#/setup` valide un onboarding via `POST /v1/onboarding/evaluate` : aucune
mutation IBM i, S3 ou Snowflake, aucun secret accepté.

Le hot-reload Vite sur `http://127.0.0.1:5180/` reste un outil de
développement optionnel. Il n'est pas le chemin d'exploitation. Vite utilise
ce port en `strictPort` : s'il est occupé, il échoue au lieu d'en choisir un
autre. Le build de production reste same-origin et n'embarque aucun fallback
vers une fixture.

## Ce que cette preuve établit

- le snapshot produit par le moteur est projeté en un modèle opérateur sûr ;
- Overview, Pipelines, détail Overview/Live, Incidents et Usage consomment le
  même contrat REST/SSE ;
- la simulation ou une observation périmée ne peut pas devenir `Healthy` ;
- la destination non observée reste visible et dégrade le verdict ;
- une révision SSE déclenche le rechargement du snapshot sans inventer de
  mesures intermédiaires.

La preuve reste locale et historique jusqu'à ce qu'un worker live mette à jour
le fichier et que sa source soit déclarée explicitement `live`. Changer le mot
`historical` dans la commande ne crée pas une preuve live : il faut un worker
réel, une observation fraîche et les signaux attendus.

La livraison Snowflake reste non prouvée tant que le replay n'a pas composé le
snapshot capture avec une preuve contenant `load_checkpoint`,
`apply_checkpoint` et les comptes réconciliés. Le cockpit affiche donc
« Destination non observée » par défaut et ne conclut jamais à une garantie
end-to-end par simple présence d'une table.

Le helper de replay produit cette preuve uniquement après un `COPY`/`MERGE`
idempotent dans `DEV_RAW.AS400_RD` :

```bash
PYTHONPATH=src python3 scripts/snowflake_external_replay.py \
  --connection-name example-corp \
  --stage AS400_RD_SALE_EXTERNAL_STAGE \
  --all-jsonl \
  --run-tag E2EFULL20260831 \
  --capture-snapshot /tmp/quadringent-capture.json \
  --proof-output /tmp/quadringent-e2e.json \
  --execute --confirm AS400_RD_EXTERNAL_REPLAY_DEV
```

Le fichier combiné est écrit atomiquement. Le helper refuse tout autre schéma,
un checkpoint absent, un compte raw/canonique divergent, un doublon ou un
replay non idempotent. Le control plane peut lire le document depuis un fichier,
HTTP(S) ou directement depuis S3 avec une lecture bornée :

```bash
PYTHONPATH=src python3 scripts/quadringent_control_plane.py \
  --source live:dev-sale:s3://example-corp-000000000000-int-example-corp-raw/<cle-du-snapshot-combine> \
  --environment dev
```

Cette variante contacte uniquement S3 en lecture et exige que les dépendances
AWS soient disponibles dans l'environnement Python. `--environment dev`
empêche qu'une preuve DEV soit étiquetée `local` dans l'API et le cockpit.

Pour le loader autonome, la source courante dédiée est :

```text
s3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/proofs/quadringent-autonomous-latest.json
```

Elle n'est publiée par `quadringent_autonomous_verify.py` qu'après une
réconciliation Snowpipe/canonical réussie et une confirmation DEV explicite.

Le statut `healthy` n'est possible que si les cinq étapes sont fraîches et
`healthy`, la couverture est complète, la provenance est `live` et le retard
est mesuré. Une preuve destination périmée redevient `unknown` ; une
réconciliation décalée devient `degraded`.

## Vérification déterministe

Après `npm run build` dans `ui/`, lancer depuis la racine :

```bash
PYTHONPATH=src python3 scripts/verify_quadringent_vertical.py --ui-dist ui/dist
```

Le vérificateur est read-only. Il importe les modules runtime, rend la chart
Helm, reconstruit Vite dans un répertoire isolé depuis l'entrée exacte
`/src/main.tsx`, compare les artefacts exécutables au bundle fourni, démarre un
serveur borné sur un port système, contrôle le routeur exécuté, les routes REST
et la forme SSE, puis détruit son répertoire temporaire. Chaque contrôle imprime
`PASS` ou `FAIL` et la commande renvoie `0` uniquement si tous les contrôles et
prérequis sont satisfaits. La présence de mots ou de marqueurs dans le JavaScript
n'est pas considérée comme une preuve de code vivant.

Cette procédure ne prouve ni déploiement Kubernetes, ni accès IBM i, ni
chargement Snowflake, ni soak. Elle ne réalise aucune mutation cloud.
