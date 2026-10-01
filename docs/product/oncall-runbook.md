# Runbook d'astreinte Quadringent (DEV/INT)

Périmètre : la release `quadringent` dans le namespace `quadringent-demo`, le bucket
`example-corp-000000000000-int-example-corp-raw` (préfixe `as400/sales`), la table
DynamoDB `example-corp-int-example-corp-checkpoints` et le namespace destination
`DEV_RAW.AS400_RD`. Toutes les actions sont bornées DEV ; rien ici ne touche
la production. Installation, vérification post-install et désinstallation de
la release : `docs/product/install-client.md`.

Seuils de référence : `infra-values/slo-policy-dev.json` (fraîcheur capture
120 s, fraîcheur S3 180 s, fraîcheur destination 300 s, lag 1 000 séquences,
p95/p99 livraison 60 s/180 s, 500 000 requêtes S3/24 h, 10 fichiers Snowpipe
pending, 1 crédit Snowflake/24 h). Le dead man's switch de la sonde tire à
3 600 s (`--deadman-max-age-seconds` par défaut).

Lexique des preuves :

- `s3://…/as400/sales/sale/console-snapshot.json` : dernier état publié par
  le lecteur (throttlé à 10 s, toujours écrit à la fermeture) ;
- `s3://…/as400/sales/sale/proofs/<preuve-autonome>.json` : preuve
  cockpit combinée ; son bloc `observability` porte le rapport SLO et l'état
  d'alerte persisté. Le nom est déclaré par site (`site.autonomousProofName`,
  `cdcforge-autonomous-latest.json` en INT — identité IRSA transitoire) ;
- `s3://…/as400/sales/fleet/history-progress/<run_id>.json` : progression
  par tranches de la copie historique (un document par run) ;
- PVC `quadringent-quadringent-control-plane-fleet-state` : état
  d'orchestration de la flotte du control plane (`fleet-prepare.json`,
  `fleet-history.json`, `fleet-pause.json`, monté sur
  `/var/lib/quadringent-fleet`). Persistant par défaut
  (`controlPlane.fleetState.persistence`) : un remplacement du pod reprend la
  gate de reprise au dernier état écrit au lieu de repartir d'un répertoire
  vide. Un PVC `Pending` (StorageClass absente du cluster) bloque le démarrage
  du pod — le repli déclaré est `persistence.enabled: false` (emptyDir
  volatil) ;
- DynamoDB `example-corp-int-example-corp-checkpoints` : position durable par stream key.

Diagnostic transverse, toujours disponible :

```bash
kubectl -n quadringent-demo get pods -l app.kubernetes.io/instance=quadringent
kubectl -n quadringent-demo logs deploy/quadringent-quadringent -c capture --tail=200
kubectl -n quadringent-demo get cronjob,job -l app.kubernetes.io/component=observability
aws s3api head-object --bucket example-corp-000000000000-int-example-corp-raw \
  --key as400/sales/sale/console-snapshot.json   # LastModified = âge réel
```

## Pod capture absent ou mort

**Signal** : `kubectl get pods` ne montre aucun pod `quadringent-quadringent-*` Running,
ou `restartCount` croissant ; `replicaCount=0` est l'état nominal pendant un
pilote borné.

**Diagnostic** :

```bash
kubectl -n quadringent-demo get deploy quadringent-quadringent -o jsonpath='{.spec.replicas}'
kubectl -n quadringent-demo describe pod -l app.kubernetes.io/instance=quadringent
kubectl -n quadringent-demo logs -l app.kubernetes.io/instance=quadringent -c capture --previous --tail=200
```

**Action bornée** : si `replicaCount=0`, vérifier qu'aucun Job `*-pilot-*`
n'est actif avant de réarmer — deux lecteurs concurrents bloquent IBM i
(guard `Recreate` + `replicaCount <= 1`). Une boucle CrashLoop se lit dans
`--previous` : erreur de config → corriger les values, jamais de contournement
local. La liveness tue un lecteur gelé au bout de ~4 min sans progression CPU ;
des restarts en rafale sans erreur de log indiquent un gel, pas un crash.

## « Lecture AS400 arrêtée »

