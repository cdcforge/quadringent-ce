# Versions et livraison

La seule version éditable est `project.version` de `pyproject.toml`.
`python scripts/sync_version.py` dérive Chart version/appVersion, Java, UI et
lockfile. `--check` est obligatoire en CI. L’API `/v1/version` lit les métadonnées
installées ; les images lisent le même pyproject copié lors du build. Un tag
doit correspondre exactement : `--check --release v0.2.3`.

## Compatibilité

Version `0.x` : interfaces encore susceptibles d’évoluer, changements documentés
dans CHANGELOG. Une correction patch conserve le contrat `/v1`. Les ajouts
compatibles sont documentés et les lecteurs doivent conserver les valeurs nulles
et les états inconnus. Une rupture de champs ou de sens exige une version mineure
avant 1.0, puis majeure, avec migration et coexistence d’une nouvelle API (`/v2`)
plutôt qu’une rupture silencieuse de `/v1`. Cette politique ne certifie pas la
compatibilité des instances historiques non testées.

## Préparer l'historique public

Avant d'ouvrir un dépôt, revoir exactement son arbre et son historique.
L’historique original ne doit pas y entrer. `scripts/export_private.py` exporte les fichiers suivis et
non ignorés après contrôle, sans `.git` ni caches. Créer un **nouveau dépôt**
à partir de cet export ; ne pas simplement réécrire ou forcer une branche
de l'ancien dépôt, car des commits peuvent rester accessibles par les refs de
pull requests et les caches GitHub. Conserver l’original dans le dépôt privé.
Les empreintes SHA256 des identifiants propres aux sites de validation sont
conservées dans une liste privée **hors du dépôt** : une empreinte non salée
peut révéler un nom prévisible par essai de dictionnaire. Avant l'export final,
exécuter `python scripts/check_publication.py --history-ref HEAD
--private-denylist-file /chemin/hors/depot/identifiants.sha256`, puis
`python scripts/export_private.py --private-denylist-file
/chemin/hors/depot/identifiants.sha256 DESTINATION`. Cette liste contient une
empreinte hexadécimale SHA256 par ligne. Conserver le fichier et ses empreintes
hors du dépôt public et des artefacts. La CI sans ce fichier vérifie les règles
génériques ; elle ne remplace pas ce contrôle privé de prépublication.
Dans l'export déjà contrôlé, faire `git add --force --all` après `git init` :
un ignore Git global peut masquer des fichiers suivis par le dépôt source,
notamment ses fichiers d'instructions. Avant le premier commit, comparer la
liste et les empreintes SHA256 de **tous** les fichiers exportés avec les
fichiers effectivement ajoutés. Refuser la migration s'il en manque un.

Sur le nouveau dépôt, exécuter
`python scripts/check_publication.py --history-ref HEAD` et Gitleaks sur l'arbre et l'historique qui seront
publiés. La commande de contrôle d'identités parcourt tous les objets Git
atteignables depuis la révision indiquée, y compris les anciennes versions
de fichiers et les messages de commit ; elle renvoie uniquement les règles,
un numéro de fichier pour l'arbre ou un identifiant d'objet Git pour
l'historique, jamais les noms ni le contenu. Vérifier aussi
chaque tag qui serait publié, les refs distantes et les artefacts de release.
Un arbre propre ne suffit pas si l'historique contient encore une identité.
La CI scanne l'ascendance de `HEAD`, qui est le code proposé par la PR. Sur le
nouveau dépôt destiné à être public, contrôler en plus **toutes** les refs
qui y existent avec `gitleaks git . --log-opts=--all`, après s'être assuré
qu'aucune branche ou ref de PR privée étrangère à la release n'y est présente.

Le moteur, l’API et le cockpit local sont sous Apache-2.0, sans quota commercial.
La décision et la migration sont documentées dans [licensing.md](licensing.md).
Conserver `LICENSE`, `NOTICE`, les textes de `licenses/` et les
[avis des dépendances](../THIRD_PARTY_NOTICES.md) avec chaque artefact.

