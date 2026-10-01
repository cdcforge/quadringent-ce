# Composants tiers et redistribution

Apache-2.0 couvre le code original de Quadringent. Les dépendances ci-dessous
restent des œuvres séparées sous leurs licences propres ; ce changement de
licence ne les convertit pas en code Apache. Leurs avis doivent être conservés
dans toute redistribution. Les composants tiers sont fournis sans garantie ;
les exclusions de garantie et de responsabilité de leurs licences s’appliquent
à tous leurs contributeurs. Les éventuels engagements de support de Quadringent
sont pris par son fournisseur, au nom de lui seul.

## Java

Les versions sont définies dans `java/pom.xml`. Les bibliothèques sont livrées
en JAR séparés, sans modification par Quadringent. Les images du lecteur et du
cockpit joignent leurs archives sources correspondantes dans
`/usr/share/quadringent/sources/`, avec leurs en-têtes d’origine. Le manifeste
`java/third-party-sources.sha256` vérifie ces archives pendant le build.

| Composant | Version | Licence et attribution |
|---|---|---|
| `io.debezium:ibmi-journal-parsing` | 3.6.1.Final | Apache-2.0 ; Copyright Debezium Authors |
| `io.debezium:jt400-override-ccsid` | 3.6.1.Final | Apache-2.0 et portions sous IPL-1.0 ; conserver notamment l’en-tête IBM d’`AS400JDBCDriverForcedCcsid.java` |
| `net.sf.jt400:jt400` | 21.0.7 | IPL-1.0 ; International Business Machines Corporation and others |
| `org.slf4j:slf4j-api`, `org.slf4j:slf4j-simple` | 2.0.12 | MIT ; QOS.ch et contributeurs |

Textes : [Apache-2.0](LICENSE), [IBM IPL-1.0](licenses/JTOpen-IPL-1.0.html),
[SLF4J MIT](licenses/SLF4J-MIT.txt). Le code source de JTOpen et des portions
IBM reste disponible sous IPL-1.0, y compris ses avis et exclusions de garantie.

Sources amont exactes, également disponibles sans les images :

- [ibmi-journal-parsing 3.6.1.Final](https://repo.maven.apache.org/maven2/io/debezium/ibmi-journal-parsing/3.6.1.Final/ibmi-journal-parsing-3.6.1.Final-sources.jar)
- [jt400-override-ccsid 3.6.1.Final](https://repo.maven.apache.org/maven2/io/debezium/jt400-override-ccsid/3.6.1.Final/jt400-override-ccsid-3.6.1.Final-sources.jar)
- [JTOpen 21.0.7](https://repo.maven.apache.org/maven2/net/sf/jt400/jt400/21.0.7/jt400-21.0.7-sources.jar)
- [slf4j-api 2.0.12](https://repo.maven.apache.org/maven2/org/slf4j/slf4j-api/2.0.12/slf4j-api-2.0.12-sources.jar)
- [slf4j-simple 2.0.12](https://repo.maven.apache.org/maven2/org/slf4j/slf4j-simple/2.0.12/slf4j-simple-2.0.12-sources.jar)

## Python

Les dépendances sont déclarées dans `pyproject.toml` et `requirements*.txt` ;
les images cockpit et verifier utilisent les versions et empreintes des fichiers
`docker/*-requirements.txt`. L’extra Snowflake est inclus dans cet examen.

| Famille | Licence retenue ou applicable |
|---|---|
| boto3, botocore, s3transfer, requests, Snowflake Connector, pyOpenSSL, sortedcontainers | Apache-2.0 |
| python-dateutil, packaging, cryptography | Apache-2.0 parmi leurs licences alternatives ; conserver tous les textes fournis |
| asn1crypto, charset-normalizer, filelock, jmespath, platformdirs, PyJWT, pytz, six, tomlkit, urllib3 | MIT |
| cffi | MIT-0 pour la version verrouillée |
| idna, pycparser | BSD-3-Clause |
| typing-extensions | PSF-2.0 |
| certifi | MPL-2.0, applicable à ses fichiers ; conserver la licence et l’accès aux sources |

Les installations conservent les fichiers `*.dist-info` et les avis inclus dans
les paquets, notamment ceux des composants incorporés par Snowflake Connector.
Les images regroupent également ces textes dans
`/usr/share/quadringent/python-licenses/third-party-notices.txt`, avec les versions
et liens vers leurs distributions sources dans `distributions.json`.
Le code Python et les données certifi distribués dans l’image sont lisibles ;
leurs sources amont sont accessibles sur la page PyPI de la version indiquée.
La MPL de certifi ne remplace pas la licence des fichiers de Quadringent.

## Interface et outils de construction

`ui/package-lock.json` fixe les versions. React, React DOM et Scheduler sont
sous MIT. Les polices Geist, IBM Plex Sans et IBM Plex Mono sont sous OFL-1.1 ;
elles sont distribuées avec leurs avis, sans modification ni vente isolée.
Le build assemble leurs textes complets et les avis du runtime Vite dans
`ui/dist/assets/third-party-notices.txt`, servi à la même adresse dans le cockpit.

Le graphe npm de construction contient MIT, ISC, BSD-3-Clause, Apache-2.0,
OFL-1.1 et CC-BY-4.0 (`caniuse-lite`, données de construction). Ces outils ne
sont pas copiés en bloc dans l’image finale. Toute redistribution de leurs
paquets doit conserver les attributions correspondantes.
Les outils Python de développement restent soumis à leurs licences, conservées
dans leurs distributions : pytest, pluggy, iniconfig, pyflakes, PyYAML, Ruff,
mypy, mypy-extensions, librt, build et pyproject-hooks sont sous MIT ; Pygments
sous BSD-2-Clause et pathspec sous MPL-2.0. Ils ne sont pas inclus dans le paquet
Quadringent. Les fichiers de pathspec restent sous MPL, sans changer la licence
du code analysé par les outils.

## Images de base et mises à jour

Les bases Python/Debian et Eclipse Temurin conservent leurs propres avis,
notamment `/usr/share/doc` et `/opt/java/openjdk/legal`. Le JRE est distribué
avec ses textes GPL et Classpath Exception ; l’exception doit être conservée.
Le JDK, Maven et Node restent dans les étapes de build.

Une mise à jour de dépendance exige un nouvel examen de ses licences et avis.
L’analyse des versions présentes ne constitue pas une approbation de toutes les
versions futures permises par une plage de dépendances.
