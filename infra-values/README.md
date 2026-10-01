# Exemples d’infrastructure

Tous les identifiants de ce dossier sont synthétiques. Les digests illustrent
le format attendu, sans attester une image disponible. Aucun fichier ne décrit
un déploiement réel ni ne doit être appliqué tel quel.

Copier les seuls fichiers nécessaires dans le dépôt privé du site et adapter
identités, compte, bucket, rôles IAM, trust IRSA/OIDC, préfixes, objets Snowflake,
références de secrets, CA et digests issus de la livraison validée.

La chart accepte `dev`, `int`, `test` ou `staging`, au plus un lecteur, et exige
un namespace identique à `site.namespace`. L’exemple utilise `dev` et
`quadringent-demo`. Toute promotion PROD reste refusée. Les policies sont des exemples bornés ; leur
application exige la validation du propriétaire de l’infrastructure. Le control
plane n’a aucun droit de facturation, ni de suppression de pods.

Suivre [le guide d’installation](../docs/product/install-client.md), puis
[FinOps](../docs/finops.md) et [sécurité](../SECURITY.md). Les archives R&D et
les anciennes preuves de campagne ne sont pas distribuées.

## Catalogue de démonstration synthétique

`fleet-catalog-int.json` est une fixture statique entièrement synthétique pour
les rendus Helm hors ligne. Le nom historique du fichier ne désigne aucun
environnement observé. Les tables correspondent au manifeste de démonstration ;
les colonnes `ID`, volumes nuls, receiver `DEMORECV`, séquence `1` et date
`2000-01-01` sont des valeurs de fixture, sans mesure de client ni preuve de
continuité. La continuité `uncertain` empêche de présenter cet exemple comme une
qualification réelle.

Ne pas utiliser ce catalogue comme observation runtime ou admission de release.
Un site réel doit produire son catalogue privé à partir de sa propre sonde.

`fleet-sidecar-int.json` est généré uniquement depuis ce catalogue synthétique.
Ses volumes sont nuls, ses coûts et sa progression sont non observés, sa
continuité est incertaine et toute certification est bloquée. Le format
metadata-only impose encore `historical` dans ses sous-enveloppes source et
observabilité : il désigne ici
l'enveloppe de la fixture datée de 2000, jamais des observations de client.
Le nom historique du fichier ne constitue aucune preuve d'installation.

La provenance structurée du pipeline est `quality.evidence_kind: simulation`.
Son étape source reste `unknown` : le catalogue vide n'atteste aucune source
réelle. Le résumé et le détail du payload expliquent les enveloppes techniques
`historical` et les admissions de plan ; ces champs fermés ne sont pas des
mesures. `tests/test_public_samples.py` régénère le document à date fixe depuis
le catalogue synthétique, ajoute ces seules annotations compatibles et compare
exactement le payload.