## Workflow préparé, désactivé par défaut

`release.yml` est manuel, sur un tag existant correspondant à la version. Il
réutilise la dernière CI canonique réussie pour le SHA exact du tag, au lieu de
relancer les mêmes tests et images. Le contrôle GitHub exige une exécution
`push` sur `main` du même dépôt et les sept jobs réussis ; une PR, un autre
commit ou un job ignoré ne suffit pas. Un succès antérieur ne masque pas
un run plus récent en échec ou encore actif. Sans cette preuve, la release s'arrête
avant les builds et n'émet aucune nouvelle CI automatiquement.
Avant les constructions OCI, le job `validate` scanne aussi les deux verrous
Python hashés des images control plane et vérificateur avec la base Trivy
actualisée. Il refuse les vulnérabilités HIGH/CRITICAL corrigées, ainsi qu'un
rapport vide, partiel ou dont l'inventaire diffère des verrous. Ce contrôle
précoce réduit les constructions inutiles ; les scans des six variantes
d'images, les SBOM, signatures et reçus restent requis.
Les jobs d’écriture exigent `PUBLICATION_APPROVED=true` et
l’environnement `publication-approved`. Sur GitHub Free, les réviseurs requis
ne sont pas disponibles pour un dépôt privé : la première construction privée
est autorisée par la variable et le déclenchement manuel, après revue locale.
Configurer des réviseurs sur l'environnement après ouverture du dépôt et avant
la release suivante. Aucun push de tag ne publie automatiquement.
Les jobs CI et release utilisent Helm `v4.1.4`, version du rendu de référence
de la chart, pour que la comparaison octet à octet soit reproductible.

Après autorisation, il construit trois index OCI locaux sans upload, vérifie
leur SHA source et prépare six vues exactes, une par image et architecture.
Les scans et les six SBOM SPDX utilisent ces vues locales. Le helper
`scripts/release_oci.py` scelle les empreintes avant le login GHCR ; il les
revérifie avant `skopeo copy --all --preserve-digests`. Cette promotion publie
les trois index construits et leurs blobs sans rebuild et compare les digests
obtenus aux digests du build. Le reçu `passed.json`, composé uniquement de
chemins relatifs et
d'empreintes SHA256, est conservé un jour dans l'artefact CI distinct
`oci-scan-receipt`, après les scans et le scellement réussis. Il n'est pas
téléchargé par le job de release ni joint au brouillon. Pour qualifier les
images installées, le rapprocher du run, de son commit et des étapes de
scan et de promotion ; ce reçu seul n'atteste pas leur réussite.

Un second artefact `oci-admission-metadata`, conservé un jour, contient les
**21 originaux** : par composant, le wrapper `index.json`, `oci-layout`,
l'index et les deux manifestes/configurations. Aucune couche n'est exportée.
Les octets sont comparés au reçu avant et après copie, les architectures
et labels SHA/version/source sont vérifiés, puis le texte passe les règles
de `check_publication` et un scan secret bloquant toutes sévérités. Limites :
2 MiB par fichier, 42 MiB au total. Ces deux artefacts temporaires ne sont
pas téléchargés par le job de release ni joints au brouillon public.
L'admission privée authentifie leurs IDs, digests ZIP, run et tentative,
puis compare seulement les métadonnées locales aux empreintes CI : elle
ne prétend pas vérifier localement toutes les couches. Leur confidentialité
dépend de celle du dépôt ; attendre leur expiration ou les retirer avant
un passage public.