**Signal** : le snapshot console porte `run.state != RUNNING`
(`STOPPED_FAIL_CLOSED`, `STOPPED_BUDGET`, `STOPPED_PROOF_CHAIN`) et
`run.last_error` documente la cause.

**Diagnostic** :

```bash
aws s3 cp s3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/console-snapshot.json - \
  | python3 -m json.tool | sed -n '/"run"/,/}/p'
```

`STOPPED_BUDGET` sans `last_error` est un arrêt nominal borné
(`--max-polls`/`--max-seconds`). `STOPPED_FAIL_CLOSED` est une erreur source
répétée au-delà de `safety.maxConsecutiveErrors` (5) : lire le type d'erreur,
vérifier TLS et le compte `CDCUSER` avant tout redémarrage.

## Alerte de fraîcheur (capture ou S3)

**Signal** : rapport SLO `capture_freshness` ou `s3_freshness` en `breach`,
âge > 120 s / 180 s ; l'alerte `firing` correspondante est dans
`observability.alert_state.alerts`, émise vers le transport configuré
(`QUADRINGENT_ALERT_WEBHOOK_URL` / `QUADRINGENT_ALERT_SNS_TOPIC`).

**Diagnostic** : comparer `LastModified` du snapshot console et
`generated_at` de la preuve cockpit à l'horloge courante. Un pod vivant mais
sans publication fraîche indique un gel de boucle — la liveness devrait le
redémarrer ; vérifier `lastState` du conteneur.

**Action bornée** : si le pod est sain et le seul signal âgé est S3, suspecter
le throttle ou une erreur `console_snapshot_write_failed` dans les logs ;
ne jamais écrire de snapshot à la main.

## Ledger figé (checkpoint sans progression)

**Signal** : `lag.current` stable ou croissant, `counters.windows_published`
immobile dans le snapshot console ; le checkpoint DynamoDB ne bouge plus
alors que `position.source_tail` avance.

**Diagnostic** :

```bash
aws dynamodb get-item --table-name example-corp-int-example-corp-checkpoints \
  --key '{"stream_key":{"S":"as400/sales/sale"}}' --region eu-west-3
```

Comparer `position.checkpoint` à `position.source_tail` dans le snapshot :
un écart > 1 000 séquences est un `breach` de `checkpoint_lag`. Un écart entre
deux receivers sans chaîne explicite rend le check `unobserved` — c'est le
cas `receiver_chain_required`, pas une preuve de lag.

**Action bornée** : aucune réécriture manuelle du checkpoint. Si le receiver
a tourné, voir la section suivante ; sinon redémarrer le lecteur (il reprend
au checkpoint durable) et conserver les logs du poll fautif.

## Latence destination (p95/p99)

**Signal** : `delivery_latency_p95` > 60 s ou `delivery_latency_p99` > 180 s
dans le rapport SLO ; fichiers Snowpipe `pending` > 10.

**Diagnostic** : la latence est mesurée sur la fenêtre du run entre
`run.started_at` et `destination_proof.observed_at` — vérifier d'abord que la
preuve destination n'est pas simplement âgée (`canonical_freshness` > 300 s).
Côté Snowflake, compter les fichiers en file Snowpipe ; une accumulation
soudaine après un pic de volume est attendue, une accumulation monotone non.

**Action bornée** : attendre un cycle de 15 min avant de conclure — la mesure
est fenêtrée. Ne pas suspendre le CronJob observability « pour voir » : il ne
charge rien, il ne fait que mesurer.

## Rotation de receiver

**Signal** : `checkpoint_lag` en `unobserved` avec raison
`receiver_chain_required`, ou `position.checkpoint.receiver` différent de
`position.source_tail.receiver` dans le snapshot console.

**Diagnostic** : la chaîne de receivers du run est dans la preuve de
continuité ; la vérifier avec :

```bash
python3 scripts/quadringent_fleet_continuity.py --run-prefix as400/sales/sale/runs/<run_id>
```

**Action bornée** : la continuité se prouve par artefacts, jamais par
inférence de séquences (IBM i peut relancer à 1 après IPL). Si la chaîne est
prouvée, la projection reprend seule ; si elle est incertaine, la flotte reste
`unproven_continuity` et les promesses live/certifiée restent bloquées — ne
pas forcer.

