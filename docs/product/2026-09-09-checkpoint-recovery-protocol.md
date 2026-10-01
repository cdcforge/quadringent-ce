# Protocole de reprise après qual0909a

Préannoncé avant exécution ; ne modifie pas le FAIL p95 du premier canary.
DEV CDCUSER / SALES.SALE, sans mutation métier ni accès Popsink/PROD.

## État de départ vérifié

Checkpoint DynamoDB du stream `as400/sales/sale/runs/qual0909a` :
`DEMOJRN3963/654721284`, format as400-checkpoint-v1. La lecture source suivante
doit reprendre au checkpoint + 1, jamais au tail. Ne pas recopier/modifier ce
checkpoint à la main. Revalider sa valeur avant exécution et refuser un drift.

Le raw de qual0909a reste immuable. Chaque nouveau processus réserve un préfixe
raw différent et publie son propre snapshot, mais utilise le même stream durable.
Ne pas exiger artificiellement que le checkpoint soit absent pour une reprise.
Le ConfigMap de la capture permanente ne change pas.

## Prérequis logiciel avant exécution

La revue a identifié un couplage historique : l'observateur déduisait le run
archivé depuis `flux.id`, lui-même dérivé du stream de checkpoint. Le correctif
ajoute `verification_archive.run_id` à la preuve après acquisition et
réconciliation du run. Les collecteurs relisent cette archive et vérifient ses
identités ; `flux.id` et le checkpoint durable ne sont pas renommés.
Les anciennes preuves sans ce champ conservent leur résolution historique.
Un champ explicite invalide est refusé, sans fallback.

Ne pas exécuter B/C avant livraison et vérification de l'image du vérificateur
et de l'observateur contenant ce correctif. Aucun changement du worker source
n'est nécessaire pour cette séparation d'identités.

## Étapes et gates distincts

1. Rattrapage borné (au plus dix minutes) : nouveau Job, nouveau préfixe réservé,
   bootstrap checkpoint sans `__TAIL__`. Conserver toutes les mesures de latence,
   y compris celles dépassant 60s pendant l'arrêt. Exiger zéro erreur, checkpoint
   monotone, lecture à la position attendue, lots intègres et réconciliation
   Snowpipe stricte. Une latence élevée en rattrapage ne devient pas un PASS SLO.
2. Seulement après succès de la réconciliation de reprise, nouveau processus
   borné de dix minutes reprenant le même checkpoint, nouveau raw réservé.
   Exiger les douze SLO, dont p95 <=60s, sur toute la fenêtre de ce processus,
   sans exclure les premières lignes après coup. L'intervalle entre processus
   reste inclus dans la latence observée. Aucun allègement de seuil.
3. Seulement après cette qualification complète, préparer le soak de trente
   minutes avec le même contrat. Aucun soak autorisé par le seul gate rattrapage.

Pour chaque étape : suspendre l'observateur périodique de façon contrôlée,
attendre l'absence d'écrivain de preuve actif, vérifier les ConfigMaps et Jobs
sélectionnés, puis restaurer le réglage initial après arrêt confirmé des pods.
La preuve publiée conserve ses SLO et son historique d'alertes, même en breach.
Si la publication échoue ou reste incertaine, ne pas forcer le pointeur S3.

## Limites qui restent ouvertes

Un restart propre n'est pas encore un crash entre raw/manifeste/checkpoint.
L'identité S3/Snowflake n'est pas l'oracle indépendant du journal source.
Le ledger canonique n'est pas une table d'état métier courant. Le C/U/D, le
replay Snowflake, les scénarios de panne et la certification navigateur restent
des critères distincts du goal ; ce protocole ne les remplace pas.