Les rapports JSON de secrets et les contenus de couches restent privés
sur le runner. Le workflow produit les signatures
cosign, empaquette la chart avec
version et appVersion sans préfixe `v`, puis attache chart, wheel, sdist,
`release-manifest.json` directement consommable par `quadringent install`,
`images.json`, scans et SBOM à une **release brouillon**. Vérifier séparément
la visibilité des packages GHCR : la confidentialité du dépôt ne suffit pas
à corriger celle d’un package préexistant.
Les images de l'édition communautaire sont publiées sous
`ghcr.io/<owner>/quadringent-community-runtime` ; le manifeste de release fixe
l'URL réelle et les trois digests à utiliser. Ce package est distinct du package
historique `quadringent-community`, qui reste privé et hors de ce workflow.
Avant la première publication, vérifier que le nouveau nom n’existe pas déjà
dans GHCR. Sa création utilise le `GITHUB_TOKEN` du dépôt communautaire qui
exécute le workflow ; les droits de l’ancien package ne sont pas réutilisés.
Le gate Trivy SARIF est limité à `HIGH,CRITICAL` par
`limit-severities-for-sarif: true` : sans cette option, l'action ignore le
filtre de sévérité pour le format SARIF et échoue aussi sur `LOW/MEDIUM`.
Ces trois rapports SARIF portent sur les vues locales amd64. Un second gate
Trivy lit les vues locales arm64 pour les vulnérabilités HIGH/CRITICAL
corrigibles et sonde les secrets de **toutes** sévérités sur les six variantes.
Un scan de fichiers supplémentaire par image couvre aussi les configurations
OCI et toutes les couches, y compris les couches de base et les fichiers
ensuite supprimés : chaque entrée est copiée séparément dans un staging privé,
sans appliquer les whiteouts ni extraire de liens. Les empreintes de ce
staging font partie du reçu de scan. Une détection bloque le workflow avant
login ou push. Les rapports JSON et les copies de couches restent dans
`RUNNER_TEMP`, avec des permissions privées ; ils ne sont jamais joints aux
artefacts Actions ni à la release. Seuls les SARIF de vulnérabilités et les
six SBOM sont téléversés. Ces règles de détection ne prouvent pas l'absence
universelle de secrets, notamment dans un contenu chiffré ou binaire.
Les trois images actualisent les paquets système de leur base Debian pendant
le build ; conserver les rapports du scan avant toute publication.
L'action Docker n'envoie pas ses enregistrements de build bruts comme
artefacts Actions : ils incluent des adresses internes éphémères du runner.
Les journaux et les rapports de vulnérabilités restent disponibles pour la revue.
La chart est distribuée comme archive `.tgz` de la release ; elle n'est pas
publiée dans le registre OCI, dont le push Helm a échoué lors du premier essai
privé sur GHCR. L'installation par le CLI utilise la chart du sdist.