## Échec de publication S3

**Signal** : erreurs `PutObject`/`AccessDenied` dans les logs capture, ou
`publication_status: "unknown"` dans les logs du CronJob observability ; compteur
`errors` > 0 côté SLO.

**Diagnostic** : `kubectl logs` du conteneur concerné ; vérifier l'identité
IRSA du ServiceAccount (`as400-snowflake-capture` pour la capture,
`cdcforge-verifier` pour l'observabilité en INT — identité IRSA transitoire,
cf. `verifierServiceAccountName`) et la policy
`infra-values/iam-int-policy.json` / `iam-verifier-int-policy.json`. Une
publication conditionnelle `IfMatch` en conflit (`412`) signifie qu'une
preuve plus récente existe : elle prévaut, ne pas forcer.

**Action bornée** : relire l'objet S3 actuel avant toute relance manuelle ; un
`publication_status: unknown` exige de vérifier la version S3 réelle — jamais
de PUT aveugle.

## Sonde d'observabilité silencieuse (dead man's switch)

**Signal** : `observability.alert_state.updated_at` (ou
`slo_report.observed_at`) de la preuve cockpit plus vieux que 3 600 s, ou bloc
`observability` absent — alors même que le CronJob devrait tourner toutes les
15 min. C'est le cas où « rien n'alerte » est l'alerte.

**Diagnostic** :

```bash
kubectl -n quadringent-demo get cronjob -l app.kubernetes.io/component=observability \
  -o jsonpath='{.items[0].spec.suspend} {.items[0].status.lastScheduleTime}'
kubectl -n quadringent-demo get jobs -l app.kubernetes.io/component=observability --sort-by=.metadata.creationTimestamp
python3 scripts/quadringent_slo_alerts.py --report <dernier-rapport.json> \
  --state <etat-alerte.json> \
  --observability-snapshot <preuve-cockpit.json>   # deadman => stale|missing
```

**Action bornée** : si `suspend` est revenu à `true` après un upgrade,
réaligner `infra-values/values-int.yaml` (`observability.suspend: false`). Si
les Jobs échouent, lire leurs logs (`activeDeadlineSeconds: 600`,
`backoffLimit: 0` — aucun retry automatique) ; un échec `a delivery proof must
exist` signifie qu'aucune preuve de livraison n'a encore été produite, pas que
la sonde est cassée.

## Livraison flotte « non observée » (étapes load/destination inconnues)

**Signal** : la console flotte affiche `load`/`destination` inconnus alors que
les tuyaux Snowpipe tournent — le lecteur écrit le brut, rien ne mesure la
cible tant que `fleetObserve` n'est pas déployé et appliqué.

**Chaîne attendue** : le CronJob `*-fleet-observe` lit
`as400/sales/fleet/console-snapshot.json`, mesure chaque tuyau
(`SYSTEM$PIPE_STATUS`), les volumes bruts et canoniques par receiver, puis
publie le document combiné sur `as400/sales/fleet/console-proof.json`
(une clé, un écrivain — le snapshot du lecteur n'est jamais modifié). Le
control plane lit cette clé quand `fleetConsoleSource` y pointe.

**Diagnostic** :

```bash
kubectl -n quadringent-demo get cronjob -l app.kubernetes.io/component=fleet-observability \
  -o jsonpath='{.items[0].spec.suspend} {.items[0].status.lastScheduleTime}'
kubectl -n quadringent-demo logs job/<job-fleet-observe> -c observer --tail=5
aws s3api head-object --bucket <raw> --key as400/sales/fleet/console-proof.json   # preuve présente ?
python3 scripts/quadringent_fleet_observe.py --aws-profile <profil> \
  --connection-name <connexion>    # dry-run borné, rien n'est écrit
```

**Action bornée** : un statut `publication_status: confirmed` avec
`load/destination/reconciliation` mesurés signe la chaîne ; un échec sur la
lecture du snapshot indique un lecteur arrêté (voir « Lecture AS400
arrêtée »), pas une sonde cassée. Le `generated_at` de la preuve est l'heure
de mesure : une cadence suspendue fait déclarer la fraîcheur `stale` après
5 min — vérifier `fleetObserve.suspend` avant tout diagnostic Snowflake.
