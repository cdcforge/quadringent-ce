# Référence des valeurs du chart quadringent

Généré depuis `chart/values.schema.json` par `scripts/generate_chart_values_doc.py` — ne pas éditer à la main.

| Clé | Type | Obligatoire (racine) | Description |
|---|---|---|---|
| `gcpIdentityMode` | `string` | non | GKE : annotation Workload Identity ; VM Compute Engine dédiée : identité du nœud via le serveur de métadonnées, sans annotation GKE. |
| `image` | `object` | oui | Image du conteneur principal (lecteur de journal). |
| `image.repository` | `string` | oui | Registre + dépôt de l'image, ex. ghcr.io/quadringent/quadringent. |
| `image.digest` | `string` | non | Digest d'image, vide autorisé quand le défaut du chart s'applique. |
| `image.pullPolicy` | `string` | oui | Politique de pull de l'image du conteneur. |
| `image.pullSecret` | `string` | non | Nom du Secret imagePullSecret pour un registre privé. Vide : aucun imagePullSecret monté. |
| `deployment` | `object` | oui | Palier de déploiement. La chart est une surface hors production : aucun profil PROD n'est accepté. |
| `deployment.environment` | `string` | oui | Environnement non productif déclaré. Aucun chemin de promotion PROD. |
| `deployment.productionPromotionAllowed` | `boolean` | oui | Doit rester false : refusé au rendu sinon. |
| `site` | `object` | oui | Identité déclarée du site, injectée dans le ConfigMap -site (QUADRINGENT_*). Aucun défaut : une release sans déclaration de site est refusée par les gardes du chart. |
| `site.connectionDeclared` | `boolean` | non | false : aucune connexion IBM i n'est encore déclarée (installateur « mode par défaut », avant l'assistant en trois écrans) — les champs de connexion IBM i/Snowflake ci-dessous restent vides et ne sont pas exigés. true (défaut) : comportement historique, tous ces champs redeviennent obligatoires. |
| `site.snowflakeCreditPrice` | `string | number` | non | Chaîne ou nombre : accepte aussi bien un --set texte qu'un --set numérique. |
| `site.costCurrency` | `string` | non | Devise du tarif (ex. EUR, USD). Doit être déclarée avec snowflakeCreditPrice. |
| `site.id` | `string` | non | Identifiant court du site (minuscules, chiffres, tirets). |
| `site.awsAccountId` | `string` | non | Compte AWS de douze chiffres. Obligatoire quand storage.backend=aws ; doit rester vide quand storage.backend=gcs. |
| `site.namespace` | `string` | non | Namespace Kubernetes déclaré : le rendu est refusé pour tout autre namespace. Vide : la garde deployment.environment/siteGuards exige explicitement sa déclaration (message dédié, non un rejet de schéma). |
| `site.ibmiHost` | `string` | non | Hôte ou IP de la partition IBM i. |
| `site.ibmiUser` | `string` | non | Utilisateur IBM i du lecteur. |
| `site.sourceSchema` | `string` | non | Schéma (bibliothèque) source IBM i. |
| `site.proofTable` | `string` | non | Table de la voie de preuve : doit figurer dans fleetTables. |
| `site.journalName` | `string` | non | Nom du journal IBM i observé. |
| `site.rawBucket` | `string` | non | Bucket S3/GCS brut du site. |
| `site.rawPrefixRoot` | `string` | non | Racine des préfixes objets, ex. <produit>/<bibliothèque>. |
| `site.checkpointTable` | `string` | non | Table DynamoDB des checkpoints. |
| `site.destinationDatabase` | `string` | non | Base Snowflake de destination. |
| `site.destinationSchema` | `string` | non | Schéma Snowflake de destination. |
| `site.destinationId` | `string` | non | Identifiant de la destination Snowflake. |
| `site.destinationPrefix` | `string` | non | Préfixe des objets Snowflake provisionnés. Vide : QUADRINGENT appliqué par le runtime. |
| `site.destinationMode` | `string` | non | Voie miroir/historique. Vide ou copy_merge : chemin COPY INTO + MERGE existant. streaming : Snowpipe Streaming + MERGE miroir loader-driven (exige streaming.profileSecret). |
| `site.snowflakeAccount` | `string` | non | Compte Snowflake du site. |
| `site.snowflakeConnection` | `string` | non | Alias de connexion Snowflake (optionnel). |
| `site.snowflakeWarehouse` | `string` | non | Warehouse Snowflake existant, jamais créé ni suspendu par la chart. Vide : warehouse dédié dérivé du préfixe. |
| `site.awsProfile` | `string` | non | Profil AWS local (optionnel). |
| `site.autonomousProofName` | `string` | non | Nom du document de preuve autonome. Vide : quadringent-autonomous-latest.json appliqué par le runtime. |
| `site.fleetTables` | `array<string>` | non | Manifeste des tables de la flotte. |
| `site.keyedTables` | `array<string>` | non | Tables du manifeste dotées d'une clé métier. |
| `site.provisionedStages` | `array<string>` | non | Stages Snowflake déjà provisionnés. |
| `site.reservableTables` | `array<string>` | non | Tables réservables au-delà de la table de preuve. Vide par défaut. |
| `site.proofKeyColumns` | `array<string>` | non | Colonnes composant la clé de la voie de preuve. |
| `site.forbiddenFragments` | `array<string>` | non | Fragments interdits dans les identifiants générés (garde anti-collision). |
| `nameOverride` | `string` | non | Surcharge du nom court du chart. |
| `fullnameOverride` | `string` | non | Surcharge du nom complet des ressources. |
| `serviceAccount` | `object` | oui | ServiceAccount de capture (lecteur de journal, et Jobs/Deployments créés par l'exécuteur de pipeline v2 — droits S3/GCS/DynamoDB du site). |
| `serviceAccount.create` | `boolean` | non | true (défaut) : la chart crée ce ServiceAccount et pose son annotation d'identité cloud selon storage.backend. false : fourni hors de la chart. |
| `serviceAccount.name` | `string` | non | Nom Kubernetes (DNS-1123 label) : minuscules, chiffres, tirets, borné à 63 caractères. |
| `serviceAccount.roleArn` | `string` | non | ARN IAM (IRSA) du rôle AWS associé au ServiceAccount. Obligatoire et exclusif de gcpServiceAccount quand create=true et storage.backend=aws. |
| `serviceAccount.gcpServiceAccount` | `string` | non | Compte GCP attendu, lié par Workload Identity sur GKE ou attaché à la VM. Obligatoire et exclusif de roleArn quand create=true et storage.backend=gcs. |
| `serviceAccount.annotations` | `object` | non | Annotations additionnelles du ServiceAccount. Fusionnées avec l'annotation d'identité cloud (eks.amazonaws.com/role-arn ou iam.gke.io/gcp-service-account) gérée par la chart selon storage.backend. |
| `replicaCount` | `integer` | oui | 0 ou 1 seulement : IBM i ne tolère pas deux lecteurs de journal concurrents. |
| `pilot` | `object` | non | Palier de certification borné : un Job unique exécute le worker avec un budget strict, le Deployment permanent reste à 0. |
| `pilot.enabled` | `boolean` | non | Active le Job pilote borné. |
| `pilot.runId` | `string` | non | Identifiant neuf réservé par le worker avant tout accès source. |
| `pilot.maxPolls` | `integer` | non | Entier strictement positif. |
| `pilot.maxSeconds` | `integer` | non | Entier strictement positif. |
| `pilot.shutdownMarginSeconds` | `integer` | non | Entier positif ou nul. |
| `pilot.ttlSecondsAfterFinished` | `integer` | non | Entier positif ou nul. |
| `pilot.proofWindow` | `object` | non | Fenêtre de preuve du pilote. |
| `pilot.proofWindow.enabled` | `boolean` | non | Active la fenêtre de preuve. |
| `pilot.proofWindow.id` | `string` | non | Identifiant de la fenêtre de preuve. |
| `pilot.proofWindow.seconds` | `integer` | non | Entier strictement positif. |
| `pilot.proofWindow.count` | `integer` | non | Entier strictement positif. |
| `verifierServiceAccountName` | `string` | non | ServiceAccount des charges de vérification (CronJob d'observabilité, Job verifier), créé hors chart avec son rôle IAM (IRSA) dédié. |
| `verification` | `object` | non | Observateur de la voie de preuve du site. Ne crée aucun rôle ni permission. |
| `verification.enabled` | `boolean` | non | Active le Job verifier. |
| `verification.publishProof` | `boolean` | non | Publie la preuve vérifiée. |
| `verification.slo` | `object` | non |  |
| `verification.slo.enabled` | `boolean` | non | Active la vérification SLO. |
| `verification.slo.policyJson` | `string` | non | Politique SLO versionnée, chargée via --set-file. |
| `verification.imageDigest` | `string` | non | Digest d'image, vide autorisé quand le défaut du chart s'applique. |
| `observability` | `object` | non | Rafraîchissement indépendant de la preuve existante : ne démarre jamais de capture. |
| `observability.enabled` | `boolean` | non | Active le CronJob d'observabilité. |
| `observability.suspend` | `boolean` | non | Suspend le CronJob sans le supprimer. |
| `observability.schedule` | `string` | non | Expression cron du relevé. |
| `observability.policyJson` | `string` | non | Politique SLO embarquée. |
| `observability.imageDigest` | `string` | non | Digest d'image, vide autorisé quand le défaut du chart s'applique. |
| `observability.alerts` | `object` | non | Alertes SLO optionnelles. Sans destination déclarée, le CronJob mesure en silence. |
| `observability.alerts.webhookUrl` | `string` | non | Webhook d'alerte (optionnel). |
| `observability.alerts.snsTopic` | `string` | non | Sujet SNS d'alerte (optionnel). |
| `fleetObserve` | `object` | non | Mesure périodique de la livraison Snowflake de la flotte. |
| `fleetObserve.enabled` | `boolean` | non | Active le CronJob fleet-observe. |
| `fleetObserve.suspend` | `boolean` | non | Suspend le CronJob sans le supprimer. |
| `fleetObserve.schedule` | `string` | non | Expression cron du relevé. |
| `fleetObserve.imageDigest` | `string` | non | Digest d'image, vide autorisé quand le défaut du chart s'applique. |
| `fleetObserve.manifestBudget` | `integer` | non | Entier strictement positif. |
| `safety` | `object` | non |  |
| `safety.maxConsecutiveErrors` | `integer` | non | Jamais plus de 3 : chaque erreur est un nouveau sign-on IBM i. |
| `bootstrap` | `object` | non | Amorçage explicite du lecteur. |
| `bootstrap.mode` | `string` | non | checkpoint exige un état durable, tail lit une fenêtre finie, explicit exige receiver + sequence. |
| `bootstrap.receiver` | `string` | non | Receiver de départ (mode explicit). |
| `bootstrap.sequence` | `string` | non | Séquence de départ (mode explicit). |
| `nodeSelector` | `object` | non | Sélecteur de nœuds Kubernetes, propre au cluster du site. |
| `tolerations` | `array<object>` | non | Tolérances Kubernetes standard (passées telles quelles au manifeste). |
| `podAntiAffinity` | `object` | non | Anti-affinité déclarative vis-à-vis d'autres charges du cluster du site. |
| `podAntiAffinity.enabled` | `boolean` | non | Active l'anti-affinité. |
| `podAntiAffinity.namespaces` | `array<string>` | non | Namespaces ciblés par l'anti-affinité. |
| `podAntiAffinity.matchLabels` | `object` | non | Carte libre de labels/sélecteurs Kubernetes (clé/valeur arbitraires). |
| `affinity` | `object` | non | Règle d'affinité Kubernetes brute (affinity/nodeAffinity/podAffinity), passée telle quelle. Alternative avancée à podAntiAffinity pour les profils experts. |
| `resources` | `object` | oui | Requêtes et limites de ressources Kubernetes pour un conteneur. |
| `resources.requests` | `object` | non |  |
| `resources.requests.cpu` | `string` | non | CPU demandé, ex. 50m ou "1". |
| `resources.requests.memory` | `string` | non | Mémoire demandée, ex. 512Mi. |
| `resources.limits` | `object` | non |  |
| `resources.limits.cpu` | `string` | non | CPU maximal, ex. "1". |
| `resources.limits.memory` | `string` | non | Mémoire maximale, ex. 2Gi. |
| `podSecurityContext` | `object` | non | securityContext appliqué au Pod (niveau Pod, distinct du securityContext du conteneur qui reste géré par la chart). Vide par défaut : aucun changement de comportement. |
| `imagePullSecrets` | `array<object>` | non | imagePullSecrets additionnels au-delà de image.pullSecret, pour les sites multi-registres. |
| `podAnnotations` | `object` | non | Annotations additionnelles posées sur le Pod du lecteur. |
| `podLabels` | `object` | non | Labels additionnels posés sur le Pod du lecteur. |
| `extraEnv` | `array<object>` | non | Variables d'environnement additionnelles injectées dans le conteneur du lecteur (format EnvVar Kubernetes). |
| `extraVolumes` | `array<object>` | non | Volumes additionnels montés sur le Pod du lecteur (format Volume Kubernetes). |
| `extraVolumeMounts` | `array<object>` | non | Points de montage additionnels sur le conteneur du lecteur (format VolumeMount Kubernetes). |
| `networkPolicy` | `object` | non | NetworkPolicy optionnelle restreignant le trafic du namespace. Désactivée par défaut : aucun changement de comportement réseau existant. |
| `networkPolicy.enabled` | `boolean` | non | Active le rendu d'une NetworkPolicy de base. |
| `ibmi` | `object` | oui | Câblage d'exécution du lecteur. |
| `ibmi.host` | `string` | non | Hôte IBM i. |
| `ibmi.user` | `string` | non | Utilisateur IBM i. |
| `ibmi.schema` | `string` | non | Schéma (bibliothèque) source. |
| `ibmi.table` | `string` | non | Table observée. |
| `ibmi.journalLibrary` | `string` | non | Bibliothèque du journal. |
| `ibmi.journalName` | `string` | non | Nom du journal. |
| `ibmi.sourceTimeZone` | `string` | non | Zone IANA explicite, vérifiée contre l'IBM i à la connexion. Jamais inférée à UTC. |
| `ibmi.passwordSecret` | `object` | non | Secret portant le mot de passe IBM i. Jamais dans les values. |
| `ibmi.passwordSecret.name` | `string` | non |  |
| `ibmi.passwordSecret.key` | `string` | non |  |
| `as400` | `object` | oui | Réglages TLS et ports JTOpen. |
| `as400.tls` | `boolean` | non | Doit rester true : le plaintext IBM i est interdit. |
| `as400.allowPlaintext` | `boolean` | non | Doit rester false. |
| `as400.tlsCaFile` | `string` | non | Chemin absolu de l'autorité TLS, sans remontée de répertoire. |
| `as400.tlsCaSecret` | `object` | non |  |
| `as400.tlsCaSecret.name` | `string` | non |  |
| `as400.tlsCaSecret.key` | `string` | non |  |
| `as400.databasePort` | `string | integer` | non | Surcharge du port base de données JTOpen. Vide : défaut du driver. |
| `as400.signonPort` | `string | integer` | non | Surcharge du port sign-on JTOpen. Vide : défaut du driver. |
| `as400.commandPort` | `string | integer` | non | Surcharge du port commande JTOpen. Vide : défaut du driver. |
| `consoleSnapshot` | `object` | non |  |
| `consoleSnapshot.enabled` | `boolean` | non | Active la publication du snapshot console. |
| `consoleSnapshot.s3Key` | `string` | non | Clé objet du snapshot console. |
| `consoleSnapshot.intervalSeconds` | `number` | non | Intervalle de publication, en secondes. Doit être > 0. |
| `streaming` | `object` | non | Profil Snowpipe Streaming, utilisé uniquement lorsque site.destinationMode=streaming. |
| `streaming.profileSecret` | `object` | non | Secret portant le profile.json du SDK Snowpipe Streaming. Jamais dans les values. |
| `streaming.profileSecret.name` | `string` | non |  |
| `streaming.profileSecret.key` | `string` | non |  |
| `streaming.mountPath` | `string` | non | Chemin de montage du volume secret contenant profile.json. |
| `controlPlane` | `object` | non | API de consultation read-only, indépendante du lecteur IBM i. |
| `controlPlane.enabled` | `boolean` | non |  |
| `controlPlane.licenseKey` | `string` | non | Obsolète : refusée si non vide au rendu (aucune licence commerciale requise). Tolérée vide pour compatibilité ascendante. |
| `controlPlane.licenseSecret` | `object` | non | Ancienne référence de secret de licence, tolérée mais jamais injectée dans le pod (compatibilité ascendante). |
| `controlPlane.licenseSecret.name` | `string` | non |  |
| `controlPlane.licenseSecret.key` | `string` | non |  |
| `controlPlane.image` | `object` | non |  |
| `controlPlane.image.repository` | `string` | non |  |
| `controlPlane.image.digest` | `string` | non | Digest d'image, vide autorisé quand le défaut du chart s'applique. |
| `controlPlane.image.pullPolicy` | `string` | non | Politique de pull de l'image du conteneur. |
| `controlPlane.image.allowedRepositories` | `array<string | null>` | non | Registres autorisés pour l'image control-plane (immuabilité par digest exigée dans tous les cas). Un élément peut être absent (null) lorsqu'il est ajouté par --set sur un index isolé. |
| `controlPlane.replicaCount` | `integer` | non | Toujours 1 : le control plane read-only n'est pas dimensionné pour plusieurs répliques dans cette version. |
| `controlPlane.source` | `string` | non | Source de lecture live du control plane. |
| `controlPlane.windowProof` | `string` | non | Preuve de fenêtre de destination immuable (optionnelle). |
| `controlPlane.infrastructureCostsSource` | `string` | non | Source de collecte des coûts d'infrastructure (fichier séparé). |
| `controlPlane.port` | `integer` | non | Port d'écoute du control plane. |
| `controlPlane.refreshSeconds` | `number` | non | Intervalle de rafraîchissement de la lecture live. |
| `controlPlane.catalogRefreshSeconds` | `integer` | non | Entier positif ou nul. |
| `controlPlane.progressionSeconds` | `integer` | non | Entier strictement positif. |
| `controlPlane.host` | `string` | non | Adresse d'écoute. Loopback seul sans authentification. |
| `controlPlane.auth` | `object` | non | Authentification déléguée à l'IdP du site via un proxy de confiance. |
| `controlPlane.auth.enabled` | `boolean` | non |  |
| `controlPlane.auth.userHeader` | `string` | non | En-tête HTTP portant l'identité utilisateur, posé par le proxy. |
| `controlPlane.auth.groupsHeader` | `string` | non | En-tête HTTP portant les groupes, posé par le proxy. |
| `controlPlane.auth.operatorGroups` | `string | array` | non | Chaîne unique ou liste de chaînes (accepte le scalaire produit par --set aussi bien que la syntaxe liste). |
| `controlPlane.auth.adminGroups` | `string | array` | non | Chaîne unique ou liste de chaînes (accepte le scalaire produit par --set aussi bien que la syntaxe liste). |
| `controlPlane.auth.proxySecret` | `object` | non | Secret partagé prouvant le transit par le proxy. |
| `controlPlane.auth.proxySecret.secretName` | `string` | non |  |
| `controlPlane.auth.proxySecret.secretKey` | `string` | non |  |
| `controlPlane.telemetry` | `object` | non | Télémétrie produit opt-in, désactivée par défaut. |
| `controlPlane.telemetry.enabled` | `boolean` | non |  |
| `controlPlane.telemetry.endpoint` | `string` | non | Vide = endpoint vendeur ; https obligatoire sinon. |
| `controlPlane.serviceAccount` | `object` | non |  |
| `controlPlane.serviceAccount.create` | `boolean` | non |  |
| `controlPlane.serviceAccount.name` | `string` | non | Nom Kubernetes (DNS-1123 label) : minuscules, chiffres, tirets, borné à 63 caractères. |
| `controlPlane.serviceAccount.roleArn` | `string` | non | ARN IAM (IRSA) du rôle AWS associé au ServiceAccount. Obligatoire et exclusif de gcpServiceAccount quand storage.backend=aws. |
| `controlPlane.serviceAccount.gcpServiceAccount` | `string` | non | Compte GCP attendu, lié par Workload Identity sur GKE ou attaché à la VM. Obligatoire et exclusif de roleArn quand storage.backend=gcs. |
| `controlPlane.serviceAccount.annotations` | `object` | non | Annotations additionnelles du ServiceAccount. Fusionnées avec l'annotation d'identité cloud (eks.amazonaws.com/role-arn ou iam.gke.io/gcp-service-account) gérée par la chart selon storage.backend. |
| `controlPlane.launch` | `object` | non | Lancement de Jobs de capture depuis le contrôleur. Désactivé par défaut. |
| `controlPlane.launch.enabled` | `boolean` | non |  |
| `controlPlane.launch.jobTemplate` | `string` | non | Gabarit de Job JSON, chargé via --set-file. |
| `controlPlane.launch.fleetCatalog` | `string` | non | Catalogue de flotte JSON, chargé via --set-file. |
| `controlPlane.launch.fleetSidecar` | `string` | non | Sidecar de flotte JSON, chargé via --set-file. |
| `controlPlane.launch.fleetConsoleSource` | `string` | non | Source console de flotte (optionnelle, dérivée par défaut de storage.rawBucket/site.rawPrefixRoot). |
| `controlPlane.fleetState` | `object` | non |  |
| `controlPlane.fleetState.persistence` | `object` | non |  |
| `controlPlane.fleetState.persistence.enabled` | `boolean` | non | true : PVC persistant. false : emptyDir volatil. |
| `controlPlane.fleetState.persistence.size` | `string` | non | Taille du PVC, ex. 256Mi. |
| `controlPlane.fleetState.persistence.storageClassName` | `string` | non | "" = StorageClass par défaut ; "-" = provisionnement dynamique désactivé. |
| `controlPlane.resources` | `object` | non | Requêtes et limites de ressources Kubernetes pour un conteneur. |
| `controlPlane.resources.requests` | `object` | non |  |
| `controlPlane.resources.requests.cpu` | `string` | non | CPU demandé, ex. 50m ou "1". |
| `controlPlane.resources.requests.memory` | `string` | non | Mémoire demandée, ex. 512Mi. |
| `controlPlane.resources.limits` | `object` | non |  |
| `controlPlane.resources.limits.cpu` | `string` | non | CPU maximal, ex. "1". |
| `controlPlane.resources.limits.memory` | `string` | non | Mémoire maximale, ex. 2Gi. |
| `controlPlane.v2` | `object` | non | Surface v2 (FastAPI/uvicorn, Postgres) — second conteneur du même Pod, v1 conservée à côté. Désactivée par défaut. |
| `controlPlane.v2.enabled` | `boolean` | non |  |
| `controlPlane.v2.image` | `object` | non |  |
| `controlPlane.v2.image.repository` | `string` | non | Vide = controlPlane.image.repository. |
| `controlPlane.v2.image.digest` | `string` | non | Digest d'image, vide autorisé quand le défaut du chart s'applique. |
| `controlPlane.v2.port` | `integer` | non | Entier strictement positif. |
| `controlPlane.v2.orgId` | `string` | non | Identifiant d'organisation v2 ; vide = site.id. |
| `controlPlane.v2.readerPollSeconds` | `number | string` | non | Nombre strictement positif (entier ou décimal). Accepte aussi la forme chaîne : --set n'auto-convertit en nombre que les entiers, une valeur décimale comme 15.1 reste une chaîne. |
| `controlPlane.v2.loaderPollSeconds` | `number | string` | non | Nombre strictement positif (entier ou décimal). Accepte aussi la forme chaîne : --set n'auto-convertit en nombre que les entiers, une valeur décimale comme 15.1 reste une chaîne. |
| `controlPlane.v2.loaderHistoryMode` | `string` | non | Streaming Snowpipe ou MERGE SQL synchrone pour les petits flux CDC. |
| `controlPlane.v2.loaderFlushEachBatch` | `boolean` | non | Force un flush Snowpipe après chaque lot pour réduire la latence ; peut augmenter les coûts et la fréquence des requêtes. |
| `controlPlane.v2.resources` | `object` | non | Requêtes et limites de ressources Kubernetes pour un conteneur. |
| `controlPlane.v2.resources.requests` | `object` | non |  |
| `controlPlane.v2.resources.requests.cpu` | `string` | non | CPU demandé, ex. 50m ou "1". |
| `controlPlane.v2.resources.requests.memory` | `string` | non | Mémoire demandée, ex. 512Mi. |
| `controlPlane.v2.resources.limits` | `object` | non |  |
| `controlPlane.v2.resources.limits.cpu` | `string` | non | CPU maximal, ex. "1". |
| `controlPlane.v2.resources.limits.memory` | `string` | non | Mémoire maximale, ex. 2Gi. |
| `postgres` | `object` | non | Postgres embarqué par la chart pour l'état du control plane v2. Actif par défaut ; externalDatabase le remplace en mode expert. |
| `postgres.runAsUser` | `integer` | non | UID de l'utilisateur postgres de l'image (70 en alpine, 999 en Debian) : exigé par runAsNonRoot. |
| `postgres.enabled` | `boolean` | non |  |
| `postgres.image` | `object` | non |  |
| `postgres.image.repository` | `string` | non |  |
| `postgres.image.digest` | `string` | non | Digest d'image immuable (sha256:<64 hex>). Jamais de tag mouvant. |
| `postgres.image.pullPolicy` | `string` | non | Politique de pull de l'image du conteneur. |
| `postgres.database` | `string` | non | Nom de la base Postgres. |
| `postgres.username` | `string` | non | Utilisateur applicatif Postgres. |
| `postgres.port` | `integer` | non | Entier strictement positif. |
| `postgres.storage` | `object` | non |  |
| `postgres.storage.size` | `string` | non | Taille du volume Postgres, ex. 5Gi. |
| `postgres.storage.storageClassName` | `string` | non | "" = StorageClass par défaut du cluster. |
| `postgres.resources` | `object` | non | Requêtes et limites de ressources Kubernetes pour un conteneur. |
| `postgres.resources.requests` | `object` | non |  |
| `postgres.resources.requests.cpu` | `string` | non | CPU demandé, ex. 50m ou "1". |
| `postgres.resources.requests.memory` | `string` | non | Mémoire demandée, ex. 512Mi. |
| `postgres.resources.limits` | `object` | non |  |
| `postgres.resources.limits.cpu` | `string` | non | CPU maximal, ex. "1". |
| `postgres.resources.limits.memory` | `string` | non | Mémoire maximale, ex. 2Gi. |
| `postgres.backup` | `object` | non | Sauvegarde logique périodique (pg_dump) vers le stockage objet du site. Désactivée par défaut. |
| `postgres.backup.enabled` | `boolean` | non |  |
| `postgres.backup.schedule` | `string` | non | Expression cron (fuseau UTC). |
| `postgres.backup.destinationPrefix` | `string` | non | Préfixe objet des sauvegardes, sous storage.rawBucket. |
| `postgres.backup.serviceAccountName` | `string` | non | Vide = identité de capture par défaut du site. |
| `externalDatabase` | `object` | non | Postgres géré externe (mode expert), alternative à `postgres` (mutuellement exclusifs). |
| `externalDatabase.url` | `string` | non | DSN SQLAlchemy complet (postgresql+psycopg://...). Alternative à existingSecret. |
| `externalDatabase.existingSecret` | `string` | non | Secret du site portant le DSN sous existingSecretKey. |
| `externalDatabase.existingSecretKey` | `string` | non |  |
| `preflight` | `object` | non | Diagnostic pré-vol borné. Désactivé par défaut. |
| `preflight.enabled` | `boolean` | non |  |
| `preflight.serviceAccountName` | `string` | non | ServiceAccount du pod pré-vol. |
| `preflight.snowflakeOidc` | `boolean` | non | Monte un jeton OIDC projeté et active le contrôle Snowflake en cluster. |
| `preflight.image` | `object` | non |  |
| `preflight.image.repository` | `string` | non | Défaut : controlPlane.image.repository. |
| `preflight.image.digest` | `string` | non | Digest d'image, vide autorisé quand le défaut du chart s'applique. |
| `tuning` | `object` | oui | Réglages du lecteur, validés par le soak du 2026-08-27. Modifier avec prudence hors nouvelle mesure. |
| `tuning.batchEntries` | `integer` | oui | Entier strictement positif. |
| `tuning.maxDecodedEntries` | `integer` | oui | Entier strictement positif. |
| `tuning.journalBufferSize` | `integer` | oui | Entier strictement positif. |
| `tuning.readerTimeoutSeconds` | `number | string` | oui | Nombre strictement positif (entier ou décimal). Accepte aussi la forme chaîne : --set n'auto-convertit en nombre que les entiers, une valeur décimale comme 15.1 reste une chaîne. |
| `tuning.retrieveTimeoutMs` | `integer` | oui | Entier strictement positif. |
| `tuning.moreDataTimeoutMs` | `integer` | oui | Entier strictement positif. |
| `tuning.finalPositionTimeoutMs` | `integer` | oui | Entier strictement positif. |
| `tuning.pollSeconds` | `integer` | oui | Entier strictement positif. |
| `tuning.emptyProbe` | `boolean` | oui | Probe conditionnel : seulement si lag <= fenêtre. |
| `tuning.minPollSeconds` | `number | string` | oui | Nombre strictement positif (entier ou décimal). Accepte aussi la forme chaîne : --set n'auto-convertit en nombre que les entiers, une valeur décimale comme 15.1 reste une chaîne. |
| `tuning.tailProbe` | `boolean` | oui | Sonde bornée du receiver ATTACHED à chaque poll : une nouvelle entrée est visible sans relire le catalogue complet. |
| `tuning.catalogQueryTimeoutSeconds` | `integer` | oui | Entier strictement positif. |
| `tuning.receiverMetadataLimit` | `integer` | oui | Entier strictement positif. |
| `tuning.catalogCachePolls` | `integer` | oui | Entier strictement positif. |
| `tuning.catalogCacheSeconds` | `integer` | oui | Entier strictement positif. |
| `tuning.catalogMaxStale` | `integer` | oui | Entier strictement positif. |
| `tuning.catchUpBatchEntries` | `integer` | oui | Entier strictement positif. |
| `tuning.catchUpDivisor` | `integer` | oui | Entier strictement positif. |
| `tuning.metricsIntervalSeconds` | `integer` | oui | Entier strictement positif. |
| `storage` | `object` | oui |  |
| `storage.backend` | `string` | non | Backend de stockage durable : aws (S3+DynamoDB) ou gcs (bucket GCS unique). |
| `storage.rawBucket` | `string` | non | Bucket S3/GCS brut. |
| `storage.rawPrefix` | `string` | non | Préfixe objet de la table observée. |
| `storage.streamKey` | `string` | non | Clé de flux du checkpoint. |
| `storage.checkpointTable` | `string` | non | Table DynamoDB des checkpoints (backend aws). |
| `storage.checkpointBucket` | `string` | non | Bucket GCS des checkpoints (backend gcs). |
| `aws` | `object` | oui |  |
| `aws.region` | `string` | non | Région AWS du site, ex. eu-west-3. Obligatoire quand storage.backend=aws. |
| `costGuardrail` | `object` | non | Seuil indicatif AWS S3 pour une alerte externe ; la chart ne crée ni alarme ni budget. |
| `costGuardrail.enabled` | `boolean` | non | Affiche le seuil indicatif dans les notes Helm AWS. |
| `costGuardrail.requestAlarmThresholdPerDay` | `integer` | non | Entier strictement positif. |