L'installation CLI hors checkout utilise le wheel **et** le sdist de cette
même release : ce dernier fournit `chart/` et `deploy/terraform/`, indiqués
par `--assets-dir`. Le wheel seul ne contient pas ces fichiers. Voir
[le guide d'installation](product/install-default.md#installation-depuis-les-artefacts-de-release).

Le workflow Pages exige les mêmes gardes et reste manuel. Ne pas l’activer pour
la revue privée : un site Pages peut être public même quand son dépôt est privé.
La présentation est servie localement depuis `site/` pendant cette phase.

Les workflows préparés ne constituent pas une preuve d’exécution distante.
La validation privée doit inclure les digests réellement obtenus, l’installation
neuve sur site autorisé, les preuves de réplication et les avis de licence.
Avant de rendre le dépôt visible, vérifier le même commit dans les trois
étiquettes OCI, le wheel, le sdist et la chart ; scanner l'arbre, tout
l'historique destiné au public et le contenu extrait des artefacts. Exécuter
la CI sur le dépôt distant privé, relire les journaux Actions et les artefacts
qu'elle publie, puis installer ces digests précis. Les essais EKS et VM
qualifient l'installation et l'exploitation du control plane ; ils ne valent
pas preuve de réplication IBM i sur ces cibles. Ne pas annoncer un maximum de
10 s tant qu'un essai incluant le démarrage à froid ne le démontre pas.

## Avis des dépendances

Le build du cockpit joint les textes complets des licences des dépendances npm
de production et du runtime Vite dans `dist/assets/third-party-notices.txt`.
Ils sont lus depuis les paquets installés, dont les versions doivent correspondre
au lockfile. Une licence absente bloque le build. Ce fichier accompagne les
polices et le JavaScript dans l’image du cockpit et reste accessible à
`/assets/third-party-notices.txt`.

Les images Java joignent les sources exactes des cinq JAR dépendants et vérifient
leurs empreintes. Les avis Python sont collectés depuis les distributions
installées, y compris l’extra Snowflake dans le verifier. Une distribution sans
texte de licence bloque la collecte. Les bibliothèques restent sous leurs
licences respectives, dont IPL-1.0 pour JTOpen et certaines portions de l’adaptateur
Debezium ; elles ne sont pas relicenciées sous Apache-2.0.

## CI privée économique sur runner local

Un runner Linux ARM64 dédié peut exécuter la CI et la release complètes dans
Docker Desktop. Configurer `PRIVATE_RUNNER_LABELS` avec une liste JSON de labels
incluant `self-hosted`, `Linux`, `ARM64` et un label propre au dépôt. Après revue
privée de l'arbre, de l'historique et des workflows, fixer `PRIVATE_APPROVED_SHA`
au commit exact et `PRIVATE_APPROVED_TAG` au tag de version correspondant.
Le dépôt doit rester privé et figé pendant cette campagne.

Lorsque le dépôt est privé et que `PRIVATE_RUNNER_LABELS` est renseigné, les
sept jobs CI acceptent seulement un événement `push` sur `main` pour le SHA
approuvé et le workflow `ci.yml@refs/heads/main`. Les quatre jobs de release
acceptent seulement le déclenchement manuel sur le tag approuvé, son workflow
`release.yml@refs/tags/<tag>`, le même SHA et la même version en entrée.
Toute autre identité ignore les jobs avant allocation : aucun fallback vers
un runner hébergé payant. Les dépendances `needs` et la condition de succès
restent requises. Des jobs ignorés ne satisfont pas l'admission canonique.
Sans ce mode privé configuré, le comportement hébergé existant est conservé.

La validation de release compare le tag résolu, le SHA approuvé et `main`
distant actuel. Les sept succès CI exacts restent obligatoires avant les
builds. `PUBLICATION_APPROVED=true` et `publication-approved` restent requis
pour les écritures, même privées. Les scans des six variantes, SBOM, signatures
OIDC, reçus, promotion par digest et contrôles d'admission restent inchangés.
Le run release reste sur le tag : aucune signature locale isolée ne remplace
cette preuve GitHub complète.

Préférer un daemon Docker-in-Docker dédié sans socket Docker hôte ni partage
de HOME. DinD exige un conteneur privilégié et partage le noyau de la VM Docker
Desktop ; une VM Linux dédiée sans partage Mac renforce cette séparation.
Les labels de runner ne sont pas une frontière de sécurité. Installer une
politique de pré-job indépendante, en lecture seule, qui refuse toute identité
non approuvée avant les steps, puis vérifier ce refus sur la distribution
réelle. Ajouter une restriction serveur aux workflows revus si le plan GitHub
la propose. Ne pas inscrire le runner avant ce contrôle. Retirer le runner
avant ouverture publique et conserver l'ancien historique dans le dépôt privé.

Les minutes self-hosted ne sont pas facturées selon la
[tarification GitHub Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
actuelle ; stockage Actions, caches et Packages restent soumis aux quotas et
peuvent être facturés. Vérifier ces quotas et le disque local avant lancement.
Les variables approuvées se changent après revue de chaque nouvelle source,
jamais pour admettre des images ou attestations d'un ancien commit.
