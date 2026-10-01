# Sécurité et limites d’exposition

## Authentification réelle

Par défaut, le cockpit v1 écoute sur `127.0.0.1:8844` et **n'a pas
d'authentification applicative propre**. Le control plane v2 exige en revanche
une session applicative : le premier administrateur active son compte avec un
lien à usage unique, puis se connecte. La chart n’ajoute pas de Service ni
d’Ingress pour le cockpit. Garder le port-forward privé ; ne pas exposer le
cockpit v1 au réseau avec un simple tunnel public. Le RBAC Kubernetes protège
l'ouverture du port-forward, pas les requêtes d'une personne qui peut déjà
atteindre ce port local.

L’authentification optionnelle délègue l’identité à un proxy OIDC de confiance.
Configurer `controlPlane.auth.enabled`, `userHeader`, `groupsHeader`,
`operatorGroups`/`adminGroups` et la référence `proxySecret`. Le proxy doit
écraser les en-têtes reçus et ajouter `x-quadringent-proxy-secret`. Une identité
sans groupe d’exploitation peut lire mais pas écrire. Le secret est fourni par
`QUADRINGENT_AUTH_PROXY_SECRET`, jamais en argument de processus.

Le backend autorise un bind réseau seulement avec configuration auth ; celle-ci
ne crée pas le proxy et ne prouve pas son isolation. **Sans secret partagé et
restriction réseau, des en-têtes forgés peuvent usurper une identité.** Ces règles
relèvent du déploiement du site, pas d’une sécurité créée automatiquement par Helm.

## Flux et réseau

| Origine → destination | Port / transport | Usage |
|---|---|---|
| Navigateur → loopback ou proxy | 8844 local ; HTTPS du proxy selon site | UI, API, SSE |
| Lecteur/sondes → IBM i | ports TLS déclarés, généralement 9471/9475/9476 | JDBC, sign-on, commande |
| Capture → S3, DynamoDB, STS | HTTPS 443 | brut, preuve, checkpoint, identité AWS |
| Verifier → Snowflake, CloudWatch, S3 | HTTPS 443 | mesures bornées et publication de preuve |
| Collecteur de coûts → CloudWatch et catalogue public AWS | HTTPS 443 | volume S3 Standard et tarif régional |
| Collecteur de coûts → OpenCost | port du service déclaré, généralement 9003 | allocation namespace et actifs du cluster |
| Control plane → S3 | HTTPS 443 | lecture des clés de preuve autorisées |
| Control plane → API Kubernetes | HTTPS du cluster | Jobs `create/get/patch` |
| Jobs / résolution | DNS TCP/UDP 53 selon cluster | résolution des services |

Aucune NetworkPolicy n’est livrée : DNS, endpoints privés, proxy et filtrage CNI
dépendent du site. Ce n’est pas une preuve d’isolation réseau. Le propriétaire
choisit et valide les règles d’entrée/sortie avant une exposition partagée.
La télémétrie est désactivée par défaut ; son opt-in et payload sont documentés
[dans le guide dédié](docs/product/telemetry.md).

## TLS, identités et secrets

TLS IBM i obligatoire, plaintext refusé. Le site fournit son CA via Secret monté
en lecture seule ; aucune autorité client n’est incluse dans les images. Vérifier
empreinte, chaîne, validité et noms attendus hors bande. Un montage `subPath`
nécessite un redémarrage contrôlé pour prendre une rotation en compte.

Les secrets attendus figurent dans [l’installation](docs/product/install-client.md).
Aucun mot de passe, token de licence, clé privée ou credential cloud ne doit entrer
dans Git, les URLs, les arguments de processus ou les logs. Les références de
secrets sont des métadonnées ; elles ne prouvent pas que le secret existe.
L’édition communautaire n’utilise aucun jeton de licence commerciale.

