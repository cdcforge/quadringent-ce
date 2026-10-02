# Sauvegarder, restaurer et revenir à une version précédente

Ce guide concerne PostgreSQL du control plane v2. Il ne sauvegarde pas les
objets CDC, checkpoints, PVC d'état de flotte ou données Snowflake : leur
conservation et leur cohérence nécessitent des procédures propres au site.

Les commandes sont destinées à l'administrateur produit et à l'opérateur
plateforme autorisé sur le site client. Le rôle administrateur dans l'API
ne confère pas les permissions Kubernetes, PostgreSQL ou S3/GCS. Pour une
qualification, utiliser un environnement **DEV isolé**, avec accès client
explicitement autorisés et ressources déjà provisionnées. Les exemples ne
créent aucune infrastructure cloud.

## Préparer les accès et les références

Conserver dans un espace privé : version, commit et admission de release,
chart et checksum, digests OCI des trois images, valeurs Helm effectives,
références des secrets et backup antérieur à toute mise à jour. La release
candidate et la précédente doivent être disponibles dans le registry autorisé.
Voir [installation](install-default.md) et [admission native](native-qualification.md).

Préserver **les valeurs originales** de `SECRET_KEY` et `TOKEN_PEPPER` du
Secret `<nom-complet-chart>-v2-secrets`, ainsi que les références des secrets
PostgreSQL et des connexions externes. La clé Fernet protège les secrets de
connexion en base ; le pepper est nécessaire aux jetons existants. La chart
conserve ces deux valeurs par `lookup` et `resource-policy: keep` dans le même
namespace. Une nouvelle release ou un autre namespace exige leur récupération
par le canal de secrets du client **avant** le démarrage v2 : laisser la chart
générer de nouvelles valeurs empêcherait de relire les secrets restaurés.
Ne pas afficher ces valeurs ni les inclure dans les logs, Git ou un rapport.
Un dump SQL ne remplace pas cette conservation séparée.

Les exemples utilisent les clients PostgreSQL compatibles avec la version du
serveur, `helm`, `kubectl` et, selon le stockage, un client AWS ou Google déjà
authentifié. Fournir le mot de passe par un fichier `PGPASSFILE` privé en 0600,
préparé par le canal de secrets du client ; aucun DSN avec mot de passe dans
les arguments. Ne pas activer `set -x`.

## Sauvegarde et intégrité

Pour PostgreSQL embarqué, activer `postgres.backup.enabled` dans les valeurs
relues du site. Le CronJob `<service-postgres>-backup` utilise l'image PostgreSQL
épinglée pour `pg_dump --format=custom`, puis décode entièrement l'archive avec
`pg_restore --file=/dev/null`. Il écrit un reçu local sur le volume partagé.
L'uploader control plane exige ce reçu lié au hash exact si `pg_restore` n'est
pas disponible localement ; il relit ensuite l'objet et compare longueur et
SHA256 avant d'annoncer le succès. Son identité S3/GCS doit pouvoir écrire **et
relire** le préfixe `postgres.backup.destinationPrefix`.
Si `networkPolicy.enabled=true`, la chart autorise le pod de backup seulement
pour les trois labels exacts nom/instance/composant de la même release, sur le
port PostgreSQL. L’entrée backup disparaît quand la sauvegarde est désactivée.

Le reçu `quadringent.pg-backup-validation.v1` contient le validateur
`pg_restore-full-sql-decode`, la taille et le SHA256. Il atteste le décodage par
l'initContainer de confiance, sans authentification indépendante ni preuve de
restauration. Le reçu reste sur le volume temporaire du Job : le conserver par
le canal privé avant nettoyage du Job si nécessaire. Il n'est pas publié
comme objet séparé par le script. Ne pas confondre la rétention du Job avec
celle des sauvegardes dans le stockage.

Pour une sauvegarde manuelle d'une base autorisée :

```sh
set -eu
umask 077
export PGHOST='<hôte-source-autorisé>' PGPORT='5432'
export PGUSER='<utilisateur-sauvegarde>' PGDATABASE='<base-source>'
export PGPASSFILE='/chemin/prive/pgpass-source'
pg_dump --format=custom --no-owner --no-privileges --file /chemin/prive/preupgrade.dump
pg_restore --file=/dev/null /chemin/prive/preupgrade.dump
shasum -a 256 /chemin/prive/preupgrade.dump
```

Pour télécharger une sauvegarde existante, choisir **un** stockage, sans
modifier les droits du client :

Pour S3 :

```sh
set -eu
umask 077
aws s3 cp 's3://<bucket>/<préfixe>/<objet>.dump' /chemin/prive/restoration.dump
shasum -a 256 /chemin/prive/restoration.dump
pg_restore --file=/dev/null /chemin/prive/restoration.dump
```

Ou, pour GCS :

```sh
set -eu
umask 077
gcloud storage cp 'gs://<bucket>/<préfixe>/<objet>.dump' /chemin/prive/restoration.dump
shasum -a 256 /chemin/prive/restoration.dump
pg_restore --file=/dev/null /chemin/prive/restoration.dump
```

