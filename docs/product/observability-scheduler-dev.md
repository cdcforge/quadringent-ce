# Planification de l'observabilité DEV

Le bloc Helm `observability` rafraîchit uniquement l'observabilité de la preuve
existante. Il ne démarre pas de capture et ne transforme pas une livraison
historique en livraison fraîche. La qualification de ce runtime doit être
rejouée sur le site avant activation.

## Activation contrôlée

- `observability.enabled` vaut `false` par défaut.
- `observability.suspend` vaut `true` par défaut. Une activation explicite du
  composant ne démarre donc pas immédiatement les rafraîchissements.
- Charger la politique versionnée avec
  `--set-file observability.policyJson=infra-values/slo-policy-dev.json`.
- Cadences admises : `*/15 * * * *` ou `0 * * * *`, en UTC.
- Image exclusivement par digest, service account déclaré par site
  (`verifierServiceAccountName`), associé au rôle du vérificateur.
- Concurrence `Forbid`, délai de démarrage 120 s, exécution maximale 600 s,
  aucune relance automatique de Job après échec.

Avant un upgrade, récupérer les valeurs effectives de la release et comparer
les manifests. Ne pas appliquer aveuglément `values-int.yaml` : son image de
capture peut différer de l'image déjà déployée. Les Deployments capture et
cockpit doivent rester identiques. Aucun rôle ou secret n'est créé ici.

## Arrêt et incidents

Suspendre le CronJob empêche les prochains départs, mais n'arrête pas un Job
déjà actif ; celui-ci reste borné à 600 secondes. Conserver le réglage de
suspension dans les valeurs Helm pour qu'un upgrade ne le réactive pas.

Un code de sortie zéro signifie que le cycle de collecte/publication s'est
exécuté, pas que les SLO passent. Le cockpit expose les dépassements et les
mesures inconnues. La publication utilise l'ETag précédent : un conflit avec
une nouvelle preuve ne doit pas être forcé. Si le statut de publication est
`unknown`, vérifier la version S3 actuelle avant toute relance manuelle.

### Contrôle manuel pendant la planification

`concurrencyPolicy: Forbid` protège uniquement les Jobs gérés par ce CronJob.
Il ne bloque pas un Job créé manuellement depuis son modèle. Vérifier qu'aucun
Job n'est actif avant le lancement manuel ne suffit pas : une échéance peut
survenir pendant son exécution.

Pour un contrôle manuel nécessitant une publication :

1. Conserver les valeurs Helm effectives et le réglage de suspension initial.
2. Suspendre le scheduler via un changement DEV contrôlé, en vérifiant que le
   rendu ne modifie ni la capture ni le cockpit.
3. Attendre l'état terminal des Jobs observateurs déjà actifs ; la suspension
   du CronJob ne les arrête pas. Ne pas créer un second écrivain de preuve.
4. Exécuter le Job manuel borné, conserver son résultat et vérifier sa version
   S3. En cas de publication incertaine, ne pas forcer ni relancer le PUT.
5. Restaurer le réglage initial et observer le premier cycle programmé suivant.

Cette procédure ne remplace pas le contrôle conditionnel ETag : une nouvelle
preuve d'ingestion peut toujours arriver pendant la collecte. Un conflit doit
préserver la preuve la plus récente, jamais l'écraser avec l'archive précédente.

Les logs conservent le reçu de publication (version/ETag). Le rapport SLO complet
est dans la version S3 publiée ; `/work/observation.json` est éphémère. Ce composant
ne livre aucune notification externe. Il ne fournit pas le coût Snowflake tant
que la vue de metering requise n'est pas disponible.