Exception bornée : le lien initial d'activation contient un jeton dans le
fragment `#/wizard/activate?token=…`. Ce fragment n'est pas envoyé au serveur
dans la requête HTTP, mais le lien reste sensible dans le presse-papiers,
l'historique local et la sortie de l'installateur. Le navigateur retire le
jeton de la barre d'adresse après lecture. Protéger la sortie d'installation
et le répertoire privé du site ; ne jamais joindre le lien à un ticket.

Les secrets remis une seule fois sont absents des réponses idempotentes
persistées et de l'audit. Un rejeu vérifie toujours l'identité et les droits
actuels ; il ne permet pas de récupérer une clé ou un lien perdu. La réémission
du lien initial est réservée au premier admin encore non activé, par l'opérateur
admin autorisé. Voir [le contrat API](docs/api-v2.md).

Lors d'une mise à niveau d'une ancienne base, arrêter les anciens processus
du control plane avant d'appliquer `0015_one_time_secrets`. Cette migration
expurge les JSON historiques sans modifier les secrets chiffrés ni les hashes
d'authentification. Elle ne purge pas les sauvegardes, WAL ou copies déjà
exportées. Conserver ces anciens supports comme données sensibles. La
restauration d'un ancien backup doit être migrée avant de servir l'API ; un
retour à un ancien binaire susceptible de réécrire ces secrets est exclu.

Le rôle de la flotte v1 autorise les Jobs du namespace : `create`, `get`,
`patch` ; son client restreint PATCH à `spec.suspend`. Quand le control plane
v2 est activé, deux rôles additionnels autorisent les Jobs et Secrets de
diagnostic, la lecture des Pods et de leurs journaux, ainsi que la création,
mise à jour et suppression des Deployments de lecteurs et chargeurs. Le rôle
v2 peut également créer et modifier les Secrets de source et destination.
Ces droits portent sur le **namespace entier** : réserver un namespace dédié
à Quadringent et restreindre l'accès au compte de service du control plane.
Le filtrage par table appliqué aux journaux dans l'API ne réduit pas la portée
de lecture de son identité Kubernetes. Les droits cloud du control plane,
de la capture et du verifier sont séparés. Pas de Cost Explorer.
Le collecteur de coûts est un processus séparé, installé avec le paquet Python.
Il utilise une identité AWS autorisée à `cloudwatch:GetMetricStatistics` et un
endpoint OpenCost fourni par le site. Aucun rôle, scheduler ou droit supplémentaire
n’est créé par cette intégration. Le control plane ne lit que son fichier local.
Le fichier atomique est privé (0600) ; les échecs de collecte ne publient ni
réponse fournisseur ni credential. Voir [FinOps](docs/finops.md) pour le périmètre.
Les modèles de Jobs restent des artefacts de confiance administrés par le site ;
la validation applicative ne remplace pas les politiques d’admission du cluster.

## Données, rétention et RGPD

Les lignes métier transitent dans S3 puis Snowflake et peuvent contenir des données
personnelles. Le responsable du traitement décide minimisation, base légale,
régions, chiffrement/KMS, accès, durée de conservation et procédure d’effacement.
Quadringent n’anonymise pas automatiquement les lignes répliquées.

Définir des règles S3 incluant versions et uploads incomplets, la rétention des
journaux et sauvegardes, et la politique Snowflake (Time Travel / Fail-safe compris).
Un delete source n’est pas une purge immédiate du brut ni des sauvegardes.
`helm uninstall` ne supprime pas ces copies. Un effacement demande un inventaire
et une autorisation du propriétaire, avec preuve distincte pour chaque stockage.

## Signalement et livraison privée

Signaler une faille en privé au propriétaire du dépôt par le canal déjà convenu,
ou via une alerte privée GitHub si celle-ci est activée. Ne pas ouvrir d’issue
publique avec données ou secret. Aucune adresse de support ni délai de réponse
non établi n’est promis.

L’historique d’origine n’est pas distribué : il contient une licence de site.
L'arbre destiné à la publication passe Gitleaks et le contrôle
d'identifiants avant ouverture du dépôt. Aucune activation Pages automatique.