Comparer le hash à celui conservé lors de la publication réussie ou au reçu
original protégé. Un hash recalculé seul ne prouve pas l'origine du fichier.
Refuser une archive vide, tronquée, un décodage en échec ou une discordance.
Le dump peut contenir des données sensibles ; accès et rétention restent privés.

## Restaurer dans une base neuve isolée

Provisionner séparément, avec l'autorisation du client, un serveur/base de
restauration sans accès des workloads actifs. Ne jamais viser la base courante.
Les paramètres suivants doivent désigner cet environnement isolé ; contrôler
l'hôte et le nom avant toute commande. L'utilisateur doit pouvoir créer la
base et les objets nécessaires.

```sh
set -eu
umask 077
export PGHOST='<hôte-restauration-isolé>' PGPORT='5432'
export PGUSER='<administrateur-restauration>'
export PGPASSFILE='/chemin/prive/pgpass-restauration'
export PGDATABASE='quadringent_restore_dev'
createdb --maintenance-db=postgres "$PGDATABASE"
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges \
  --dbname "$PGDATABASE" /chemin/prive/restoration.dump
psql --no-psqlrc --set=ON_ERROR_STOP=1 --command='SELECT current_database();'
```

`createdb` doit échouer si la cible existe : ne pas supprimer ni vider cette
base pour contourner l'échec. Ne pas utiliser `--clean` contre une base active.
Vérifier ensuite les tables et comptes attendus, les comptages de référence,
les données et la capacité de déchiffrement avec les secrets originaux. Le
succès `pg_restore` seul ne qualifie pas le fonctionnement du produit.

Tester d'abord le control plane **de la version de sauvegarde**, avec la base
isolée, ses clés/pepper d'origine et les intégrations externes désactivées ou
strictement isolées. Pour une base externe, la chart utilise
`postgres.enabled=false` et `externalDatabase.existingSecret`/
`externalDatabase.existingSecretKey` : le secret DSN est préparé par le canal
client, jamais écrit dans les commandes. Éviter de créer une seconde flotte
active ou de réutiliser un pipeline de production pendant ce contrôle.

## Mettre à jour et revenir en arrière

Le démarrage v2 applique automatiquement les migrations verrouillées. Vérifier
leur compatibilité sur une copie isolée **avant** la mise à jour. Arrêter les
écritures pendant la fenêtre approuvée et produire une sauvegarde préupgrade.
Archiver la chart précédente et les valeurs relues ; les valeurs doivent
épingler `image.digest`, `controlPlane.image.digest` et `verifier.imageDigest`
aux digests admis de la même release. Un tag mutable n'est pas un pin.

À partir de 0.2.5, la chart relit le StatefulSet PostgreSQL existant et conserve
les métadonnées de son template PVC immutable. L'identité Kubernetes utilisée
par Helm doit pouvoir lire ce StatefulSet ; une lecture refusée bloque le rendu.
Les labels des nouvelles claims sont indépendants de la version du produit.
Ne pas utiliser `--force`, supprimer le StatefulSet ou recréer le PVC pour
contourner un refus de mise à jour. La préversion 0.2.4 changeait un label du
template PVC et ne convient pas au parcours de mise à jour depuis 0.2.3.

Le contrat de rendu 0.2.3 → 0.2.5 → 0.2.3 conserve le template PVC initial.
La conservation des UID et des données sur Kubernetes est encore à vérifier
pour ce candidat ; ce contrat ne qualifie pas un retour d'une installation
neuve 0.2.5 vers 0.2.3. Un rendu hors ligne (`helm template`) ne lit pas l'état
du cluster et ne peut attester la conservation des métadonnées existantes.

Après vérification des checksums de l'archive chart candidate et des références
images, depuis le contexte Kubernetes explicite et autorisé :

```sh
helm upgrade --install '<release>' /chemin/prive/quadringent-0.2.5.tgz \
  --kube-context '<contexte-autorisé>' --namespace '<namespace>' \
  --values /chemin/prive/site-candidate-pinned.yaml --wait --timeout 5m
```

Un retour Helm réussi ou `--wait` ne prouve pas les données, l'identité cloud
ou la reprise CDC. Relire la version, les digests exécutés, l'API et les données
avec les procédures de qualification du site.

Si les migrations sont compatibles avec la version précédente, appliquer
l'archive chart précédente avec ses valeurs/digests épinglés, puis vérifier
le fonctionnement. Sinon, maintenir les écritures arrêtées, restaurer le dump
préupgrade dans **une nouvelle base isolée**, préserver les clés/pepper, tester
l'ancienne version contre cette base puis faire approuver le basculement client.
Les écritures postérieures au dump doivent être réconciliées avant reprise.
Ne jamais promettre qu'un `helm rollback` annule une migration PostgreSQL :
il remet des manifests Kubernetes, pas les données ni le schéma de la base.

## Portée des preuves

Un contrôle local PostgreSQL jetable a vérifié la restauration de 50 lignes
synthétiques identiques à la source, le décodage d'une archive complète et le
refus d'une archive tronquée gardant son en-tête PGDMP. Cela vérifie le chemin
archive/restauration local. La qualification de ce lot exact sur GKE, EKS ou
VM exige les exécutions autorisées et les preuves du site ; ce guide ne les
atteste pas.
