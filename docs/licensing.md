# Édition communautaire et modèle du projet

Le moteur CDC, l’API, le cockpit local, la chart et la documentation originale
de Quadringent sont sous [Apache-2.0](../LICENSE). Leur utilisation, modification
et redistribution, y compris commerciale, sont permises selon cette licence.
Aucune clé d’activation, limite commerciale de tables ou expiration n’est
imposée par Quadringent. Les contraintes de ressources, contrôles d’accès et
preuves exigées avant une action restent applicables.

Le cockpit communautaire permet de configurer les liaisons, suivre les preuves,
comprendre les incidents et consulter les coûts. Il fait partie du logiciel
libre ; les fonctions essentielles d’exploitation ne dépendent pas d’un achat.
Les coûts IBM i, cloud et Snowflake restent à la charge de l’exploitant.

Des prestations d’installation, de support et d’exploitation pourront être
vendues. Une future console avancée ou un service hébergé pourra avoir ses
propres conditions, avec des fonctions distinctes de gestion d’équipes ou de
plusieurs sites. Il n’existe ici ni offre payante disponible ni engagement de
livraison de ces fonctions. Aucun abonnement n’est nécessaire à cette édition.

Les versions publiées sous Apache-2.0 conservent les droits déjà accordés.
Un utilisateur peut maintenir un fork ou proposer une offre commerciale,
sans devoir publier ses modifications ni verser de redevance à l’auteur,
sous réserve des obligations de licence et des composants tiers.
Apache-2.0 ne concède pas les marques de Quadringent.

Les contributions suivent Apache-2.0 sans transfert de droits d’auteur ;
voir [CONTRIBUTING.md](../CONTRIBUTING.md). Les composants tiers gardent leurs
propres licences : [avis et sources](../THIRD_PARTY_NOTICES.md).

## Migration depuis la préparation sous BSL

Le changement concerne cette version préparée du projet. Le module de
vérification des licences commerciales et l’outil d’émission sont retirés.
`QUADRINGENT_LICENSE_KEY` et `controlPlane.licenseSecret` ne sont plus utilisés.
Une ancienne référence à un Secret ne sera pas injectée dans les pods ; la
chart ne supprime aucun Secret existant. Un `controlPlane.licenseKey` en clair
reste refusé pour éviter la diffusion accidentelle d’un ancien jeton.

La télémétrie reste désactivée par défaut. Lorsqu’elle est explicitement
activée, son champ `tier` vaut `community` et ne lit aucun jeton.
La publication publique suit une revue du code, de l'historique et des
artefacts. Cette décision ne modifie pas les droits de la licence Apache-2.0.
